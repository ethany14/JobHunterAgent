"""Transactional repository for tool calls and their audit events."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.errors import (ApprovalBindingError, IdempotencyConflictError,
    InvalidToolCallTransitionError, StaleToolCallError, ToolCallNotFoundError)
from agent_runtime.models import ToolCallEventRow, ToolCallRow
from agent_runtime.security import canonical_json, redact_sensitive
from agent_runtime.types import (ToolCallEventRecord, ToolCallRecord, ToolCallRequest,
    ToolExecutionStatus, ToolResult, ToolRiskLevel, ToolSideEffect)


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ToolCallRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_or_create(self, *, request: ToolCallRequest, tool_version: str,
                      risk_level: ToolRiskLevel, scope_type: str, scope_id: str,
                      arguments_hash: str, redacted_arguments: dict,
                      side_effect: ToolSideEffect | None = None,
                      idempotent: bool | None = None) -> tuple[ToolCallRecord, bool]:
        if not request.idempotency_key:
            raise ValueError("A persisted tool call requires an idempotency key.")
        try:
            return self._get_or_create_once(request=request, tool_version=tool_version,
                risk_level=risk_level, scope_type=scope_type, scope_id=scope_id,
                arguments_hash=arguments_hash, redacted_arguments=redacted_arguments,
                side_effect=side_effect, idempotent=idempotent)
        except IntegrityError:
            # A concurrent creator may win the unique-key race. Only translate
            # that specific case; unrelated integrity failures remain visible.
            with self._session_factory() as session:
                row = session.scalar(self._idempotency_query(
                    request, scope_type, scope_id))
                if row is None:
                    raise
                if row.arguments_hash != arguments_hash:
                    raise IdempotencyConflictError(
                        "The idempotency key was already used with different arguments."
                    )
                return self._record(row), False

    def _get_or_create_once(self, *, request: ToolCallRequest, tool_version: str,
                            risk_level: ToolRiskLevel, scope_type: str, scope_id: str,
                            arguments_hash: str, redacted_arguments: dict,
                            side_effect: ToolSideEffect | None = None,
                            idempotent: bool | None = None) -> tuple[ToolCallRecord, bool]:
        with self._session_factory.begin() as session:
            row = session.scalar(self._idempotency_query(request, scope_type, scope_id))
            if row is not None:
                if row.arguments_hash != arguments_hash:
                    raise IdempotencyConflictError("The idempotency key was already used with different arguments.")
                return self._record(row), False
            now = datetime.now(UTC)
            row = ToolCallRow(call_id=str(uuid4()), scope_type=scope_type, scope_id=scope_id,
                tool_name=request.tool_name, tool_version=tool_version,
                idempotency_key=request.idempotency_key, arguments_hash=arguments_hash,
                arguments_json=canonical_json(redacted_arguments), risk_level=risk_level.value,
                tool_side_effect=side_effect.value if side_effect is not None else None,
                tool_idempotent=idempotent,
                status=ToolExecutionStatus.REQUESTED.value, retryable=False, attempt_count=0,
                max_attempts=request.max_attempts, timeout_seconds=request.timeout_seconds,
                version=0, event_sequence=1, created_at=now, updated_at=now)
            session.add(row)
            # There is intentionally no ORM relationship; flush the parent first
            # so SQLite can enforce the event foreign key within this transaction.
            session.flush()
            session.add(self._event(row, 1, "requested", None, ToolExecutionStatus.REQUESTED, {}))
            session.flush()
            return self._record(row), True

    @staticmethod
    def _idempotency_query(request: ToolCallRequest, scope_type: str, scope_id: str):
        return select(ToolCallRow).where(
            ToolCallRow.scope_type == scope_type, ToolCallRow.scope_id == scope_id,
            ToolCallRow.tool_name == request.tool_name,
            ToolCallRow.idempotency_key == request.idempotency_key)

    def require(self, call_id: str) -> ToolCallRecord:
        with self._session_factory() as session:
            row = session.get(ToolCallRow, call_id)
            if row is None:
                raise ToolCallNotFoundError("The tool call does not exist.")
            return self._record(row)

    def list_for_scope(self, scope_type: str, scope_id: str) -> list[ToolCallRecord]:
        """Return persisted calls for diagnostics without exposing raw arguments."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(ToolCallRow)
                .where(
                    ToolCallRow.scope_type == scope_type,
                    ToolCallRow.scope_id == scope_id,
                )
                .order_by(ToolCallRow.created_at, ToolCallRow.call_id)
            ).all()
            return [self._record(row) for row in rows]

    def find_by_idempotency(
        self,
        *,
        scope_type: str,
        scope_id: str,
        tool_name: str,
        idempotency_key: str,
    ) -> ToolCallRecord | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.scope_type == scope_type,
                    ToolCallRow.scope_id == scope_id,
                    ToolCallRow.tool_name == tool_name,
                    ToolCallRow.idempotency_key == idempotency_key,
                )
            )
            return None if row is None else self._record(row)

    def list_events(self, call_id: str) -> list[ToolCallEventRecord]:
        with self._session_factory() as session:
            rows = session.scalars(select(ToolCallEventRow).where(
                ToolCallEventRow.call_id == call_id).order_by(ToolCallEventRow.sequence)).all()
            return [ToolCallEventRecord(event_id=r.event_id, call_id=r.call_id,
                sequence=r.sequence, event_type=r.event_type,
                from_status=ToolExecutionStatus(r.from_status) if r.from_status else None,
                to_status=ToolExecutionStatus(r.to_status), payload=json.loads(r.payload_json),
                occurred_at=ensure_utc(r.occurred_at)) for r in rows]

    def transition(self, call_id: str, *, expected_version: int,
                   status: ToolExecutionStatus, event_type: str,
                   result: ToolResult | None = None, error_code: str | None = None,
                   error_message: str | None = None, retryable: bool | None = None,
                   increment_attempt: bool = False, payload: dict | None = None,
                   approval: tuple[str, str, str] | None = None,
                   execution_attempt_id: str | None = None,
                   execution_lease_until: datetime | None = None) -> ToolCallRecord:
        with self._session_factory.begin() as session:
            current = session.get(ToolCallRow, call_id)
            if current is None:
                raise ToolCallNotFoundError("The tool call does not exist.")
            if current.version != expected_version:
                raise StaleToolCallError("The tool call has a stale state version.")
            now = datetime.now(UTC)
            previous_status = ToolExecutionStatus(current.status)
            next_sequence = current.event_sequence + 1
            values = {"status": status.value, "version": expected_version + 1,
                "event_sequence": next_sequence, "updated_at": now,
                "result_json": result.model_dump_json() if result is not None else current.result_json,
                "error_code": error_code, "error_message": error_message,
                "retryable": current.retryable if retryable is None else retryable,
                "attempt_count": current.attempt_count + (1 if increment_attempt else 0)}
            if execution_attempt_id is not None:
                values["execution_attempt_id"] = execution_attempt_id
            if execution_lease_until is not None:
                values["execution_lease_until"] = execution_lease_until
            if approval:
                values.update(approval_tool_name=approval[0], approval_tool_version=approval[1], approval_arguments_hash=approval[2])
            changed = session.execute(update(ToolCallRow).where(
                ToolCallRow.call_id == call_id, ToolCallRow.version == expected_version).values(**values))
            if changed.rowcount != 1:
                raise StaleToolCallError("The tool call has a stale state version.")
            session.add(self._event(current, next_sequence, event_type,
                previous_status, status, payload or {}, occurred_at=now))
            session.flush()
            row = session.get(ToolCallRow, call_id)
            session.refresh(row)
            return self._record(row)

    def approve(self, call_id: str, *, expected_version: int, tool_name: str,
                tool_version: str, arguments_hash: str) -> ToolCallRecord:
        current = self.require(call_id)
        if current.status != ToolExecutionStatus.APPROVAL_REQUIRED:
            raise InvalidToolCallTransitionError("Only a call awaiting approval can be approved.")
        if (current.request.tool_name != tool_name or current.tool_version != tool_version
                or current.arguments_hash != arguments_hash):
            raise ApprovalBindingError("Approval does not match the tool version and arguments.")
        return self.transition(call_id, expected_version=expected_version,
            status=ToolExecutionStatus.APPROVED, event_type="approved",
            approval=(tool_name, tool_version, arguments_hash))

    def reject(self, call_id: str, *, expected_version: int) -> ToolCallRecord:
        current = self.require(call_id)
        if current.status != ToolExecutionStatus.APPROVAL_REQUIRED:
            raise InvalidToolCallTransitionError(
                "Only a call awaiting approval can be rejected."
            )
        return self.transition(
            call_id,
            expected_version=expected_version,
            status=ToolExecutionStatus.DENIED,
            event_type="user_rejected",
            error_code="user_rejected",
            error_message="The user rejected this tool call.",
            payload={"rejection_source": "user"},
        )

    def recover_expired_running(
        self, call_id: str, *, expected_version: int, now: datetime, retry_safe: bool
    ) -> ToolCallRecord:
        current = self.require(call_id)
        if current.status != ToolExecutionStatus.RUNNING:
            return current
        if current.execution_lease_until is None or current.execution_lease_until > now:
            raise InvalidToolCallTransitionError("The tool execution lease is still active.")
        if retry_safe:
            return self.transition(
                call_id, expected_version=expected_version,
                status=ToolExecutionStatus.FAILED,
                event_type="execution_lease_expired_retryable",
                error_code="execution_lease_expired",
                error_message="The prior read-only execution lease expired.",
                retryable=True,
            )
        return self.transition(
            call_id, expected_version=expected_version,
            status=ToolExecutionStatus.OUTCOME_UNKNOWN,
            event_type="execution_lease_expired_outcome_unknown",
            error_code="outcome_unknown",
            error_message="The write outcome could not be determined after lease expiry.",
            retryable=False,
        )

    def mark_outcome_unknown(
        self, call_id: str, *, expected_version: int, reason_code: str
    ) -> ToolCallRecord:
        current = self.require(call_id)
        if current.status == ToolExecutionStatus.OUTCOME_UNKNOWN:
            return current
        return self.transition(
            call_id,
            expected_version=expected_version,
            status=ToolExecutionStatus.OUTCOME_UNKNOWN,
            event_type="outcome_became_uncertain",
            error_code="outcome_unknown",
            error_message="The tool outcome requires manual review.",
            retryable=False,
            payload={"reason_code": reason_code},
        )

    @staticmethod
    def _event(row: ToolCallRow, sequence: int, event_type: str,
               from_status: ToolExecutionStatus | None, to_status: ToolExecutionStatus,
               payload: dict, occurred_at: datetime | None = None) -> ToolCallEventRow:
        return ToolCallEventRow(event_id=str(uuid4()), call_id=row.call_id, sequence=sequence,
            event_type=event_type, from_status=from_status.value if from_status else None,
            to_status=to_status.value, payload_json=canonical_json(redact_sensitive(payload)),
            occurred_at=occurred_at or datetime.now(UTC))

    @staticmethod
    def _record(row: ToolCallRow) -> ToolCallRecord:
        result = ToolResult.model_validate_json(row.result_json) if row.result_json else None
        return ToolCallRecord(call_id=row.call_id,
            request=ToolCallRequest(tool_name=row.tool_name, arguments=json.loads(row.arguments_json),
                idempotency_key=row.idempotency_key, timeout_seconds=row.timeout_seconds,
                max_attempts=row.max_attempts), status=ToolExecutionStatus(row.status),
            tool_version=row.tool_version, risk_level=ToolRiskLevel(row.risk_level),
            side_effect=ToolSideEffect(row.tool_side_effect) if row.tool_side_effect else None,
            idempotent=row.tool_idempotent,
            execution_attempt_id=row.execution_attempt_id,
            execution_lease_until=(ensure_utc(row.execution_lease_until)
                if row.execution_lease_until else None),
            scope_type=row.scope_type, scope_id=row.scope_id, arguments_hash=row.arguments_hash,
            result=result, error_code=row.error_code, error_message=row.error_message,
            retryable=row.retryable, attempt_count=row.attempt_count, version=row.version,
            event_sequence=row.event_sequence, approval_tool_name=row.approval_tool_name,
            approval_tool_version=row.approval_tool_version,
            approval_arguments_hash=row.approval_arguments_hash,
            created_at=ensure_utc(row.created_at), updated_at=ensure_utc(row.updated_at))

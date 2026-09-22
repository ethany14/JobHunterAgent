"""Transactional persistence for versioned agent sessions."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from collections.abc import Sequence

from sqlalchemy import inspect, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.security import canonical_json, redact_sensitive
from agent_runtime.sessions.errors import (
    MessageConflictError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    SessionTerminalError,
    StaleSessionError,
)
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.models import (
    AgentSessionEventRow,
    AgentSessionMessageRow,
    AgentSessionRow,
)
from agent_runtime.sessions.state import (
    PersistedSessionMessage,
    SessionMessageDraft,
    SessionMessageVisibility,
    SessionState,
)


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SessionRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        # Historical migration tests open a populated pre-task schema with the
        # current repository before upgrading. Keep those writes compatible.
        bind = session_factory.kw.get("bind")
        self._has_task_columns = bind is None or "task_id" in {
            column["name"] for column in inspect(bind).get_columns("agent_sessions")}

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def create(
        self,
        state: SessionState,
        event: SessionEvent,
        *,
        messages: Sequence[SessionMessageDraft] = (),
    ) -> SessionState:
        if state.session_id != event.session_id:
            raise ValueError("State and event session IDs do not match.")
        if event.event_type != SessionEventType.SESSION_CREATED:
            raise ValueError("Initial event must be SESSION_CREATED.")
        if state.version or state.event_sequence or state.message_sequence:
            raise ValueError("Initial session counters must be zero.")
        now = datetime.now(UTC)
        try:
            with self._session_factory.begin() as session:
                if self._has_task_columns:
                    row = self._row_from_state(state)
                    session.add(row)
                    session.flush()
                else:
                    row = None
                    session.execute(insert(AgentSessionRow.__table__).values(
                        **self._projection_values(state)))
                message_sequence = self._insert_messages(
                    session,
                    state.session_id,
                    messages,
                    current_sequence=0,
                    now=now,
                )
                persisted_event = self._assigned_event(event, sequence=1, now=now)
                persisted = self._updated_state(
                    state,
                    {
                        "event_sequence": 1,
                        "message_sequence": message_sequence,
                        "created_at": ensure_utc(state.created_at),
                        "updated_at": now,
                    },
                )
                if row is not None:
                    self._apply_state(row, persisted)
                else:
                    session.execute(update(AgentSessionRow.__table__).where(
                        AgentSessionRow.__table__.c.session_id == state.session_id).values(
                        **self._projection_values(persisted)))
                session.add(self._event_row(persisted_event))
        except IntegrityError as exc:
            if self.get(state.session_id) is not None:
                raise SessionAlreadyExistsError(
                    f"Session '{state.session_id}' already exists."
                ) from exc
            raise
        return persisted

    def get(self, session_id: str) -> SessionState | None:
        with self._session_factory() as session:
            row = session.get(AgentSessionRow, session_id)
            return None if row is None else SessionState.model_validate_json(row.state_json)

    def require(self, session_id: str) -> SessionState:
        state = self.get(session_id)
        if state is None:
            raise SessionNotFoundError(f"Session '{session_id}' was not found.")
        return state

    def list_recent(self, *, limit: int = 25) -> list[SessionState]:
        if not 1 <= limit <= 100:
            raise ValueError("Session history limit must be between 1 and 100.")
        with self._session_factory() as session:
            rows = session.scalars(
                select(AgentSessionRow)
                .where(AgentSessionRow.archived_at.is_(None))
                .order_by(AgentSessionRow.updated_at.desc(), AgentSessionRow.session_id)
                .limit(limit)
            ).all()
            return [SessionState.model_validate_json(row.state_json) for row in rows]

    def is_archived(self, session_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(AgentSessionRow, session_id)
            return row is not None and row.archived_at is not None

    def archive(self, session_id: str, *, expected_version: int) -> None:
        """Hide a local conversation while retaining its audit records."""
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.version == expected_version,
                    AgentSessionRow.archived_at.is_(None),
                )
                .values(archived_at=now)
            )
            if changed.rowcount != 1:
                row = session.get(AgentSessionRow, session_id)
                if row is None or row.archived_at is not None:
                    raise SessionNotFoundError(f"Session '{session_id}' was not found.")
                raise StaleSessionError(
                    f"Session '{session_id}' has a stale state version."
                )

    def save_transition(
        self,
        state: SessionState,
        event: SessionEvent,
        *,
        expected_version: int,
        messages: Sequence[SessionMessageDraft] = (),
    ) -> SessionState:
        if state.session_id != event.session_id:
            raise ValueError("State and event session IDs do not match.")
        if state.version != expected_version:
            raise StaleSessionError(
                f"Session '{state.session_id}' has a stale state version."
            )
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.get(AgentSessionRow, state.session_id)
            if row is None:
                raise SessionNotFoundError(f"Session '{state.session_id}' was not found.")
            if row.version != expected_version:
                raise StaleSessionError(
                    f"Session '{state.session_id}' has a stale state version."
                )
            current = SessionState.model_validate_json(row.state_json)
            if current.terminal:
                raise SessionTerminalError("A terminal session cannot continue.")
            message_sequence = self._insert_messages(
                session,
                state.session_id,
                messages,
                current_sequence=current.message_sequence,
                now=now,
            )
            next_event_sequence = current.event_sequence + 1
            persisted_event = self._assigned_event(
                event, sequence=next_event_sequence, now=now
            )
            persisted = self._updated_state(
                state,
                {
                    "version": expected_version + 1,
                    "event_sequence": next_event_sequence,
                    "message_sequence": message_sequence,
                    "created_at": current.created_at,
                    "updated_at": now,
                },
            )
            values = self._projection_values(persisted)
            changed = session.execute(
                update(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == state.session_id,
                    AgentSessionRow.version == expected_version,
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise StaleSessionError(
                    f"Session '{state.session_id}' has a stale state version."
                )
            session.add(self._event_row(persisted_event))
            session.flush()
        return persisted

    def messages(
        self,
        session_id: str,
        *,
        visibility: SessionMessageVisibility | None = None,
        task_id: str | None = None,
    ) -> list[PersistedSessionMessage]:
        query = select(AgentSessionMessageRow).where(
            AgentSessionMessageRow.session_id == session_id
        )
        if visibility == SessionMessageVisibility.TASK_PRIVATE:
            if not task_id:
                raise ValueError("Task-private message reads require a task ID.")
            query = query.where(
                AgentSessionMessageRow.visibility == visibility.value,
                AgentSessionMessageRow.task_id == task_id,
            )
        elif visibility is not None:
            query = query.where(AgentSessionMessageRow.visibility == visibility.value)
        elif task_id is not None:
            query = query.where(
                or_(
                    AgentSessionMessageRow.visibility
                    != SessionMessageVisibility.TASK_PRIVATE.value,
                    AgentSessionMessageRow.task_id == task_id,
                )
            )
        else:
            query = query.where(
                AgentSessionMessageRow.visibility
                != SessionMessageVisibility.TASK_PRIVATE.value
            )
        with self._session_factory() as session:
            rows = session.scalars(query.order_by(AgentSessionMessageRow.sequence)).all()
            return [self._message(row) for row in rows]

    def message(self, message_id: str) -> PersistedSessionMessage | None:
        with self._session_factory() as session:
            row = session.get(AgentSessionMessageRow, message_id)
            return None if row is None else self._message(row)

    def events(self, session_id: str) -> list[SessionEvent]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AgentSessionEventRow)
                .where(AgentSessionEventRow.session_id == session_id)
                .order_by(AgentSessionEventRow.sequence)
            ).all()
            return [
                SessionEvent(
                    event_id=row.event_id,
                    session_id=row.session_id,
                    sequence=row.sequence,
                    event_type=row.event_type,
                    payload=json.loads(row.payload_json),
                    occurred_at=ensure_utc(row.occurred_at),
                )
                for row in rows
            ]

    @staticmethod
    def _message_hash(message_json: str) -> str:
        return hashlib.sha256(message_json.encode("utf-8")).hexdigest()

    def _insert_messages(
        self,
        session: Session,
        session_id: str,
        drafts: Sequence[SessionMessageDraft],
        *,
        current_sequence: int,
        now: datetime,
    ) -> int:
        sequence = current_sequence
        seen: dict[str, str] = {}
        for draft in drafts:
            message_json = canonical_json(draft.message.model_dump(mode="json"))
            content_hash = self._message_hash(message_json)
            prior_hash = seen.get(draft.message.message_id)
            if prior_hash is not None:
                if prior_hash != content_hash:
                    raise MessageConflictError(
                        "The message ID was reused with different content."
                    )
                continue
            seen[draft.message.message_id] = content_hash
            existing = session.get(AgentSessionMessageRow, draft.message.message_id)
            if existing is not None:
                if existing.session_id != session_id or existing.content_hash != content_hash:
                    raise MessageConflictError(
                        "The message ID was reused with different content."
                    )
                continue
            sequence += 1
            session.add(
                AgentSessionMessageRow(
                    message_id=draft.message.message_id,
                    session_id=session_id,
                    sequence=sequence,
                    task_id=draft.task_id,
                    visibility=draft.visibility.value,
                    message_json=message_json,
                    content_hash=content_hash,
                    created_at=now,
                )
            )
        return sequence

    @staticmethod
    def _assigned_event(
        event: SessionEvent, *, sequence: int, now: datetime
    ) -> SessionEvent:
        return SessionEvent.model_validate(
            {
                **event.model_dump(mode="python"),
                "sequence": sequence,
                "occurred_at": now,
                "payload": redact_sensitive(event.payload),
            }
        )

    @staticmethod
    def _updated_state(state: SessionState, updates: dict) -> SessionState:
        return SessionState.model_validate(
            {**state.model_dump(mode="python"), **updates}
        )

    def _row_from_state(self, state: SessionState) -> AgentSessionRow:
        return AgentSessionRow(**self._projection_values(state))

    def _apply_state(self, row: AgentSessionRow, state: SessionState) -> None:
        for key, value in self._projection_values(state).items():
            setattr(row, key, value)

    def _projection_values(self, state: SessionState) -> dict:
        values = {
            "session_id": state.session_id,
            "schema_version": state.schema_version,
            "user_id": state.user_id,
            "title": state.title,
            "status": state.status.value,
            "version": state.version,
            "event_sequence": state.event_sequence,
            "message_sequence": state.message_sequence,
            "active_run_id": state.active_run_id,
            "parent_session_id": state.parent_session_id,
            "task_id": state.task_id,
            "agent_role": state.agent_role,
            "pending_assistant_message_id": state.pending_assistant_message_id,
            "pending_tool_call_ids_json": canonical_json(state.pending_tool_call_ids),
            "allowed_tools_json": canonical_json(sorted(state.allowed_tools)),
            "loop_iteration": state.loop_iteration,
            "executed_tool_calls": state.executed_tool_calls,
            "max_loop_iterations": state.max_loop_iterations,
            "max_tool_calls": state.max_tool_calls,
            "total_input_tokens": state.total_input_tokens,
            "total_output_tokens": state.total_output_tokens,
            "error_code": state.error_code,
            "error_message": state.error_message,
            "cancel_requested": state.cancel_requested,
            "cancel_requested_at": state.cancel_requested_at,
            "cancel_reason": state.cancel_reason,
            "turn_deadline_at": state.turn_deadline_at,
            "session_expires_at": state.session_expires_at,
            "terminal_reason": state.terminal_reason,
            "manual_recovery_tool_call_ids_json": canonical_json(
                state.manual_recovery_tool_call_ids
            ),
            "allowed_skills_json": canonical_json(sorted(state.allowed_skills)),
            "current_context_snapshot_id": state.current_context_snapshot_id,
            "last_context_snapshot_id": state.last_context_snapshot_id,
            "state_json": state.model_dump_json(),
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }
        if not self._has_task_columns:
            for key in ("parent_session_id", "task_id", "agent_role"):
                values.pop(key)
        return values

    @staticmethod
    def _event_row(event: SessionEvent) -> AgentSessionEventRow:
        return AgentSessionEventRow(
            event_id=event.event_id,
            session_id=event.session_id,
            sequence=event.sequence,
            event_type=event.event_type.value,
            payload_json=canonical_json(event.payload),
            occurred_at=event.occurred_at,
        )

    @staticmethod
    def _message(row: AgentSessionMessageRow) -> PersistedSessionMessage:
        return PersistedSessionMessage(
            message_id=row.message_id,
            session_id=row.session_id,
            sequence=row.sequence,
            task_id=row.task_id,
            visibility=row.visibility,
            message=json.loads(row.message_json),
            content_hash=row.content_hash,
            created_at=ensure_utc(row.created_at),
        )

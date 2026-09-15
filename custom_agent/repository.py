"""Transactional SQLite/SQLAlchemy state repository for the custom loop."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from api.models import Run
from custom_agent.errors import StaleStateError, StateNotFoundError
from custom_agent.events import AgentEvent
from custom_agent.models import CustomAgentEventRow, CustomAgentStateRow
from custom_agent.state import AgentState, AgentStatus


def projection_status(status: AgentStatus) -> str:
    if status == AgentStatus.AWAITING_REVIEW:
        return "awaiting_review"
    if status == AgentStatus.REVISING:
        return "revising"
    if status == AgentStatus.APPROVED:
        return "approved"
    if status == AgentStatus.FAILED:
        return "failed"
    return "running"


class StateRepository:
    """Persist state, event, and public run projection in one transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        state: AgentState,
        event: AgentEvent,
        *,
        result: dict | None = None,
    ) -> AgentState:
        if state.version != 0 or state.event_sequence != 0 or event.sequence != 1:
            raise ValueError("Initial state and event versions are invalid.")
        persisted = state.model_copy(
            update={"event_sequence": 1, "updated_at": datetime.now(UTC)}
        )
        with self._session_factory.begin() as session:
            session.add(
                Run(
                    run_id=state.run_id,
                    thread_id=state.run_id,
                    status=projection_status(state.status),
                    resume_text=state.resume_text,
                    job_description=state.job_description,
                    result_json=self._encode(result),
                    error_message=state.error_message,
                )
            )
            session.add(self._state_row(persisted))
            session.add(self._event_row(event))
        return persisted

    def get(self, run_id: str) -> AgentState | None:
        with self._session_factory() as session:
            row = session.get(CustomAgentStateRow, run_id)
            if row is None:
                return None
            return AgentState.model_validate_json(row.state_json)

    def require(self, run_id: str) -> AgentState:
        state = self.get(run_id)
        if state is None:
            raise StateNotFoundError(f"Custom agent run '{run_id}' was not found.")
        return state

    def save(
        self,
        state: AgentState,
        event: AgentEvent,
        *,
        expected_version: int,
        result: dict | None,
    ) -> AgentState:
        if state.run_id != event.run_id:
            raise ValueError("State and event run IDs do not match.")
        if state.version != expected_version:
            raise StaleStateError(
                f"Custom agent run '{state.run_id}' has a stale state version."
            )
        if event.sequence != state.event_sequence + 1:
            raise ValueError("Event sequence does not follow the current state.")
        now = datetime.now(UTC)
        persisted = state.model_copy(
            update={
                "version": expected_version + 1,
                "event_sequence": event.sequence,
                "updated_at": now,
            }
        )
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(CustomAgentStateRow)
                .where(
                    CustomAgentStateRow.run_id == state.run_id,
                    CustomAgentStateRow.version == expected_version,
                )
                .values(
                    state_json=persisted.model_dump_json(),
                    step=persisted.step.value,
                    status=persisted.status.value,
                    version=persisted.version,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                exists = session.scalar(
                    select(CustomAgentStateRow.run_id).where(
                        CustomAgentStateRow.run_id == state.run_id
                    )
                )
                if exists is None:
                    raise StateNotFoundError(
                        f"Custom agent run '{state.run_id}' was not found."
                    )
                raise StaleStateError(
                    f"Custom agent run '{state.run_id}' has a stale state version."
                )
            session.add(self._event_row(event))
            projected = session.execute(
                update(Run)
                .where(Run.run_id == state.run_id)
                .values(
                    status=projection_status(persisted.status),
                    result_json=self._encode(result),
                    error_message=persisted.error_message,
                    updated_at=now,
                )
            )
            if projected.rowcount != 1:
                raise StateNotFoundError(
                    f"Run projection '{state.run_id}' was not found."
                )
        return persisted

    def events(self, run_id: str) -> list[AgentEvent]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(CustomAgentEventRow)
                .where(CustomAgentEventRow.run_id == run_id)
                .order_by(CustomAgentEventRow.sequence)
            ).all()
            return [
                AgentEvent(
                    event_id=row.event_id,
                    run_id=row.run_id,
                    sequence=row.sequence,
                    event_type=row.event_type,
                    step=row.step,
                    payload=json.loads(row.payload_json),
                    occurred_at=row.occurred_at,
                )
                for row in rows
            ]

    @staticmethod
    def _encode(value: dict | None) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _state_row(state: AgentState) -> CustomAgentStateRow:
        return CustomAgentStateRow(
            run_id=state.run_id,
            state_json=state.model_dump_json(),
            step=state.step.value,
            status=state.status.value,
            version=state.version,
            updated_at=state.updated_at,
        )

    @staticmethod
    def _event_row(event: AgentEvent) -> CustomAgentEventRow:
        return CustomAgentEventRow(
            event_id=event.event_id,
            run_id=event.run_id,
            sequence=event.sequence,
            event_type=event.event_type.value,
            step=event.step.value,
            payload_json=json.dumps(
                event.payload, ensure_ascii=False, separators=(",", ":")
            ),
            occurred_at=event.occurred_at,
        )

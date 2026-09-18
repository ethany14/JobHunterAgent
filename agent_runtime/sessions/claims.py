"""Atomic session execution claims and attempt history."""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.sessions.clock import Clock, SystemClock
from agent_runtime.sessions.errors import (
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
    SessionNotFoundError,
)
from agent_runtime.sessions.models import AgentSessionAttemptRow, AgentSessionRow
from agent_runtime.types import RuntimeModel


def ensure_utc(value: datetime) -> datetime:
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SessionAttemptStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class SessionExecutionAttempt(RuntimeModel):
    attempt_id: str
    session_id: str
    worker_id: str
    status: SessionAttemptStatus
    lease_until: datetime
    last_heartbeat_at: datetime
    acquired_at: datetime
    released_at: datetime | None = None
    recovered_from_attempt_id: str | None = None


class SessionClaimRepository:
    def __init__(
        self, session_factory: sessionmaker[Session], *, clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    def claim(
        self, session_id: str, worker_id: str, *, lease_seconds: float
    ) -> SessionExecutionAttempt:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        attempt_id = str(uuid4())
        with self._session_factory.begin() as session:
            projection = session.execute(
                select(
                    AgentSessionRow.active_attempt_id,
                    AgentSessionRow.lease_until,
                ).where(AgentSessionRow.session_id == session_id)
            ).one_or_none()
            if projection is None:
                raise SessionNotFoundError(f"Session '{session_id}' was not found.")
            prior_id, prior_lease = projection
            prior_lease = ensure_utc(prior_lease) if prior_lease else None
            owner_matches = (
                AgentSessionRow.active_attempt_id.is_(None)
                if prior_id is None
                else AgentSessionRow.active_attempt_id == prior_id
            )
            changed = session.execute(
                update(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == session_id,
                    owner_matches,
                    or_(
                        AgentSessionRow.active_attempt_id.is_(None),
                        AgentSessionRow.lease_until <= now,
                    ),
                )
                .values(active_attempt_id=attempt_id, lease_until=lease_until)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise SessionClaimConflictError(
                    "The session already has an active execution lease."
                )
            if prior_id is not None:
                session.execute(
                    update(AgentSessionAttemptRow)
                    .where(
                        AgentSessionAttemptRow.attempt_id == prior_id,
                        AgentSessionAttemptRow.status
                        == SessionAttemptStatus.ACTIVE.value,
                    )
                    .values(
                        status=SessionAttemptStatus.EXPIRED.value,
                        released_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            row = AgentSessionAttemptRow(
                attempt_id=attempt_id,
                session_id=session_id,
                worker_id=worker_id,
                status=SessionAttemptStatus.ACTIVE.value,
                lease_until=lease_until,
                last_heartbeat_at=now,
                acquired_at=now,
                recovered_from_attempt_id=prior_id,
            )
            session.add(row)
            session.flush()
            return self._record(row)

    def heartbeat(
        self, attempt_id: str, worker_id: str, *, lease_seconds: float
    ) -> SessionExecutionAttempt:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._session_factory.begin() as session:
            attempt = session.get(AgentSessionAttemptRow, attempt_id)
            if attempt is None or attempt.worker_id != worker_id:
                raise SessionClaimNotOwnedError("The execution attempt is not owned.")
            changed = session.execute(
                update(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == attempt.session_id,
                    AgentSessionRow.active_attempt_id == attempt_id,
                    AgentSessionRow.lease_until > now,
                )
                .values(lease_until=lease_until)
                .execution_options(synchronize_session=False)
            )
            attempt_changed = session.execute(
                update(AgentSessionAttemptRow)
                .where(
                    AgentSessionAttemptRow.attempt_id == attempt_id,
                    AgentSessionAttemptRow.worker_id == worker_id,
                    AgentSessionAttemptRow.status == SessionAttemptStatus.ACTIVE.value,
                    AgentSessionAttemptRow.lease_until > now,
                )
                .values(lease_until=lease_until, last_heartbeat_at=now)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1 or attempt_changed.rowcount != 1:
                raise SessionClaimNotOwnedError(
                    "The execution lease is expired or no longer owned."
                )
            session.flush()
            session.refresh(attempt)
            return self._record(attempt)

    def release(self, attempt_id: str, worker_id: str) -> SessionExecutionAttempt:
        now = self._clock.now()
        with self._session_factory.begin() as session:
            attempt = session.get(AgentSessionAttemptRow, attempt_id)
            if attempt is None or attempt.worker_id != worker_id:
                raise SessionClaimNotOwnedError("The execution attempt is not owned.")
            changed = session.execute(
                update(AgentSessionRow)
                .where(
                    AgentSessionRow.session_id == attempt.session_id,
                    AgentSessionRow.active_attempt_id == attempt_id,
                    AgentSessionRow.lease_until > now,
                )
                .values(active_attempt_id=None, lease_until=None)
                .execution_options(synchronize_session=False)
            )
            attempt_changed = session.execute(
                update(AgentSessionAttemptRow)
                .where(
                    AgentSessionAttemptRow.attempt_id == attempt_id,
                    AgentSessionAttemptRow.worker_id == worker_id,
                    AgentSessionAttemptRow.status == SessionAttemptStatus.ACTIVE.value,
                )
                .values(status=SessionAttemptStatus.RELEASED.value, released_at=now)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1 or attempt_changed.rowcount != 1:
                raise SessionClaimNotOwnedError(
                    "The execution lease is expired or no longer owned."
                )
            session.flush()
            session.refresh(attempt)
            return self._record(attempt)

    def active_claim(self, session_id: str) -> SessionExecutionAttempt | None:
        now = self._clock.now()
        with self._session_factory() as session:
            row = session.scalar(
                select(AgentSessionAttemptRow)
                .join(
                    AgentSessionRow,
                    AgentSessionRow.active_attempt_id
                    == AgentSessionAttemptRow.attempt_id,
                )
                .where(
                    AgentSessionRow.session_id == session_id,
                    AgentSessionRow.lease_until > now,
                    AgentSessionAttemptRow.status == SessionAttemptStatus.ACTIVE.value,
                )
            )
            return None if row is None else self._record(row)

    def history(self, session_id: str) -> list[SessionExecutionAttempt]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AgentSessionAttemptRow)
                .where(AgentSessionAttemptRow.session_id == session_id)
                .order_by(AgentSessionAttemptRow.acquired_at, AgentSessionAttemptRow.attempt_id)
            ).all()
            return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: AgentSessionAttemptRow) -> SessionExecutionAttempt:
        return SessionExecutionAttempt(
            attempt_id=row.attempt_id,
            session_id=row.session_id,
            worker_id=row.worker_id,
            status=row.status,
            lease_until=ensure_utc(row.lease_until),
            last_heartbeat_at=ensure_utc(row.last_heartbeat_at),
            acquired_at=ensure_utc(row.acquired_at),
            released_at=ensure_utc(row.released_at) if row.released_at else None,
            recovered_from_attempt_id=row.recovered_from_attempt_id,
        )

"""Transactional persistence for immutable context snapshots and usage audit."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.context.models import (
    ContextSnapshotEventRow,
    ContextSnapshotRow,
    MemoryUsageEventRow,
    SkillUsageEventRow,
)
from agent_runtime.context.snapshots import ContextSnapshot, ContextSnapshotStatus, ContextSnapshotUnavailableError
from agent_runtime.memory.models import MemoryItemRow
from agent_runtime.memory.types import MemoryItem
from agent_runtime.security import canonical_json
from agent_runtime.sessions.clock import Clock, SystemClock


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ContextSnapshotRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, clock: Clock | None = None) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    def prepare(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        if snapshot.status != ContextSnapshotStatus.PREPARED or snapshot.version != 0:
            raise ValueError("A new context snapshot must be prepared at version zero.")
        with self._session_factory.begin() as session:
            session.add(ContextSnapshotRow(
                snapshot_id=snapshot.snapshot_id, session_id=snapshot.session_id,
                status=snapshot.status.value, context_hash=snapshot.context_hash,
                manifest_json=snapshot.model_dump_json(), version=0,
                prepared_at=snapshot.prepared_at, used_at=None, abandoned_at=None,
            ))
            session.flush()
            session.add(self._event(snapshot.snapshot_id, 1, "prepared", snapshot.prepared_at))
        return snapshot

    def get(self, snapshot_id: str) -> ContextSnapshot | None:
        with self._session_factory() as session:
            row = session.get(ContextSnapshotRow, snapshot_id)
            return None if row is None else ContextSnapshot.model_validate_json(row.manifest_json)

    def require(self, snapshot_id: str) -> ContextSnapshot:
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise ContextSnapshotUnavailableError("Context snapshot is unavailable.")
        return snapshot

    def mark_used(self, snapshot_id: str) -> ContextSnapshot:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row = session.get(ContextSnapshotRow, snapshot_id)
            if row is None:
                raise ContextSnapshotUnavailableError("Context snapshot is unavailable.")
            snapshot = ContextSnapshot.model_validate_json(row.manifest_json)
            if snapshot.status == ContextSnapshotStatus.USED:
                return snapshot
            if snapshot.status != ContextSnapshotStatus.PREPARED:
                raise ContextSnapshotUnavailableError("Context snapshot is not prepared.")
            used = ContextSnapshot.model_validate({
                **snapshot.model_dump(mode="python"), "status": ContextSnapshotStatus.USED,
                "used_at": now, "version": snapshot.version + 1,
            })
            changed = session.execute(update(ContextSnapshotRow).where(
                ContextSnapshotRow.snapshot_id == snapshot_id,
                ContextSnapshotRow.version == snapshot.version,
            ).values(status=used.status.value, manifest_json=used.model_dump_json(),
                     version=used.version, used_at=now))
            if changed.rowcount != 1:
                raise ContextSnapshotUnavailableError("Context snapshot changed concurrently.")
            session.add(self._event(snapshot_id, 2, "used", now))
            for reference in used.memory_versions:
                session.add(MemoryUsageEventRow(
                    usage_id=str(uuid4()), memory_id=reference.memory_id,
                    snapshot_id=snapshot_id, memory_version=reference.version, used_at=now,
                ))
                memory_row = session.get(MemoryItemRow, reference.memory_id)
                if memory_row is not None:
                    memory = MemoryItem.model_validate_json(memory_row.item_json)
                    updated_memory = MemoryItem.model_validate({
                        **memory.model_dump(mode="python"), "last_used_at": now,
                    })
                    session.execute(update(MemoryItemRow).where(
                        MemoryItemRow.memory_id == reference.memory_id
                    ).values(last_used_at=now, item_json=updated_memory.model_dump_json()))
            for reference in used.skill_versions:
                session.add(SkillUsageEventRow(
                    usage_id=str(uuid4()), version_id=reference.version_id,
                    snapshot_id=snapshot_id, used_at=now,
                ))
            session.flush()
        return used

    def abandon(self, snapshot_id: str, *, reason: str) -> ContextSnapshot:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row = session.get(ContextSnapshotRow, snapshot_id)
            if row is None:
                raise ContextSnapshotUnavailableError("Context snapshot is unavailable.")
            snapshot = ContextSnapshot.model_validate_json(row.manifest_json)
            if snapshot.status != ContextSnapshotStatus.PREPARED:
                return snapshot
            abandoned = ContextSnapshot.model_validate({
                **snapshot.model_dump(mode="python"), "status": ContextSnapshotStatus.ABANDONED,
                "abandoned_at": now, "abandon_reason": reason[:128],
                "version": snapshot.version + 1,
            })
            changed = session.execute(update(ContextSnapshotRow).where(
                ContextSnapshotRow.snapshot_id == snapshot_id,
                ContextSnapshotRow.version == snapshot.version,
            ).values(status=abandoned.status.value, manifest_json=abandoned.model_dump_json(),
                     version=abandoned.version, abandoned_at=now))
            if changed.rowcount != 1:
                raise ContextSnapshotUnavailableError("Context snapshot changed concurrently.")
            session.add(self._event(snapshot_id, 2, "abandoned", now,
                                    {"reason": reason[:128]}))
        return abandoned

    @staticmethod
    def _event(snapshot_id: str, sequence: int, event_type: str, occurred_at: datetime,
               payload: dict | None = None) -> ContextSnapshotEventRow:
        return ContextSnapshotEventRow(
            event_id=str(uuid4()), snapshot_id=snapshot_id, sequence=sequence,
            event_type=event_type, payload_json=canonical_json(payload or {}),
            occurred_at=occurred_at,
        )

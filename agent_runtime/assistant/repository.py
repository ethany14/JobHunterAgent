"""Stable, idempotent Assistant activity persistence."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.assistant.models import AssistantActivityRow
from agent_runtime.assistant.types import AssistantActivity, AssistantActivityType
from agent_runtime.security import canonical_json, redact_sensitive


class AssistantTimelineRepository:
    def __init__(self, factory: sessionmaker[Session]):
        self._factory = factory

    def project(self, *, session_id: str, activity_type: AssistantActivityType,
                status: str, reference_type: str, reference_id: str,
                payload: dict | None = None) -> AssistantActivity:
        safe_payload = redact_sensitive(payload or {})
        now = datetime.now(UTC)
        with self._factory.begin() as db:
            row = db.scalar(select(AssistantActivityRow).where(
                AssistantActivityRow.session_id == session_id,
                AssistantActivityRow.reference_type == reference_type,
                AssistantActivityRow.reference_id == reference_id))
            if row is None:
                sequence = int(db.scalar(select(func.coalesce(
                    func.max(AssistantActivityRow.sequence), 0)).where(
                    AssistantActivityRow.session_id == session_id)) or 0) + 1
                row = AssistantActivityRow(activity_id=str(uuid4()), session_id=session_id,
                    sequence=sequence, activity_type=activity_type.value, status=status[:64],
                    reference_type=reference_type[:64], reference_id=reference_id[:128],
                    payload_json=canonical_json(safe_payload), created_at=now, updated_at=now)
                db.add(row)
            else:
                row.activity_type = activity_type.value
                row.status = status[:64]
                row.payload_json = canonical_json(safe_payload)
                row.updated_at = now
            db.flush()
            return self._activity(row)

    def list(self, session_id: str, *, after_sequence: int = 0,
             limit: int = 100) -> list[AssistantActivity]:
        with self._factory() as db:
            rows = db.scalars(select(AssistantActivityRow).where(
                AssistantActivityRow.session_id == session_id,
                AssistantActivityRow.sequence > after_sequence).order_by(
                AssistantActivityRow.sequence).limit(limit)).all()
            return [self._activity(row) for row in rows]

    @staticmethod
    def _activity(row: AssistantActivityRow) -> AssistantActivity:
        return AssistantActivity(activity_id=row.activity_id, session_id=row.session_id,
            sequence=row.sequence, type=row.activity_type, status=row.status,
            reference_type=row.reference_type, reference_id=row.reference_id,
            payload=json.loads(row.payload_json), created_at=row.created_at,
            updated_at=row.updated_at)

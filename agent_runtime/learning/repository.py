"""Transactional storage and deterministic aggregation of conversation experience."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.feedback.policy import canonical_key
from agent_runtime.learning.errors import LearningExperienceConflictError
from agent_runtime.learning.models import (
    ConversationExperienceEventRow,
    ConversationExperienceRow,
    ConversationLearningPatternRow,
    ConversationPatternExperienceRow,
)
from agent_runtime.learning.types import (
    ConversationExperience,
    ExperienceSignal,
    ExperienceSignalType,
    LearningObservation,
    LearningPattern,
    LearningPatternStatus,
)
from agent_runtime.security import canonical_json, redact_sensitive


class ConversationLearningRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    @staticmethod
    def source_hash(user_message_id: str, assistant_message_id: str) -> str:
        return hashlib.sha256(
            f"{user_message_id}\0{assistant_message_id}".encode("utf-8")
        ).hexdigest()

    def record(
        self,
        *,
        owner_id: str,
        session_id: str,
        user_message_id: str,
        assistant_message_id: str,
        observation: LearningObservation | None,
        application_id: str | None = None,
        error_code: str | None = None,
    ) -> tuple[ConversationExperience, bool]:
        digest = self.source_hash(user_message_id, assistant_message_id)
        with self._factory.begin() as db:
            existing = db.scalar(select(ConversationExperienceRow).where(
                ConversationExperienceRow.session_id == session_id,
                ConversationExperienceRow.user_message_id == user_message_id,
                ConversationExperienceRow.assistant_message_id == assistant_message_id,
            ))
            if existing is not None:
                item = self._experience(existing)
                if item.owner_id != owner_id or item.source_hash != digest:
                    raise LearningExperienceConflictError(
                        "The conversation turn was already observed with different identity."
                    )
                return item, False
            now = datetime.now(UTC)
            item = ConversationExperience(
                owner_id=owner_id,
                session_id=session_id,
                application_id=application_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                source_hash=digest,
                observation=observation,
                status="failed" if error_code else "observed",
                error_code=error_code,
                created_at=now,
            )
            db.add(ConversationExperienceRow(
                experience_id=item.experience_id,
                owner_id=item.owner_id,
                session_id=item.session_id,
                application_id=item.application_id,
                user_message_id=item.user_message_id,
                assistant_message_id=item.assistant_message_id,
                source_hash=item.source_hash,
                observation_json=(
                    item.observation.model_dump_json() if item.observation else None
                ),
                status=item.status,
                error_code=item.error_code,
                created_at=now,
            ))
            db.flush()
            self._event(
                db,
                item.experience_id,
                "observation_failed" if error_code else "turn_observed",
                {"error_code": error_code} if error_code else {
                    "signal_count": len(observation.signals) if observation else 0
                },
                now,
            )
            return item, True

    def experience(self, experience_id: str) -> ConversationExperience | None:
        with self._factory() as db:
            row = db.get(ConversationExperienceRow, experience_id)
            return None if row is None else self._experience(row)

    def for_turn(
        self, session_id: str, user_message_id: str, assistant_message_id: str
    ) -> ConversationExperience | None:
        with self._factory() as db:
            row = db.scalar(select(ConversationExperienceRow).where(
                ConversationExperienceRow.session_id == session_id,
                ConversationExperienceRow.user_message_id == user_message_id,
                ConversationExperienceRow.assistant_message_id == assistant_message_id,
            ))
            return None if row is None else self._experience(row)

    def experiences(self, *, owner_id: str, limit: int = 100) -> list[ConversationExperience]:
        with self._factory() as db:
            rows = db.scalars(select(ConversationExperienceRow).where(
                ConversationExperienceRow.owner_id == owner_id
            ).order_by(ConversationExperienceRow.created_at.desc()).limit(limit)).all()
            return [self._experience(row) for row in rows]

    def record_event(self, experience_id: str, event_type: str, payload: dict) -> None:
        with self._factory.begin() as db:
            if db.get(ConversationExperienceRow, experience_id) is None:
                raise LearningExperienceConflictError("Conversation experience was not found.")
            self._event(db, experience_id, event_type, payload, datetime.now(UTC))

    def aggregate_procedural_signal(
        self,
        experience: ConversationExperience,
        signal: ExperienceSignal,
        *,
        minimum_occurrences: int = 3,
        minimum_sessions: int = 2,
    ) -> LearningPattern:
        if signal.signal_type not in {
            ExperienceSignalType.CORRECTION,
            ExperienceSignalType.PROCEDURAL_FAILURE,
            ExperienceSignalType.PROCEDURAL_SUCCESS,
        } or not signal.reusable_across_sessions:
            raise ValueError("Only reusable procedural observations may be aggregated.")
        key = canonical_key(signal.canonical_key or signal.summary)
        with self._factory.begin() as db:
            row = db.scalar(select(ConversationLearningPatternRow).where(
                ConversationLearningPatternRow.owner_id == experience.owner_id,
                ConversationLearningPatternRow.canonical_key == key,
            ))
            now = datetime.now(UTC)
            if row is None:
                pattern = LearningPattern(
                    owner_id=experience.owner_id,
                    canonical_key=key,
                    proposed_instruction=signal.summary,
                    experience_ids=[experience.experience_id],
                    session_ids=[experience.session_id],
                    positive_count=(1 if signal.signal_type == ExperienceSignalType.PROCEDURAL_SUCCESS else 0),
                    negative_count=(0 if signal.signal_type == ExperienceSignalType.PROCEDURAL_SUCCESS else 1),
                    created_at=now,
                    updated_at=now,
                )
                row = ConversationLearningPatternRow(
                    pattern_id=pattern.pattern_id,
                    owner_id=pattern.owner_id,
                    canonical_key=key,
                    status=pattern.status.value,
                    state_json=pattern.model_dump_json(),
                    version=pattern.version,
                    emitted_feedback_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                db.add(ConversationPatternExperienceRow(
                    pattern_id=pattern.pattern_id,
                    experience_id=experience.experience_id,
                    linked_at=now,
                ))
            else:
                pattern = LearningPattern.model_validate_json(row.state_json)
                if experience.experience_id in pattern.experience_ids:
                    return pattern
                next_experiences = [*pattern.experience_ids, experience.experience_id]
                next_sessions = list(dict.fromkeys([*pattern.session_ids, experience.session_id]))
                status = pattern.status
                if (
                    len(next_experiences) >= minimum_occurrences
                    and len(next_sessions) >= minimum_sessions
                    and status == LearningPatternStatus.COLLECTING
                ):
                    status = LearningPatternStatus.READY_FOR_REVIEW
                pattern = LearningPattern.model_validate({
                    **pattern.model_dump(mode="python"),
                    "experience_ids": next_experiences,
                    "session_ids": next_sessions,
                    "positive_count": pattern.positive_count + (
                        1 if signal.signal_type == ExperienceSignalType.PROCEDURAL_SUCCESS else 0
                    ),
                    "negative_count": pattern.negative_count + (
                        0 if signal.signal_type == ExperienceSignalType.PROCEDURAL_SUCCESS else 1
                    ),
                    "status": status,
                    "version": pattern.version + 1,
                    "updated_at": now,
                })
                changed = db.execute(update(ConversationLearningPatternRow).where(
                    ConversationLearningPatternRow.pattern_id == row.pattern_id,
                    ConversationLearningPatternRow.version == row.version,
                ).values(
                    status=pattern.status.value,
                    state_json=pattern.model_dump_json(),
                    version=pattern.version,
                    updated_at=now,
                ))
                if changed.rowcount != 1:
                    raise LearningExperienceConflictError(
                        "The learning pattern changed during aggregation."
                    )
                db.add(ConversationPatternExperienceRow(
                    pattern_id=pattern.pattern_id,
                    experience_id=experience.experience_id,
                    linked_at=now,
                ))
            self._event(db, experience.experience_id, "pattern_aggregated", {
                "pattern_id": pattern.pattern_id,
                "canonical_key": pattern.canonical_key,
                "pattern_status": pattern.status.value,
            }, now)
            return pattern

    def mark_candidate_emitted(
        self, pattern_id: str, *, expected_version: int, feedback_event_id: str
    ) -> LearningPattern:
        with self._factory.begin() as db:
            row = db.get(ConversationLearningPatternRow, pattern_id)
            if row is None:
                raise LearningExperienceConflictError("Learning pattern was not found.")
            current = LearningPattern.model_validate_json(row.state_json)
            if current.emitted_feedback_event_id:
                return current
            if row.version != expected_version or current.status != LearningPatternStatus.READY_FOR_REVIEW:
                raise LearningExperienceConflictError("Learning pattern is not ready for emission.")
            now = datetime.now(UTC)
            updated_pattern = LearningPattern.model_validate({
                **current.model_dump(mode="python"),
                "status": LearningPatternStatus.CANDIDATE_EMITTED,
                "emitted_feedback_event_id": feedback_event_id,
                "version": current.version + 1,
                "updated_at": now,
            })
            changed = db.execute(update(ConversationLearningPatternRow).where(
                ConversationLearningPatternRow.pattern_id == pattern_id,
                ConversationLearningPatternRow.version == expected_version,
            ).values(
                status=updated_pattern.status.value,
                state_json=updated_pattern.model_dump_json(),
                version=updated_pattern.version,
                emitted_feedback_event_id=feedback_event_id,
                updated_at=now,
            ))
            if changed.rowcount != 1:
                raise LearningExperienceConflictError("Learning pattern changed during emission.")
            return updated_pattern

    def pattern(self, pattern_id: str) -> LearningPattern | None:
        with self._factory() as db:
            row = db.get(ConversationLearningPatternRow, pattern_id)
            return None if row is None else LearningPattern.model_validate_json(row.state_json)

    @staticmethod
    def _event(db: Session, experience_id: str, event_type: str, payload: dict, now: datetime) -> None:
        sequence = (db.scalar(select(ConversationExperienceEventRow.sequence).where(
            ConversationExperienceEventRow.experience_id == experience_id
        ).order_by(ConversationExperienceEventRow.sequence.desc()).limit(1)) or 0) + 1
        db.add(ConversationExperienceEventRow(
            event_id=str(uuid4()),
            experience_id=experience_id,
            sequence=sequence,
            event_type=event_type,
            payload_json=canonical_json(redact_sensitive(payload)),
            created_at=now,
        ))

    @staticmethod
    def _experience(row: ConversationExperienceRow) -> ConversationExperience:
        return ConversationExperience(
            experience_id=row.experience_id,
            owner_id=row.owner_id,
            session_id=row.session_id,
            application_id=row.application_id,
            user_message_id=row.user_message_id,
            assistant_message_id=row.assistant_message_id,
            source_hash=row.source_hash,
            observation=(LearningObservation.model_validate_json(row.observation_json)
                         if row.observation_json else None),
            status=row.status,
            error_code=row.error_code,
            created_at=row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at,
        )

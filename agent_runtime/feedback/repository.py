"""Owner-isolated feedback persistence with CAS and atomic support links."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.feedback.errors import (
    FeedbackConflictError, FeedbackNotFoundError, LearningCandidateNotFoundError,
)
from agent_runtime.feedback.models import (
    FeedbackEventRow, FeedbackProcessingAttemptRow, LearningCandidateConflictRow,
    LearningCandidateEventLinkRow, LearningCandidateRow,
)
from agent_runtime.feedback.policy import LearningThresholds, canonical_key, content_hash, ready_for_review
from agent_runtime.feedback.types import (
    CandidateStatus, CandidateType, FeedbackClassification, FeedbackEvent,
    FeedbackProcessStatus, LearningCandidate,
)
from agent_runtime.security import canonical_json


def _utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


_PROCESS_FIELDS = {"feedback_event_id", "processed_status", "processing_version",
                   "created_at", "processed_at", "deleted_at"}


class FeedbackRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    @staticmethod
    def _event(row: FeedbackEventRow) -> FeedbackEvent:
        return FeedbackEvent.model_validate({**json.loads(row.payload_json),
            "feedback_event_id":row.feedback_event_id,
            "processed_status":row.processed_status,
            "processing_version":row.processing_version,
            "created_at":_utc(row.created_at), "processed_at":_utc(row.processed_at),
            "deleted_at":_utc(row.deleted_at)})

    @staticmethod
    def _candidate(row: LearningCandidateRow) -> LearningCandidate:
        return LearningCandidate.model_validate_json(row.state_json)

    @staticmethod
    def _require_event(db: Session, event_id: str, owner_id: str) -> FeedbackEventRow:
        row = db.get(FeedbackEventRow, event_id)
        if row is None or row.owner_id != owner_id:
            raise FeedbackNotFoundError("Feedback event was not found.")
        return row

    @staticmethod
    def _require_candidate(db: Session, candidate_id: str, owner_id: str) -> LearningCandidateRow:
        row = db.get(LearningCandidateRow, candidate_id)
        if row is None or row.owner_id != owner_id:
            raise LearningCandidateNotFoundError("Learning candidate was not found.")
        return row

    def ingest(self, event: FeedbackEvent) -> FeedbackEvent:
        payload = event.model_dump(mode="json", exclude=_PROCESS_FIELDS)
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        with self._factory.begin() as db:
            existing = db.scalar(select(FeedbackEventRow).where(
                FeedbackEventRow.owner_id == event.owner_id,
                FeedbackEventRow.source_action_id == event.source_action_id))
            if existing:
                if existing.payload_hash != digest:
                    raise FeedbackConflictError("Source action was reused with different feedback.")
                return self._event(existing)
            row = FeedbackEventRow(feedback_event_id=event.feedback_event_id,
                owner_id=event.owner_id, application_id=event.application_id,
                session_id=event.session_id, task_id=event.task_id,
                artifact_id=event.artifact_id, artifact_version=event.artifact_version,
                source_type=event.source_type.value, source_action_id=event.source_action_id,
                payload_json=canonical_json(payload), payload_hash=digest,
                signal_strength=event.signal_strength.value,
                processed_status=FeedbackProcessStatus.UNPROCESSED.value,
                processing_version=0, created_at=event.created_at)
            db.add(row)
            try:
                db.flush()
            except IntegrityError as exc:
                raise FeedbackConflictError("Feedback source action conflicted.") from exc
            return self._event(row)

    def get(self, event_id: str, *, owner_id: str) -> FeedbackEvent:
        with self._factory() as db:
            return self._event(self._require_event(db, event_id, owner_id))

    def list_events(self, *, owner_id: str, limit: int = 100) -> list[FeedbackEvent]:
        with self._factory() as db:
            rows = db.scalars(select(FeedbackEventRow).where(
                FeedbackEventRow.owner_id == owner_id).order_by(
                FeedbackEventRow.created_at.desc()).limit(limit)).all()
            return [self._event(row) for row in rows]

    def begin_attempt(self, event_id: str, *, owner_id: str,
                      max_attempts: int) -> tuple[FeedbackEvent, str]:
        with self._factory.begin() as db:
            row = self._require_event(db, event_id, owner_id)
            if row.deleted_at is not None:
                raise FeedbackConflictError("Deleted feedback cannot be processed.")
            if row.processed_status == FeedbackProcessStatus.COMPLETED.value:
                return self._event(row), ""
            count = len(db.scalars(select(FeedbackProcessingAttemptRow).where(
                FeedbackProcessingAttemptRow.feedback_event_id == event_id)).all())
            if count >= max_attempts:
                raise FeedbackConflictError("Feedback processing retry limit reached.")
            running = db.scalar(select(FeedbackProcessingAttemptRow).where(
                FeedbackProcessingAttemptRow.feedback_event_id == event_id,
                FeedbackProcessingAttemptRow.status == "running"))
            if running:
                # Recovery is explicit and deterministic; no model call or tool action
                # can be in flight in v0.1, so a persisted attempt can be resumed.
                return self._event(row), running.attempt_id
            attempt_id = str(uuid4())
            db.add(FeedbackProcessingAttemptRow(attempt_id=attempt_id,
                feedback_event_id=event_id, attempt_number=count+1,
                status="running", started_at=datetime.now(UTC)))
            return self._event(row), attempt_id

    def stage(self, event_id: str, *, owner_id: str, attempt_id: str,
              expected_version: int, status: FeedbackProcessStatus,
              classification: FeedbackClassification | None = None) -> FeedbackEvent:
        with self._factory.begin() as db:
            row = self._require_event(db, event_id, owner_id)
            attempt = db.get(FeedbackProcessingAttemptRow, attempt_id)
            if attempt is None or attempt.feedback_event_id != event_id or attempt.status != "running":
                raise FeedbackConflictError("Feedback processing attempt is stale.")
            changed = db.execute(update(FeedbackEventRow).where(
                FeedbackEventRow.feedback_event_id == event_id,
                FeedbackEventRow.processing_version == expected_version).values(
                processed_status=status.value, processing_version=expected_version+1,
                processed_at=(datetime.now(UTC) if status == FeedbackProcessStatus.COMPLETED else None)))
            if changed.rowcount != 1:
                raise FeedbackConflictError("Feedback processing state changed.")
            if classification is not None:
                attempt.classification_json = classification.model_dump_json()
            if status == FeedbackProcessStatus.COMPLETED:
                attempt.status = "completed"
                attempt.finished_at = datetime.now(UTC)
            db.refresh(row)
            return self._event(row)

    def classification_for_attempt(self, attempt_id: str) -> FeedbackClassification | None:
        with self._factory() as db:
            row = db.get(FeedbackProcessingAttemptRow, attempt_id)
            return (FeedbackClassification.model_validate_json(row.classification_json)
                    if row and row.classification_json else None)

    def fail_attempt(self, event_id: str, *, owner_id: str, attempt_id: str,
                     error_code: str) -> None:
        with self._factory.begin() as db:
            row = self._require_event(db, event_id, owner_id)
            attempt = db.get(FeedbackProcessingAttemptRow, attempt_id)
            if attempt is None or attempt.status != "running":
                return
            attempt.status = "failed"; attempt.error_code = error_code[:64]
            attempt.finished_at = datetime.now(UTC)
            row.processed_status = FeedbackProcessStatus.FAILED.value
            row.processing_version += 1

    def aggregate(self, event_id: str, *, owner_id: str, attempt_id: str,
                  expected_version: int, classification: FeedbackClassification,
                  thresholds: LearningThresholds) -> LearningCandidate | None:
        if classification.supporting_feedback_event_ids != [event_id]:
            raise FeedbackConflictError("Classification references another feedback event.")
        with self._factory.begin() as db:
            event_row = self._require_event(db, event_id, owner_id)
            attempt = db.get(FeedbackProcessingAttemptRow, attempt_id)
            if attempt is None or attempt.status != "running" or attempt.feedback_event_id != event_id:
                raise FeedbackConflictError("Feedback processing attempt is stale.")
            if event_row.processing_version != expected_version:
                raise FeedbackConflictError("Feedback processing state changed.")
            if classification.candidate_type == CandidateType.IGNORE:
                candidate = None
            else:
                event = self._event(event_row)
                scope_id = (event.application_id if classification.scope.value == "application"
                    else event.owner_id if classification.scope.value == "user"
                    else classification.scope.value)
                key = canonical_key(classification.proposed_key_or_name)
                digest = content_hash(classification.proposed_content)
                current = db.scalar(select(LearningCandidateRow).where(
                    LearningCandidateRow.owner_id == owner_id,
                    LearningCandidateRow.candidate_type == classification.candidate_type.value,
                    LearningCandidateRow.scope == classification.scope.value,
                    LearningCandidateRow.scope_id == scope_id,
                    LearningCandidateRow.canonical_key == key,
                    LearningCandidateRow.content_hash == digest,
                    LearningCandidateRow.status.in_(["collecting", "ready_for_review", "needs_clarification"])))
                now = datetime.now(UTC)
                if current is None:
                    candidate = LearningCandidate(owner_id=owner_id,
                        candidate_type=classification.candidate_type,
                        status=CandidateStatus.COLLECTING,
                        proposed_key_or_name=key,
                        proposed_content=classification.proposed_content,
                        scope=classification.scope, scope_id=scope_id,
                        confidence=classification.confidence,
                        occurrence_count=1, supporting_event_ids=[event_id],
                        content_hash=digest, type_metadata=classification.type_metadata,
                        expiration_at=classification.proposed_expiration)
                    row = LearningCandidateRow(candidate_id=candidate.candidate_id,
                        owner_id=owner_id, candidate_type=candidate.candidate_type.value,
                        status=candidate.status.value, canonical_key=key,
                        scope=candidate.scope.value, scope_id=scope_id,
                        content_hash=digest, state_json=candidate.model_dump_json(),
                        version=candidate.version, created_at=now, updated_at=now)
                    db.add(row); db.flush()
                else:
                    candidate = self._candidate(current)
                    if event_id in candidate.supporting_event_ids:
                        return candidate
                    candidate = LearningCandidate.model_validate({**candidate.model_dump(mode="python"),
                        "occurrence_count":candidate.occurrence_count+1,
                        "supporting_event_ids":[*candidate.supporting_event_ids,event_id],
                        "confidence":min(0.95, max(candidate.confidence,
                            classification.confidence)+0.05),
                        "version":candidate.version+1, "updated_at":now})
                    row = current
                linked_events = [self._event(self._require_event(db, linked_id, owner_id))
                    for linked_id in candidate.supporting_event_ids]
                status = (CandidateStatus.READY_FOR_REVIEW if ready_for_review(
                    candidate.candidate_type, linked_events, thresholds=thresholds)
                    else CandidateStatus.COLLECTING)
                candidate = LearningCandidate.model_validate({**candidate.model_dump(mode="python"),
                    "status":status})
                row.status = candidate.status.value
                row.state_json = candidate.model_dump_json()
                row.version = candidate.version
                row.updated_at = now
                db.add(LearningCandidateEventLinkRow(candidate_id=candidate.candidate_id,
                    feedback_event_id=event_id, linked_at=now))
                peers = db.scalars(select(LearningCandidateRow).where(
                    LearningCandidateRow.owner_id == owner_id,
                    LearningCandidateRow.candidate_type == candidate.candidate_type.value,
                    LearningCandidateRow.scope == candidate.scope.value,
                    LearningCandidateRow.scope_id == candidate.scope_id,
                    LearningCandidateRow.canonical_key == key,
                    LearningCandidateRow.content_hash != digest)).all()
                for peer in peers:
                    if peer.candidate_id == candidate.candidate_id:
                        continue
                    if db.get(LearningCandidateConflictRow,
                              (candidate.candidate_id,peer.candidate_id)) is None:
                        db.add(LearningCandidateConflictRow(candidate_id=candidate.candidate_id,
                            conflicting_candidate_id=peer.candidate_id,
                            reason_code="same_key_different_value", created_at=now))
                    if db.get(LearningCandidateConflictRow,
                              (peer.candidate_id,candidate.candidate_id)) is None:
                        db.add(LearningCandidateConflictRow(candidate_id=peer.candidate_id,
                            conflicting_candidate_id=candidate.candidate_id,
                            reason_code="same_key_different_value", created_at=now))
                    if not set(self._candidate(peer).supporting_event_ids).issubset(
                            candidate.conflicting_event_ids):
                        candidate = LearningCandidate.model_validate({**candidate.model_dump(mode="python"),
                            "conflicting_event_ids":list(dict.fromkeys([
                                *candidate.conflicting_event_ids,*self._candidate(peer).supporting_event_ids]))})
                    old_peer = self._candidate(peer)
                    revised_peer = LearningCandidate.model_validate({**old_peer.model_dump(mode="python"),
                        "conflicting_event_ids":list(dict.fromkeys([
                            *old_peer.conflicting_event_ids, *candidate.supporting_event_ids])),
                        "version":old_peer.version+1, "updated_at":now})
                    peer.state_json = revised_peer.model_dump_json()
                    peer.version = revised_peer.version
                    peer.updated_at = now
                row.state_json = candidate.model_dump_json()
            changed = db.execute(update(FeedbackEventRow).where(
                FeedbackEventRow.feedback_event_id == event_id,
                FeedbackEventRow.processing_version == expected_version).values(
                processed_status=FeedbackProcessStatus.CANDIDATE_CREATED_OR_UPDATED.value,
                processing_version=expected_version+1))
            if changed.rowcount != 1:
                raise FeedbackConflictError("Feedback processing state changed.")
            db.flush()
            return candidate

    def candidate_for_event(self, event_id: str, *, owner_id: str) -> LearningCandidate | None:
        with self._factory() as db:
            self._require_event(db, event_id, owner_id)
            row = db.scalar(select(LearningCandidateRow).join(LearningCandidateEventLinkRow).where(
                LearningCandidateEventLinkRow.feedback_event_id == event_id,
                LearningCandidateRow.owner_id == owner_id))
            return self._candidate(row) if row else None

    def candidate(self, candidate_id: str, *, owner_id: str) -> LearningCandidate:
        with self._factory() as db:
            return self._candidate(self._require_candidate(db, candidate_id, owner_id))

    def list_candidates(self, *, owner_id: str, candidate_type: CandidateType | None = None,
                        limit: int = 100) -> list[LearningCandidate]:
        with self._factory() as db:
            query = select(LearningCandidateRow).where(LearningCandidateRow.owner_id == owner_id)
            if candidate_type:
                query = query.where(LearningCandidateRow.candidate_type == candidate_type.value)
            rows = db.scalars(query.order_by(LearningCandidateRow.updated_at.desc()).limit(limit)).all()
            return [self._candidate(row) for row in rows]

    def candidate_events(self, candidate_id: str, *, owner_id: str) -> list[FeedbackEvent]:
        with self._factory() as db:
            self._require_candidate(db, candidate_id, owner_id)
            rows = db.scalars(select(FeedbackEventRow).join(LearningCandidateEventLinkRow).where(
                LearningCandidateEventLinkRow.candidate_id == candidate_id).order_by(
                FeedbackEventRow.created_at)).all()
            return [self._event(row) for row in rows]

    def conflicts(self, candidate_id: str, *, owner_id: str) -> list[LearningCandidate]:
        with self._factory() as db:
            self._require_candidate(db, candidate_id, owner_id)
            rows = db.scalars(select(LearningCandidateRow).join(LearningCandidateConflictRow,
                LearningCandidateConflictRow.conflicting_candidate_id == LearningCandidateRow.candidate_id).where(
                LearningCandidateConflictRow.candidate_id == candidate_id,
                LearningCandidateRow.owner_id == owner_id)).all()
            return [item for row in rows if (item := self._candidate(row)).occurrence_count > 0]

    def review(self, candidate_id: str, *, owner_id: str, expected_version: int,
               idempotency_key: str, status: CandidateStatus, action: str,
               proposed_content: str | None = None, linked_memory_id: str | None = None,
               linked_evidence_id: str | None = None) -> LearningCandidate:
        action_key = f"{action}:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        with self._factory.begin() as db:
            row = self._require_candidate(db, candidate_id, owner_id)
            current = self._candidate(row)
            if current.review_action == action_key:
                return current
            if row.version != expected_version:
                raise FeedbackConflictError("Learning candidate version changed.")
            if current.status in {CandidateStatus.CONFIRMED, CandidateStatus.REJECTED,
                                  CandidateStatus.SUPERSEDED, CandidateStatus.EXPIRED}:
                raise FeedbackConflictError("Learning candidate is already final.")
            now = datetime.now(UTC)
            content = proposed_content if proposed_content is not None else current.proposed_content
            metadata = dict(current.type_metadata)
            if proposed_content is not None and proposed_content != current.proposed_content:
                metadata.setdefault("original_proposal", current.proposed_content)
                metadata["review_edits"] = [*metadata.get("review_edits", []),
                    {"previous": current.proposed_content, "revised": proposed_content}]
                if current.candidate_type == CandidateType.SKILL:
                    metadata["proposed_instructions"] = proposed_content
                elif current.candidate_type == CandidateType.PREFERENCE_MEMORY:
                    metadata["proposed_value"] = proposed_content
                elif current.candidate_type == CandidateType.CAREER_EVIDENCE:
                    metadata["claim"] = proposed_content
            updated = LearningCandidate.model_validate({**current.model_dump(mode="python"),
                "status":status, "proposed_content":content,
                "content_hash":content_hash(content),
                "type_metadata":metadata,
                "review_action":action_key,
                "linked_memory_id":linked_memory_id or current.linked_memory_id,
                "linked_evidence_id":linked_evidence_id or current.linked_evidence_id,
                "version":expected_version+1,"updated_at":now,
                "reviewed_at":now if status in {CandidateStatus.CONFIRMED,
                    CandidateStatus.REJECTED} else current.reviewed_at})
            changed = db.execute(update(LearningCandidateRow).where(
                LearningCandidateRow.candidate_id == candidate_id,
                LearningCandidateRow.version == expected_version).values(
                status=status.value, state_json=updated.model_dump_json(),
                content_hash=updated.content_hash, version=updated.version, updated_at=now))
            if changed.rowcount != 1:
                raise FeedbackConflictError("Learning candidate version changed.")
            return updated

    def soft_delete(self, event_id: str, *, owner_id: str,
                    thresholds: LearningThresholds) -> FeedbackEvent:
        with self._factory.begin() as db:
            row = self._require_event(db, event_id, owner_id)
            if row.deleted_at is not None:
                return self._event(row)
            row.deleted_at = datetime.now(UTC)
            db.flush()
            affected = db.scalars(select(LearningCandidateRow).join(
                LearningCandidateEventLinkRow).where(
                LearningCandidateEventLinkRow.feedback_event_id == event_id)).all()
            for candidate_row in affected:
                candidate = self._candidate(candidate_row)
                live = [self._event(self._require_event(db, linked_id, owner_id))
                    for linked_id in candidate.supporting_event_ids if linked_id != event_id]
                live = [item for item in live if item.deleted_at is None]
                support = [item.feedback_event_id for item in live]
                next_status = candidate.status
                if candidate.status == CandidateStatus.READY_FOR_REVIEW and not ready_for_review(
                    candidate.candidate_type, live, thresholds=thresholds):
                    next_status = CandidateStatus.COLLECTING
                updated = LearningCandidate.model_validate({**candidate.model_dump(mode="python"),
                    "supporting_event_ids":support, "occurrence_count":len(support),
                    "confidence":min(candidate.confidence, 0.5+0.1*len(support)),
                    "status":next_status, "version":candidate.version+1,
                    "updated_at":datetime.now(UTC)})
                candidate_row.state_json = updated.model_dump_json()
                candidate_row.status = updated.status.value
                candidate_row.version = updated.version
                candidate_row.updated_at = updated.updated_at
            for peer_row in db.scalars(select(LearningCandidateRow).where(
                    LearningCandidateRow.owner_id == owner_id)).all():
                peer = self._candidate(peer_row)
                if event_id not in peer.conflicting_event_ids:
                    continue
                revised = LearningCandidate.model_validate({**peer.model_dump(mode="python"),
                    "conflicting_event_ids":[item for item in peer.conflicting_event_ids
                        if item != event_id], "version":peer.version+1,
                    "updated_at":datetime.now(UTC)})
                peer_row.state_json = revised.model_dump_json()
                peer_row.version = revised.version
                peer_row.updated_at = revised.updated_at
            return self._event(row)

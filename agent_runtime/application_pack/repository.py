"""Transactional Pack state, events and immutable Workspace artifacts."""
from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from pydantic import ValidationError

from agent_runtime.application_pack.errors import PackConflictError, PackNotFoundError, PackValidationError
from agent_runtime.application_pack.models import (
    ApplicationPackEventRow, ApplicationPackItemRow, ApplicationPackRow,
    GenerationEvidenceSnapshotItemRow, GenerationEvidenceSnapshotRow,
)
from agent_runtime.application_pack.types import (
    ApplicationPack, ApplicationPackItem, ArtifactVerification,
    GenerationEvidenceSnapshot, ItemStatus, PackEvent, PackStatus,
)
from agent_runtime.security import canonical_json
from agent_runtime.workspace.models import ApplicationArtifactRow, ApplicationRow, ApplicationRunRow
from api.models import Run
from job_agent.schemas import ResumeAnalysis, TailoredResume
from agent_runtime.application_pack.types import ApplicationAnswer, CoverLetter


def _utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def _block_texts(content: dict | None, artifact_type: str) -> dict[str, str]:
    if not content:
        return {}
    if artifact_type == "tailored_resume":
        resume = TailoredResume.model_validate(content)
        return {claim.claim_id: claim.text for claim in resume.claims()}
    if artifact_type == "cover_letter":
        letter = CoverLetter.model_validate(content)
        return {f"paragraph-{index}": paragraph.text
                for index, paragraph in enumerate(letter.paragraphs, start=1)}
    key = "answer_blocks"
    return {block["block_id"]: block["text"] for block in content.get(key, [])}


def _artifact_claims(content: dict, artifact_type: str) -> list[dict]:
    if artifact_type == "tailored_resume":
        return [claim.model_dump(mode="json")
                for claim in TailoredResume.model_validate(content).claims()]
    if artifact_type == "cover_letter":
        return [paragraph.model_dump(mode="json")
                for paragraph in CoverLetter.model_validate(content).paragraphs]
    return list(content.get("answer_blocks") or [])


class PackRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    @staticmethod
    def _pack(row: ApplicationPackRow) -> ApplicationPack:
        return ApplicationPack(pack_id=row.pack_id, application_id=row.application_id,
            workflow_mode=row.workflow_mode,
            snapshot_id=row.snapshot_id, status=row.status, version=row.version,
            generation_number=row.generation_number, evidence_set_hash=row.evidence_set_hash,
            preference_snapshot_id=row.preference_snapshot_id,
            created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
            completed_at=_utc(row.completed_at), error_code=row.error_code,
            stale_reason=row.stale_reason)

    @staticmethod
    def _item(row: ApplicationPackItemRow) -> ApplicationPackItem:
        return ApplicationPackItem(pack_item_id=row.pack_item_id, pack_id=row.pack_id,
            artifact_id=row.artifact_id, artifact_type=row.artifact_type,
            status=row.status, source_question=row.source_question, max_length=row.max_length,
            version=row.version, revision_count=row.revision_count,
            max_revisions=row.max_revisions,
            verification=ArtifactVerification.model_validate_json(row.verification_json)
                if row.verification_json else None,
            requires_manual_answer=row.requires_manual_answer,
            created_at=_utc(row.created_at), updated_at=_utc(row.updated_at))

    @staticmethod
    def _require(session: Session, pack_id: str) -> ApplicationPackRow:
        row = session.get(ApplicationPackRow, pack_id)
        if row is None: raise PackNotFoundError("Pack was not found.")
        return row

    @staticmethod
    def _require_item(session: Session, pack_id: str, item_id: str) -> ApplicationPackItemRow:
        row = session.get(ApplicationPackItemRow, item_id)
        if row is None or row.pack_id != pack_id: raise PackNotFoundError("Pack item was not found.")
        return row

    @staticmethod
    def _advance_pack(session: Session, row: ApplicationPackRow, expected_version: int) -> None:
        now = datetime.now(UTC)
        changed = session.execute(update(ApplicationPackRow).where(
            ApplicationPackRow.pack_id == row.pack_id,
            ApplicationPackRow.version == expected_version,
        ).values(version=expected_version + 1, updated_at=now))
        if changed.rowcount != 1: raise PackConflictError("Pack version changed.")
        session.refresh(row)

    @staticmethod
    def _advance_item(session: Session, row: ApplicationPackItemRow, expected_version: int) -> None:
        changed = session.execute(update(ApplicationPackItemRow).where(
            ApplicationPackItemRow.pack_item_id == row.pack_item_id,
            ApplicationPackItemRow.version == expected_version,
        ).values(version=expected_version + 1, updated_at=datetime.now(UTC)))
        if changed.rowcount != 1: raise PackConflictError("Pack item version changed.")
        session.refresh(row)

    @staticmethod
    def _event(session: Session, pack: ApplicationPackRow, kind: str, payload: dict | None = None,
               idempotency_key: str | None = None) -> None:
        pack.event_sequence += 1
        session.add(ApplicationPackEventRow(event_id=str(uuid4()), pack_id=pack.pack_id,
            sequence=pack.event_sequence, event_type=kind,
            payload_json=canonical_json(payload or {}), idempotency_key=idempotency_key,
            occurred_at=datetime.now(UTC)))

    @staticmethod
    def _replay(session: Session, pack_id: str, key: str | None, kind: str):
        if key is None: return None
        event = session.scalar(select(ApplicationPackEventRow).where(
            ApplicationPackEventRow.pack_id == pack_id,
            ApplicationPackEventRow.idempotency_key == key))
        if event is not None and event.event_type != kind:
            raise PackConflictError("Idempotency key was used for another action.")
        return event

    def create(self, snapshot: GenerationEvidenceSnapshot, *, idempotency_key: str,
               expected_application_version: int, workflow_mode: str = "single_custom") -> ApplicationPack:
        with self._factory.begin() as session:
            app = session.get(ApplicationRow, snapshot.application_id)
            if app is None: raise PackNotFoundError("Application was not found.")
            replay = session.scalar(select(ApplicationPackRow).where(
                ApplicationPackRow.application_id == snapshot.application_id,
                ApplicationPackRow.idempotency_key == idempotency_key))
            if replay:
                if (replay.snapshot_id != snapshot.job_snapshot_id or replay.evidence_set_hash != snapshot.evidence_set_hash
                        or replay.workflow_mode != workflow_mode):
                    raise PackConflictError("Idempotency key was used for different inputs.")
                return self._pack(replay)
            if app.version != expected_application_version:
                raise PackConflictError("Application version changed.")
            if app.current_snapshot_id != snapshot.job_snapshot_id:
                raise PackConflictError("The Job snapshot changed.")
            number = (session.scalar(select(func.max(ApplicationPackRow.generation_number)).where(
                ApplicationPackRow.application_id == app.application_id)) or 0) + 1
            now = datetime.now(UTC)
            row = ApplicationPackRow(pack_id=snapshot.pack_id, application_id=app.application_id,
                workflow_mode=workflow_mode,
                snapshot_id=snapshot.job_snapshot_id, status=PackStatus.DRAFT.value,
                version=1, generation_number=number, record_version=1, event_sequence=0,
                evidence_set_hash=snapshot.evidence_set_hash,
                preference_snapshot_id=snapshot.generation_snapshot_id,
                prompt_version=snapshot.prompt_version, idempotency_key=idempotency_key,
                created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            session.add(GenerationEvidenceSnapshotRow(
                generation_snapshot_id=snapshot.generation_snapshot_id, pack_id=row.pack_id,
                application_id=app.application_id, job_snapshot_id=app.current_snapshot_id,
                manifest_json=snapshot.model_dump_json(),
                evidence_set_hash=snapshot.evidence_set_hash, created_at=now))
            session.flush()
            for item in snapshot.items:
                session.add(GenerationEvidenceSnapshotItemRow(
                    generation_snapshot_id=snapshot.generation_snapshot_id,
                    evidence_id=item.evidence_id, evidence_version_id=item.evidence_version_id,
                    content_hash=item.content_hash, source_type=item.source_type,
                    selection_reason=item.selection_reason,
                    associated_requirement_ids_json=canonical_json(item.associated_requirement_ids)))
            self._event(session, row, "PACK_CREATED", {"snapshot_id": snapshot.job_snapshot_id})
            session.flush()
            return self._pack(row)

    def get(self, pack_id: str) -> ApplicationPack:
        with self._factory() as session: return self._pack(self._require(session, pack_id))

    def resume_source(self, application_id: str, run_id: str) -> tuple[str, ResumeAnalysis] | None:
        """Read only an explicitly attached run; never trust a foreign run ID."""
        with self._factory() as session:
            linked = session.scalar(select(ApplicationRunRow).where(
                ApplicationRunRow.application_id == application_id,
                ApplicationRunRow.run_id == run_id))
            run = session.get(Run, run_id) if linked else None
            if run is None or not run.result_json:
                return None
            try:
                result = json.loads(run.result_json)
                analysis = ResumeAnalysis.model_validate(result["resume_analysis"])
            except (ValueError, TypeError, KeyError):
                return None
            return run.resume_text, analysis

    def list_for_application(self, application_id: str) -> list[ApplicationPack]:
        with self._factory() as session:
            if session.get(ApplicationRow, application_id) is None: raise PackNotFoundError("Application was not found.")
            return [self._pack(row) for row in session.scalars(select(ApplicationPackRow).where(
                ApplicationPackRow.application_id == application_id).order_by(
                ApplicationPackRow.generation_number.desc())).all()]

    def snapshot(self, pack_id: str) -> GenerationEvidenceSnapshot:
        with self._factory() as session:
            self._require(session, pack_id)
            row = session.scalar(select(GenerationEvidenceSnapshotRow).where(
                GenerationEvidenceSnapshotRow.pack_id == pack_id))
            return GenerationEvidenceSnapshot.model_validate_json(row.manifest_json)

    def items(self, pack_id: str) -> list[ApplicationPackItem]:
        with self._factory() as session:
            self._require(session, pack_id)
            return [self._item(row) for row in session.scalars(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.pack_id == pack_id).order_by(ApplicationPackItemRow.created_at)).all()]

    def item(self, pack_id: str, item_id: str) -> ApplicationPackItem:
        with self._factory() as session: return self._item(self._require_item(session, pack_id, item_id))

    def item_for_artifact(self, artifact_id: str) -> ApplicationPackItem | None:
        """Return the Pack item that owns an immutable Workspace artifact."""
        with self._factory() as session:
            row = session.scalar(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.artifact_id == artifact_id))
            return self._item(row) if row is not None else None

    def add_item(self, pack_id: str, *, expected_version: int, artifact_type: str,
                 idempotency_key: str, source_question: str | None = None,
                 max_length: int | None = None, requires_manual_answer: bool = False,
                 max_revisions: int = 3) -> ApplicationPackItem:
        if artifact_type not in {"tailored_resume", "cover_letter", "application_answer"}:
            raise PackValidationError("Unsupported artifact type.")
        if not 0 <= max_revisions <= 3: raise PackValidationError("Revision limit is invalid.")
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            replay = session.scalar(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.pack_id == pack_id,
                ApplicationPackItemRow.idempotency_key == idempotency_key))
            if replay:
                if (replay.artifact_type, replay.source_question, replay.max_length) != (artifact_type, source_question, max_length):
                    raise PackConflictError("Idempotency key was used for another item.")
                return self._item(replay)
            if pack.version != expected_version: raise PackConflictError("Pack version changed.")
            if pack.status in {PackStatus.STALE.value, PackStatus.APPROVED.value}:
                raise PackConflictError("A stale or approved Pack cannot be changed.")
            if artifact_type != "application_answer" and session.scalar(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.pack_id == pack_id,
                ApplicationPackItemRow.artifact_type == artifact_type)):
                raise PackConflictError("This Pack already contains that material.")
            self._advance_pack(session, pack, expected_version)
            now = datetime.now(UTC)
            row = ApplicationPackItemRow(pack_item_id=str(uuid4()), pack_id=pack_id,
                artifact_type=artifact_type,
                status=ItemStatus.AWAITING_REVIEW.value if requires_manual_answer else ItemStatus.GENERATING.value,
                source_question=source_question, max_length=max_length, version=1,
                revision_count=0, max_revisions=max_revisions,
                requires_manual_answer=requires_manual_answer,
                idempotency_key=idempotency_key, created_at=now, updated_at=now)
            session.add(row)
            pack.status = PackStatus.AWAITING_REVIEW.value if requires_manual_answer else PackStatus.GENERATING.value
            self._event(session, pack, "ITEM_CREATED", {"item_id": row.pack_item_id,
                                                         "artifact_type": artifact_type,
                                                         "manual": requires_manual_answer})
            session.flush()
            return self._item(row)

    def save_artifact(self, pack_id: str, item_id: str, *, expected_version: int,
                      content: dict, revision: bool = False,
                      idempotency_key: str | None = None,
                      origin: str = "model") -> ApplicationPackItem:
        if origin not in {"model", "user_edit"}:
            raise PackValidationError("Artifact origin is invalid.")
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            item = self._require_item(session, pack_id, item_id)
            content_digest = hashlib.sha256(canonical_json(content).encode()).hexdigest()
            replay = self._replay(session, pack_id, idempotency_key, "ARTIFACT_VERSION_SAVED")
            if replay:
                metadata = json.loads(replay.payload_json)
                if metadata.get("item_id") != item_id or metadata.get("content_hash") != content_digest:
                    raise PackConflictError("Idempotency key was used for different content.")
                return self._item(item)
            if item.version != expected_version: raise PackConflictError("Pack item version changed.")
            if item.requires_manual_answer or pack.status == PackStatus.STALE.value:
                raise PackConflictError("This item cannot generate content.")
            if origin == "user_edit" and item.status not in {
                ItemStatus.AWAITING_REVIEW.value, ItemStatus.NEEDS_REVISION.value,
                ItemStatus.REJECTED.value,
            }:
                raise PackConflictError("Item cannot be edited now.")
            if item.status not in {ItemStatus.GENERATING.value, ItemStatus.NEEDS_REVISION.value,
                                   ItemStatus.AWAITING_REVIEW.value, ItemStatus.REJECTED.value,
                                   ItemStatus.FAILED.value}:
                raise PackConflictError("This item is not ready for generation.")
            schema = {"tailored_resume": TailoredResume, "cover_letter": CoverLetter,
                      "application_answer": ApplicationAnswer}[item.artifact_type]
            try:
                validated = schema.model_validate(content).model_dump(mode="json")
            except ValidationError as exc:
                raise PackValidationError("Artifact content does not match its schema.") from exc
            prior_artifact = session.get(ApplicationArtifactRow, item.artifact_id) if item.artifact_id else None
            prior_blocks = _block_texts(json.loads(prior_artifact.content_json), item.artifact_type) if prior_artifact else {}
            current_blocks = _block_texts(validated, item.artifact_type)
            block_reviews = ([{"block_id": block_id,
                "decision": "edited" if prior_blocks.get(block_id) != value else "accepted"}
                for block_id, value in current_blocks.items()] +
                [{"block_id": block_id, "decision": "rejected"}
                 for block_id in prior_blocks if block_id not in current_blocks]) if origin == "user_edit" else []
            artifact_version = (session.scalar(select(func.max(ApplicationArtifactRow.version)).where(
                ApplicationArtifactRow.application_id == pack.application_id,
                ApplicationArtifactRow.artifact_type == item.artifact_type)) or 0) + 1
            artifact = ApplicationArtifactRow(artifact_id=str(uuid4()),
                application_id=pack.application_id, artifact_type=item.artifact_type,
                workflow_mode=pack.workflow_mode,
                version=artifact_version, status="draft", content_json=canonical_json(validated),
                evidence_ids_json=canonical_json(sorted({identifier for block in
                    (_artifact_claims(validated, item.artifact_type))
                    for identifier in block.get("evidence_ids", [])})),
                verification_status="pending", created_by=f"pack:{pack_id}:{item_id}",
                created_at=datetime.now(UTC))
            session.add(artifact)
            self._advance_item(session, item, expected_version)
            self._advance_pack(session, pack, pack.version)
            item.artifact_id = artifact.artifact_id
            item.verification_json = None
            item.status = ItemStatus.VERIFYING.value
            if revision: item.revision_count += 1
            pack.status = PackStatus.VERIFYING.value
            self._event(session, pack, "ARTIFACT_VERSION_SAVED", {"item_id": item_id,
                "artifact_id": artifact.artifact_id, "version": artifact_version,
                "content_hash": content_digest, "origin": origin,
                "block_reviews": block_reviews}, idempotency_key)
            session.flush()
            return self._item(item)

    def verify(self, pack_id: str, item_id: str, *, expected_version: int,
               verification: ArtifactVerification) -> ApplicationPackItem:
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            item = self._require_item(session, pack_id, item_id)
            if item.version != expected_version or item.status != ItemStatus.VERIFYING.value:
                raise PackConflictError("Item is not awaiting verification.")
            artifact = session.get(ApplicationArtifactRow, item.artifact_id)
            self._advance_item(session, item, expected_version)
            self._advance_pack(session, pack, pack.version)
            item.verification_json = verification.model_dump_json()
            item.status = ItemStatus.AWAITING_REVIEW.value if verification.passed else ItemStatus.NEEDS_REVISION.value
            artifact.status = "verified" if verification.passed else "draft"
            artifact.verification_status = "passed" if verification.passed else "failed"
            pack.status = PackStatus.AWAITING_REVIEW.value if verification.passed else PackStatus.NEEDS_REVISION.value
            self._event(session, pack, "ARTIFACT_VERIFIED", {"item_id": item_id,
                "passed": verification.passed, "issue_count": len(verification.issues)})
            session.flush()
            return self._item(item)

    def artifact(self, pack_id: str, item_id: str) -> dict | None:
        with self._factory() as session:
            item = self._require_item(session, pack_id, item_id)
            row = session.get(ApplicationArtifactRow, item.artifact_id) if item.artifact_id else None
            return json.loads(row.content_json) if row else None

    def versions(self, pack_id: str, item_id: str) -> list[dict]:
        with self._factory() as session:
            item = self._require_item(session, pack_id, item_id)
            rows = session.scalars(select(ApplicationArtifactRow).where(
                ApplicationArtifactRow.created_by == f"pack:{pack_id}:{item_id}").order_by(
                ApplicationArtifactRow.version)).all()
            return [{"artifact_id": row.artifact_id, "version": row.version,
                     "status": row.status, "verification_status": row.verification_status,
                     "content": json.loads(row.content_json), "created_at": _utc(row.created_at).isoformat()}
                    for row in rows]

    def review(self, pack_id: str, item_id: str, *, expected_version: int,
               approve: bool, idempotency_key: str | None = None) -> ApplicationPackItem:
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            item = self._require_item(session, pack_id, item_id)
            if pack.status == PackStatus.STALE.value:
                raise PackConflictError("A stale Pack cannot be reviewed.")
            target = ItemStatus.APPROVED.value if approve else ItemStatus.REJECTED.value
            replay = self._replay(session, pack_id, idempotency_key,
                                  "ITEM_APPROVED" if approve else "ITEM_REJECTED")
            if replay:
                if json.loads(replay.payload_json).get("item_id") != item_id:
                    raise PackConflictError("Idempotency key was used for another item.")
                return self._item(item)
            if item.status == target: return self._item(item)
            if item.version != expected_version: raise PackConflictError("Pack item version changed.")
            if item.status != ItemStatus.AWAITING_REVIEW.value or item.requires_manual_answer:
                raise PackConflictError("Only a verified item awaiting review can be approved or rejected.")
            if approve and (not item.verification_json or not ArtifactVerification.model_validate_json(item.verification_json).passed):
                raise PackConflictError("Verification must pass before approval.")
            artifact = session.get(ApplicationArtifactRow, item.artifact_id)
            block_ids = list(_block_texts(json.loads(artifact.content_json), item.artifact_type))
            self._advance_item(session, item, expected_version)
            self._advance_pack(session, pack, pack.version)
            item.status = target
            if approve: artifact.status = "approved"
            self._event(session, pack, "ITEM_APPROVED" if approve else "ITEM_REJECTED",
                        {"item_id": item_id, "artifact_id": item.artifact_id,
                         "block_reviews": [{"block_id": block_id,
                           "decision": "accepted" if approve else "rejected"}
                           for block_id in block_ids]}, idempotency_key)
            rows = session.scalars(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.pack_id == pack_id)).all()
            required = {"tailored_resume", "cover_letter"}
            if approve and required <= {row.artifact_type for row in rows} and all(
                    row.status == ItemStatus.APPROVED.value or row.requires_manual_answer
                    for row in rows):
                pack.status = PackStatus.APPROVED.value
                pack.completed_at = datetime.now(UTC)
            elif not approve:
                pack.status = PackStatus.NEEDS_REVISION.value
            session.flush()
            return self._item(item)

    def fail_item(self, pack_id: str, item_id: str, *, expected_version: int) -> ApplicationPackItem:
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            item = self._require_item(session, pack_id, item_id)
            if item.version != expected_version: raise PackConflictError("Pack item version changed.")
            self._advance_item(session, item, expected_version)
            self._advance_pack(session, pack, pack.version)
            item.status = ItemStatus.FAILED.value
            pack.status = PackStatus.FAILED.value if item.artifact_type in {"tailored_resume", "cover_letter"} else pack.status
            pack.error_code = "item_generation_failed"
            self._event(session, pack, "ITEM_FAILED", {"item_id": item_id})
            session.flush()
            return self._item(item)

    def request_regeneration(self, pack_id: str, item_id: str, *, expected_version: int,
                             idempotency_key: str) -> ApplicationPackItem:
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            item = self._require_item(session, pack_id, item_id)
            if pack.status == PackStatus.STALE.value:
                raise PackConflictError("A stale Pack cannot be regenerated.")
            replay = self._replay(session, pack_id, idempotency_key, "ITEM_REGENERATE_REQUESTED")
            if replay:
                if json.loads(replay.payload_json).get("item_id") != item_id:
                    raise PackConflictError("Idempotency key was used for another item.")
                return self._item(item)
            if item.version != expected_version: raise PackConflictError("Pack item version changed.")
            if item.status not in {ItemStatus.REJECTED.value, ItemStatus.FAILED.value,
                                   ItemStatus.NEEDS_REVISION.value, ItemStatus.AWAITING_REVIEW.value}:
                raise PackConflictError("Item is not ready for regeneration.")
            if item.requires_manual_answer or pack.status == PackStatus.STALE.value:
                raise PackConflictError("This item requires a new Pack or manual answer.")
            self._advance_item(session, item, expected_version)
            self._advance_pack(session, pack, pack.version)
            item.status = ItemStatus.GENERATING.value
            item.revision_count = 0
            item.verification_json = None
            pack.status = PackStatus.GENERATING.value
            self._event(session, pack, "ITEM_REGENERATE_REQUESTED",
                        {"item_id": item_id}, idempotency_key)
            session.flush()
            return self._item(item)

    def stale(self, pack_id: str, reason: str) -> ApplicationPack:
        with self._factory.begin() as session:
            pack = self._require(session, pack_id)
            if pack.status == PackStatus.STALE.value: return self._pack(pack)
            self._advance_pack(session, pack, pack.version)
            pack.status = PackStatus.STALE.value
            pack.stale_reason = reason
            self._event(session, pack, "PACK_STALE", {"reason": reason})
            session.flush()
            return self._pack(pack)

    def events(self, pack_id: str) -> list[PackEvent]:
        with self._factory() as session:
            self._require(session, pack_id)
            rows = session.scalars(select(ApplicationPackEventRow).where(
                ApplicationPackEventRow.pack_id == pack_id).order_by(ApplicationPackEventRow.sequence)).all()
            return [PackEvent(event_id=row.event_id, pack_id=row.pack_id, sequence=row.sequence,
                event_type=row.event_type, payload=json.loads(row.payload_json),
                occurred_at=_utc(row.occurred_at)) for row in rows]

"""Transactional Career Evidence Vault repository."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.security import canonical_json
from agent_runtime.evidence.errors import (
    EvidenceNotFoundError, EvidenceProvenanceError, InvalidEvidenceTransitionError,
    StaleEvidenceError,
)
from agent_runtime.evidence.models import (
    CareerEvidenceEventRow, CareerEvidenceRow, CareerEvidenceVersionRow,
    EvidenceApplicationLinkRow,
)
from agent_runtime.evidence.types import (
    CareerEvidence, CareerEvidenceEvent, EvidenceApplicationLink, EvidenceCategory,
    EvidenceLinkType, EvidenceMetric, EvidenceSourceType, EvidenceStatus,
    EvidenceVersion,
)
from agent_runtime.workspace.models import ApplicationRow
from job_agent.domain import normalize_text
from job_agent.schemas import ResumeAnalysis


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _content_hash(data: dict[str, Any]) -> str:
    normalized = {
        key: normalize_text(value) if isinstance(value, str) else value
        for key, value in data.items()
        if key not in {"source_run_id", "created_by"}
    }
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


class CareerEvidenceRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _version(row: CareerEvidenceVersionRow) -> EvidenceVersion:
        return EvidenceVersion(
            evidence_version_id=row.evidence_version_id, evidence_id=row.evidence_id,
            version_number=row.version_number, category=row.category, claim_text=row.claim_text,
            source_type=row.source_type, source_reference=row.source_reference,
            exact_quote=row.exact_quote, source_section=row.source_section,
            employer_or_project=row.employer_or_project, role=row.role,
            start_date=row.start_date, end_date=row.end_date,
            technologies=json.loads(row.technologies_json), metrics=json.loads(row.metrics_json),
            tags=json.loads(row.tags_json), created_by=row.created_by,
            created_at=_utc(row.created_at), content_hash=row.content_hash,
            external_evidence_id=row.external_evidence_id, source_run_id=row.source_run_id,
        )

    def _item(self, session: Session, row: CareerEvidenceRow) -> CareerEvidence:
        version = session.scalar(select(CareerEvidenceVersionRow).where(
            CareerEvidenceVersionRow.evidence_id == row.evidence_id,
            CareerEvidenceVersionRow.version_number == row.current_version,
        ))
        return CareerEvidence(
            evidence_id=row.evidence_id, status=row.status, current_version=row.current_version,
            version=row.version, created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
            confirmed_at=_utc(row.confirmed_at), rejected_at=_utc(row.rejected_at),
            archived_at=_utc(row.archived_at), current=self._version(version),
        )

    @staticmethod
    def _event(session: Session, row: CareerEvidenceRow, kind: str,
               version_id: str | None = None, payload: dict | None = None) -> None:
        row.event_sequence += 1
        session.add(CareerEvidenceEventRow(
            event_id=str(uuid4()), evidence_id=row.evidence_id, sequence=row.event_sequence,
            event_type=kind, evidence_version_id=version_id,
            payload_json=canonical_json(payload or {}), occurred_at=datetime.now(UTC),
        ))

    @staticmethod
    def _require(session: Session, evidence_id: str) -> CareerEvidenceRow:
        row = session.get(CareerEvidenceRow, evidence_id)
        if row is None:
            raise EvidenceNotFoundError("The evidence item was not found.")
        return row

    @staticmethod
    def _advance(session: Session, row: CareerEvidenceRow, expected_version: int) -> None:
        changed = session.execute(update(CareerEvidenceRow).where(
            CareerEvidenceRow.evidence_id == row.evidence_id,
            CareerEvidenceRow.version == expected_version,
        ).values(version=expected_version + 1, updated_at=datetime.now(UTC)))
        if changed.rowcount != 1:
            raise StaleEvidenceError("The evidence version has changed.")
        session.refresh(row)

    @staticmethod
    def _validated_fields(data: dict[str, Any], original_resume_text: str | None) -> dict[str, Any]:
        source = EvidenceSourceType(data["source_type"])
        claim = str(data["claim_text"]).strip()
        quote = data.get("exact_quote")
        if not claim:
            raise EvidenceProvenanceError("A claim is required.")
        if quote and normalize_text(claim) != normalize_text(quote):
            raise EvidenceProvenanceError("Claim wording must not be stronger than the source quote.")
        if source == EvidenceSourceType.RESUME:
            if not quote or not original_resume_text or normalize_text(quote) not in normalize_text(original_resume_text):
                raise EvidenceProvenanceError("The exact resume quote must occur in the original resume.")
        if source == EvidenceSourceType.DOCUMENT and (not quote or not data.get("source_reference")):
            raise EvidenceProvenanceError("Document evidence needs an exact quote and source reference.")
        if source == EvidenceSourceType.USER_ATTESTED and not quote:
            raise EvidenceProvenanceError("User-attested claim must preserve the original statement.")
        if source == EvidenceSourceType.PROJECT and not data.get("source_reference"):
            raise EvidenceProvenanceError("Project evidence needs a source reference.")
        metrics = [EvidenceMetric.model_validate(item).model_dump(mode="json") for item in data.get("metrics", [])]
        for metric in metrics:
            if normalize_text(metric["original_text"]) not in normalize_text(quote or claim):
                raise EvidenceProvenanceError("Metrics must appear verbatim in the source statement.")
            if normalize_text(metric["value"]) not in normalize_text(metric["original_text"]):
                raise EvidenceProvenanceError("Metric value must appear in its original wording.")
        return {
            "category": EvidenceCategory(data["category"]).value,
            "claim_text": claim, "source_type": source.value,
            "source_reference": data.get("source_reference"), "exact_quote": quote,
            "source_section": data.get("source_section"),
            "employer_or_project": data.get("employer_or_project"), "role": data.get("role"),
            "start_date": data.get("start_date"), "end_date": data.get("end_date"),
            "technologies": list(data.get("technologies", [])), "metrics": metrics,
            "tags": list(data.get("tags", [])),
            "external_evidence_id": data.get("external_evidence_id"),
            "source_run_id": data.get("source_run_id"),
        }

    @staticmethod
    def _add_version(session: Session, evidence_id: str, number: int, fields: dict,
                     created_by: str) -> CareerEvidenceVersionRow:
        row = CareerEvidenceVersionRow(
            evidence_version_id=str(uuid4()), evidence_id=evidence_id, version_number=number,
            category=fields["category"], claim_text=fields["claim_text"],
            source_type=fields["source_type"], source_reference=fields["source_reference"],
            exact_quote=fields["exact_quote"], source_section=fields["source_section"],
            employer_or_project=fields["employer_or_project"], role=fields["role"],
            start_date=fields["start_date"], end_date=fields["end_date"],
            technologies_json=canonical_json(fields["technologies"]),
            metrics_json=canonical_json(fields["metrics"]), tags_json=canonical_json(fields["tags"]),
            created_by=created_by, created_at=datetime.now(UTC), content_hash=_content_hash(fields),
            external_evidence_id=fields["external_evidence_id"], source_run_id=fields["source_run_id"],
        )
        session.add(row)
        return row

    def create_candidate(self, *, category: EvidenceCategory | str, claim_text: str,
                         source_type: EvidenceSourceType | str, created_by: str = "local-user",
                         original_resume_text: str | None = None, **details) -> CareerEvidence:
        fields = self._validated_fields({
            "category": category, "claim_text": claim_text,
            "source_type": source_type, **details,
        }, original_resume_text)
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = CareerEvidenceRow(
                evidence_id=str(uuid4()), status=EvidenceStatus.CANDIDATE.value,
                current_version=1, version=1, event_sequence=0,
                created_at=now, updated_at=now,
            )
            session.add(row)
            session.flush()
            version = self._add_version(session, row.evidence_id, 1, fields, created_by)
            session.flush()
            self._event(session, row, "EVIDENCE_CREATED", version.evidence_version_id)
            session.flush()
            result = self._item(session, row)
        return result

    def get(self, evidence_id: str) -> CareerEvidence:
        with self._sessions() as session:
            return self._item(session, self._require(session, evidence_id))

    def list(self, *, status: EvidenceStatus | str | None = None,
             category: EvidenceCategory | str | None = None,
             search: str | None = None, limit: int = 100) -> list[CareerEvidence]:
        with self._sessions() as session:
            query = select(CareerEvidenceRow).order_by(CareerEvidenceRow.created_at.desc(), CareerEvidenceRow.evidence_id)
            if status:
                query = query.where(CareerEvidenceRow.status == str(status))
            rows = session.scalars(query.limit(500)).all()
            items = [self._item(session, row) for row in rows]
            if category:
                items = [item for item in items if item.current.category == category]
            if search:
                term = normalize_text(search)
                items = [item for item in items if term in normalize_text(item.current.claim_text)]
            return items[:limit]

    def search(self, text: str, **filters) -> list[CareerEvidence]:
        return self.list(search=text, **filters)

    def _change(self, evidence_id: str, expected_version: int, *, allowed: set[EvidenceStatus],
                status: EvidenceStatus, event: str, reason: str | None = None) -> CareerEvidence:
        with self._sessions.begin() as session:
            row = self._require(session, evidence_id)
            if row.version != expected_version:
                raise StaleEvidenceError("The evidence version has changed.")
            if EvidenceStatus(row.status) not in allowed:
                raise InvalidEvidenceTransitionError("This evidence transition is not allowed.")
            self._advance(session, row, expected_version)
            row.status = status.value
            now = datetime.now(UTC)
            if status == EvidenceStatus.CONFIRMED:
                row.confirmed_at = now
            elif status == EvidenceStatus.REJECTED:
                row.rejected_at = now
            elif status == EvidenceStatus.ARCHIVED:
                row.archived_at = now
            elif status == EvidenceStatus.CANDIDATE:
                row.archived_at = None
            self._event(session, row, event, payload={"reason_code": "user_rejected"} if reason else None)
            session.flush()
            result = self._item(session, row)
        return result

    def confirm(self, evidence_id: str, expected_version: int) -> CareerEvidence:
        return self._change(evidence_id, expected_version, allowed={EvidenceStatus.CANDIDATE},
                            status=EvidenceStatus.CONFIRMED, event="CONFIRMED")

    def reject(self, evidence_id: str, expected_version: int, reason: str = "") -> CareerEvidence:
        return self._change(evidence_id, expected_version, allowed={EvidenceStatus.CANDIDATE},
                            status=EvidenceStatus.REJECTED, event="REJECTED", reason=reason)

    def archive(self, evidence_id: str, expected_version: int) -> CareerEvidence:
        return self._change(evidence_id, expected_version,
                            allowed={EvidenceStatus.CANDIDATE, EvidenceStatus.CONFIRMED,
                                     EvidenceStatus.REJECTED, EvidenceStatus.SUPERSEDED},
                            status=EvidenceStatus.ARCHIVED, event="ARCHIVED")

    def restore(self, evidence_id: str, expected_version: int) -> CareerEvidence:
        return self._change(evidence_id, expected_version,
                            allowed={EvidenceStatus.ARCHIVED, EvidenceStatus.REJECTED},
                            status=EvidenceStatus.CANDIDATE, event="RESTORED")

    def supersede(self, evidence_id: str, replacement_evidence_id: str,
                  expected_version: int) -> CareerEvidence:
        """Retire one confirmed item after a separately confirmed replacement exists."""
        with self._sessions.begin() as session:
            old = self._require(session, evidence_id)
            replacement = self._require(session, replacement_evidence_id)
            if old.version != expected_version:
                raise StaleEvidenceError("The evidence version has changed.")
            if old.evidence_id == replacement.evidence_id or old.status != EvidenceStatus.CONFIRMED or replacement.status != EvidenceStatus.CONFIRMED:
                raise InvalidEvidenceTransitionError("Supersession requires two distinct confirmed items.")
            self._advance(session, old, expected_version)
            old.status = EvidenceStatus.SUPERSEDED.value
            self._event(session, old, "SUPERSEDED", payload={"replacement_evidence_id": replacement_evidence_id})
            session.flush()
            result = self._item(session, old)
        return result

    def propose_revision(self, evidence_id: str, changes: dict[str, Any],
                         expected_version: int, *, original_resume_text: str | None = None,
                         created_by: str = "local-user") -> CareerEvidence:
        allowed_changes = {"category", "claim_text", "source_reference", "exact_quote",
                           "source_section", "employer_or_project", "role", "start_date",
                           "end_date", "technologies", "metrics", "tags"}
        if not changes or not set(changes) <= allowed_changes:
            raise EvidenceProvenanceError("Revision contains unsupported fields.")
        with self._sessions.begin() as session:
            row = self._require(session, evidence_id)
            if row.version != expected_version:
                raise StaleEvidenceError("The evidence version has changed.")
            if row.status != EvidenceStatus.CONFIRMED:
                raise InvalidEvidenceTransitionError("Only confirmed evidence can be revised.")
            previous = self._version(session.scalar(select(CareerEvidenceVersionRow).where(
                CareerEvidenceVersionRow.evidence_id == evidence_id,
                CareerEvidenceVersionRow.version_number == row.current_version,
            )))
            data = previous.model_dump(mode="python", exclude={"evidence_id", "evidence_version_id",
                "version_number", "created_at", "content_hash", "created_by"})
            data.update(changes)
            fields = self._validated_fields(data, original_resume_text)
            if _content_hash(fields) == previous.content_hash:
                raise InvalidEvidenceTransitionError("Revision content is unchanged.")
            self._advance(session, row, expected_version)
            row.current_version += 1
            row.status = EvidenceStatus.CANDIDATE.value
            row.confirmed_at = None
            version = self._add_version(session, evidence_id, row.current_version, fields, created_by)
            session.flush()
            self._event(session, row, "REVISION_PROPOSED", version.evidence_version_id)
            session.flush()
            result = self._item(session, row)
        return result

    def list_versions(self, evidence_id: str) -> list[EvidenceVersion]:
        with self._sessions() as session:
            self._require(session, evidence_id)
            rows = session.scalars(select(CareerEvidenceVersionRow).where(
                CareerEvidenceVersionRow.evidence_id == evidence_id
            ).order_by(CareerEvidenceVersionRow.version_number)).all()
            return [self._version(row) for row in rows]

    def list_events(self, evidence_id: str) -> list[CareerEvidenceEvent]:
        with self._sessions() as session:
            self._require(session, evidence_id)
            rows = session.scalars(select(CareerEvidenceEventRow).where(
                CareerEvidenceEventRow.evidence_id == evidence_id
            ).order_by(CareerEvidenceEventRow.sequence)).all()
            return [CareerEvidenceEvent(
                event_id=row.event_id, evidence_id=row.evidence_id, sequence=row.sequence,
                event_type=row.event_type, evidence_version_id=row.evidence_version_id,
                payload=json.loads(row.payload_json), occurred_at=_utc(row.occurred_at),
            ) for row in rows]

    def import_resume_evidence(self, analysis: ResumeAnalysis, original_resume_text: str,
                               *, source_run_id: str | None = None,
                               created_by: str = "resume_import") -> list[CareerEvidence]:
        source_ref = hashlib.sha256(normalize_text(original_resume_text).encode()).hexdigest()
        result = []
        for item in analysis.evidence:
            fields = self._validated_fields({
                "category": EvidenceCategory.EXPERIENCE,
                "claim_text": item.exact_text, "source_type": EvidenceSourceType.RESUME,
                "source_reference": source_ref, "exact_quote": item.exact_text,
                "source_section": item.source_section,
                "external_evidence_id": item.evidence_id,
                "source_run_id": source_run_id,
            }, original_resume_text)
            digest = _content_hash(fields)
            with self._sessions() as session:
                existing = session.scalar(select(CareerEvidenceVersionRow).where(
                    CareerEvidenceVersionRow.source_type == "resume",
                    CareerEvidenceVersionRow.source_reference == source_ref,
                    CareerEvidenceVersionRow.external_evidence_id == item.evidence_id,
                    CareerEvidenceVersionRow.content_hash == digest,
                ))
                if existing:
                    result.append(self._item(session, self._require(session, existing.evidence_id)))
                    continue
            created = self.create_candidate(
                category=EvidenceCategory.EXPERIENCE, claim_text=item.exact_text,
                source_type=EvidenceSourceType.RESUME, original_resume_text=original_resume_text,
                source_reference=source_ref, exact_quote=item.exact_text,
                source_section=item.source_section, external_evidence_id=item.evidence_id,
                source_run_id=source_run_id, created_by=created_by,
            )
            with self._sessions.begin() as session:
                row = self._require(session, created.evidence_id)
                self._advance(session, row, created.version)
                self._event(session, row, "SOURCE_VALIDATED", created.current.evidence_version_id,
                            {"source_type": "resume"})
            result.append(self.confirm(created.evidence_id, created.version + 1))
        return result

    def link_to_application(self, evidence_id: str, application_id: str, *,
                            expected_version: int, link_type: EvidenceLinkType = EvidenceLinkType.SUPPORTS,
                            requirement_id: str | None = None,
                            canonical_requirement: str | None = None,
                            created_by: str = "local-user") -> EvidenceApplicationLink:
        with self._sessions.begin() as session:
            row = self._require(session, evidence_id)
            if row.version != expected_version:
                raise StaleEvidenceError("The evidence version has changed.")
            if row.status != EvidenceStatus.CONFIRMED:
                raise InvalidEvidenceTransitionError("Only confirmed evidence can support an application.")
            if session.get(ApplicationRow, application_id) is None:
                raise EvidenceNotFoundError("The application was not found.")
            version = session.scalar(select(CareerEvidenceVersionRow).where(
                CareerEvidenceVersionRow.evidence_id == evidence_id,
                CareerEvidenceVersionRow.version_number == row.current_version,
            ))
            link = EvidenceApplicationLinkRow(
                link_id=str(uuid4()), evidence_id=evidence_id,
                evidence_version_id=version.evidence_version_id, application_id=application_id,
                requirement_id=requirement_id, canonical_requirement=canonical_requirement,
                link_type=EvidenceLinkType(link_type).value, created_at=datetime.now(UTC),
                created_by=created_by,
            )
            self._advance(session, row, expected_version)
            session.add(link)
            self._event(session, row, "APPLICATION_LINKED", version.evidence_version_id,
                        {"application_id": application_id, "link_id": link.link_id})
            session.flush()
            result = self._link(link)
        return result

    @staticmethod
    def _link(row: EvidenceApplicationLinkRow) -> EvidenceApplicationLink:
        return EvidenceApplicationLink(
            link_id=row.link_id, evidence_id=row.evidence_id,
            evidence_version_id=row.evidence_version_id, application_id=row.application_id,
            requirement_id=row.requirement_id, canonical_requirement=row.canonical_requirement,
            link_type=row.link_type, created_at=_utc(row.created_at), created_by=row.created_by,
        )

    def list_for_application(self, application_id: str) -> list[EvidenceApplicationLink]:
        with self._sessions() as session:
            rows = session.scalars(select(EvidenceApplicationLinkRow).where(
                EvidenceApplicationLinkRow.application_id == application_id
            ).order_by(EvidenceApplicationLinkRow.created_at)).all()
            return [self._link(row) for row in rows]

    def unlink_from_application(self, application_id: str, link_id: str, *,
                                expected_version: int) -> None:
        with self._sessions.begin() as session:
            link = session.get(EvidenceApplicationLinkRow, link_id)
            if link is None or link.application_id != application_id:
                raise EvidenceNotFoundError("The evidence link was not found.")
            row = self._require(session, link.evidence_id)
            if row.version != expected_version:
                raise StaleEvidenceError("The evidence version has changed.")
            self._advance(session, row, expected_version)
            self._event(session, row, "APPLICATION_UNLINKED", link.evidence_version_id,
                        {"application_id": application_id, "link_id": link_id})
            session.delete(link)

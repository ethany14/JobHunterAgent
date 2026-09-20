"""Transactional Interviewer persistence; no model calls inside DB transactions."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.evidence.models import CareerEvidenceRow, CareerEvidenceVersionRow, EvidenceApplicationLinkRow
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.evidence.types import EvidenceCategory, EvidenceSourceType, EvidenceStatus
from agent_runtime.interviewer.errors import (
    InterviewConflictError, InterviewNotFoundError, InterviewStaleVersionError,
    InterviewValidationError,
)
from agent_runtime.interviewer.models import (
    ApplicationRequirementAssessmentRow as AssessmentRow,
    InterviewSessionRow, InterviewTurnRow,
)
from agent_runtime.interviewer.types import (
    ApplicationRequirementAssessment, EvidenceAssessmentStatus, InterviewSession,
    InterviewStatus, InterviewTurn, InterviewTurnType,
)
from agent_runtime.security import canonical_json
from agent_runtime.workspace.models import ApplicationArtifactRow, ApplicationRow, JobSnapshotRow
from job_agent.domain import normalize_text
from job_agent.schemas import JobAnalysis, SkillMatch


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class InterviewRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    @staticmethod
    def _assessment(row: AssessmentRow) -> ApplicationRequirementAssessment:
        return ApplicationRequirementAssessment(
            assessment_id=row.assessment_id, application_id=row.application_id,
            snapshot_id=row.snapshot_id, requirement_id=row.requirement_id,
            canonical_requirement=row.canonical_requirement,
            original_requirement_text=row.original_requirement_text,
            requirement_level=row.requirement_level, match_status=row.match_status,
            evidence_status=row.evidence_status,
            linked_evidence_ids=json.loads(row.linked_evidence_ids_json),
            source_match_artifact_id=row.source_match_artifact_id, jd_order=row.jd_order,
            interview_exhausted=row.interview_exhausted,
            version=row.version, created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
        )

    @staticmethod
    def _interview(row: InterviewSessionRow) -> InterviewSession:
        return InterviewSession(
            interview_session_id=row.interview_session_id, application_id=row.application_id,
            snapshot_id=row.snapshot_id, agent_session_id=row.agent_session_id,
            status=row.status, current_assessment_id=row.current_assessment_id,
            pending_answer_turn_id=row.pending_answer_turn_id,
            pending_candidate_evidence_id=row.pending_candidate_evidence_id,
            questions_asked=row.questions_asked, max_questions=row.max_questions,
            followups_for_current_requirement=row.followups_for_current_requirement,
            max_followups_per_requirement=row.max_followups_per_requirement,
            version=row.version, turn_sequence=row.turn_sequence, error_code=row.error_code,
            created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
            completed_at=_utc(row.completed_at),
        )

    @staticmethod
    def _turn(row: InterviewTurnRow) -> InterviewTurn:
        return InterviewTurn(
            turn_id=row.turn_id, interview_session_id=row.interview_session_id,
            sequence=row.sequence, assessment_id=row.assessment_id,
            turn_type=row.turn_type, content=row.content,
            metadata=json.loads(row.metadata_json), created_at=_utc(row.created_at),
        )

    @staticmethod
    def _require(session: Session, interview_id: str) -> InterviewSessionRow:
        row = session.get(InterviewSessionRow, interview_id)
        if row is None:
            raise InterviewNotFoundError("Interview was not found.")
        return row

    @staticmethod
    def _advance(session: Session, row: InterviewSessionRow, expected_version: int) -> None:
        changed = session.execute(update(InterviewSessionRow).where(
            InterviewSessionRow.interview_session_id == row.interview_session_id,
            InterviewSessionRow.version == expected_version,
        ).values(version=expected_version + 1, updated_at=datetime.now(UTC)))
        if changed.rowcount != 1:
            raise InterviewStaleVersionError("Interview version changed.")
        session.refresh(row)

    @staticmethod
    def _append(session: Session, row: InterviewSessionRow, kind: InterviewTurnType,
                content: str, *, assessment_id: str | None = None,
                metadata: dict | None = None, idempotency_key: str | None = None) -> InterviewTurnRow:
        row.turn_sequence += 1
        turn = InterviewTurnRow(
            turn_id=str(uuid4()), interview_session_id=row.interview_session_id,
            sequence=row.turn_sequence, assessment_id=assessment_id,
            turn_type=kind.value, content=content, metadata_json=canonical_json(metadata or {}),
            idempotency_key=idempotency_key, created_at=datetime.now(UTC),
        )
        session.add(turn)
        return turn

    def prepare_assessments(self, application_id: str) -> list[ApplicationRequirementAssessment]:
        with self._factory.begin() as session:
            app = session.get(ApplicationRow, application_id)
            if app is None:
                raise InterviewNotFoundError("Application was not found.")
            snapshot = session.get(JobSnapshotRow, app.current_snapshot_id)
            artifacts = session.scalars(select(ApplicationArtifactRow).where(
                ApplicationArtifactRow.application_id == application_id,
                ApplicationArtifactRow.artifact_type.in_(["match_report", "job_analysis"]),
            ).order_by(ApplicationArtifactRow.created_at.desc(), ApplicationArtifactRow.version.desc())).all()
            match = next((a for a in artifacts if a.artifact_type == "match_report"), None)
            job = next((a for a in artifacts if a.artifact_type == "job_analysis"
                        and (match is None or a.source_run_id == match.source_run_id)), None)
            if match is None or job is None or match.created_at < snapshot.captured_at:
                raise InterviewValidationError("Analyze the current Job snapshot before interviewing.")
            analysis = JobAnalysis.model_validate_json(job.content_json)
            report = SkillMatch.model_validate_json(match.content_json)
            by_id = {entry.requirement_id: entry for entry in report.matches}
            existing = session.scalars(select(AssessmentRow).where(
                AssessmentRow.application_id == application_id,
                AssessmentRow.snapshot_id == app.current_snapshot_id,
            )).all()
            if existing:
                if any(row.source_match_artifact_id != match.artifact_id for row in existing):
                    raise InterviewConflictError("A newer match report requires a new interview assessment.")
                return [self._assessment(row) for row in sorted(existing, key=lambda r: r.jd_order)]
            seen: set[str] = set()
            now = datetime.now(UTC)
            rows = []
            for index, requirement in enumerate(analysis.requirements):
                canonical = normalize_text(requirement.canonical_name)
                if canonical in seen:
                    continue
                seen.add(canonical)
                item = by_id.get(requirement.requirement_id)
                match_status = item.match_status if item is not None else "unknown"
                if match_status == "needs_confirmation":
                    match_status = "unknown"
                row = AssessmentRow(
                    assessment_id=str(uuid4()), application_id=application_id,
                    snapshot_id=app.current_snapshot_id,
                    requirement_id=requirement.requirement_id,
                    canonical_requirement=requirement.canonical_name,
                    original_requirement_text=requirement.source_text or requirement.original_text,
                    requirement_level=requirement.level, match_status=match_status,
                    evidence_status=(EvidenceAssessmentStatus.SUFFICIENT.value if match_status == "matched"
                                     else EvidenceAssessmentStatus.NEEDS_CLARIFICATION.value),
                    linked_evidence_ids_json="[]", source_match_artifact_id=match.artifact_id,
                    jd_order=index, version=1, created_at=now, updated_at=now,
                )
                session.add(row)
                rows.append(row)
            session.flush()
            return [self._assessment(row) for row in rows]

    def create(self, application_id: str, agent_session_id: str, snapshot_id: str,
               *, max_questions: int = 10, max_followups: int = 2) -> InterviewSession:
        if not 1 <= max_questions <= 20 or not 0 <= max_followups <= 3:
            raise InterviewValidationError("Interview limits are out of range.")
        with self._factory.begin() as session:
            active = session.scalar(select(InterviewSessionRow).where(
                InterviewSessionRow.application_id == application_id,
                InterviewSessionRow.status.in_(["planning", "awaiting_answer", "awaiting_evidence_confirmation", "failed"]),
            ))
            if active:
                if active.snapshot_id != snapshot_id:
                    raise InterviewConflictError("An interview for an earlier Job snapshot is still active.")
                return self._interview(active)
            now = datetime.now(UTC)
            row = InterviewSessionRow(
                interview_session_id=str(uuid4()), application_id=application_id,
                snapshot_id=snapshot_id, agent_session_id=agent_session_id,
                status=InterviewStatus.PLANNING.value, current_assessment_id=None,
                pending_answer_turn_id=None, pending_candidate_evidence_id=None,
                questions_asked=0, max_questions=max_questions,
                followups_for_current_requirement=0,
                max_followups_per_requirement=max_followups,
                version=1, turn_sequence=0, created_at=now, updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._interview(row)

    def get(self, interview_id: str) -> InterviewSession:
        with self._factory() as session:
            return self._interview(self._require(session, interview_id))

    def active_for_application(self, application_id: str) -> InterviewSession | None:
        with self._factory() as session:
            row = session.scalar(select(InterviewSessionRow).where(
                InterviewSessionRow.application_id == application_id,
                InterviewSessionRow.status.in_(["planning", "awaiting_answer", "awaiting_evidence_confirmation", "failed"]),
            ))
            return self._interview(row) if row else None

    def assessments(self, application_id: str, snapshot_id: str) -> list[ApplicationRequirementAssessment]:
        with self._factory() as session:
            rows = session.scalars(select(AssessmentRow).where(
                AssessmentRow.application_id == application_id,
                AssessmentRow.snapshot_id == snapshot_id,
            ).order_by(AssessmentRow.jd_order)).all()
            return [self._assessment(row) for row in rows]

    def turns(self, interview_id: str, *, limit: int = 100) -> list[InterviewTurn]:
        with self._factory() as session:
            self._require(session, interview_id)
            rows = session.scalars(select(InterviewTurnRow).where(
                InterviewTurnRow.interview_session_id == interview_id,
            ).order_by(InterviewTurnRow.sequence).limit(limit)).all()
            return [self._turn(row) for row in rows]

    def append(self, interview_id: str, expected_version: int, kind: InterviewTurnType,
               content: str, *, status: InterviewStatus,
               assessment_id: str | None = None, idempotency_key: str | None = None,
               updates: dict | None = None, metadata: dict | None = None) -> tuple[InterviewSession, InterviewTurn, bool]:
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            if idempotency_key:
                previous = session.scalar(select(InterviewTurnRow).where(
                    InterviewTurnRow.interview_session_id == interview_id,
                    InterviewTurnRow.idempotency_key == idempotency_key,
                ))
                if previous:
                    if previous.turn_type != kind.value or previous.content != content:
                        raise InterviewConflictError("Idempotency key was used for a different action.")
                    return self._interview(row), self._turn(previous), True
            if row.version != expected_version:
                raise InterviewStaleVersionError("Interview version changed.")
            self._advance(session, row, expected_version)
            row.status = status.value
            for key, value in (updates or {}).items():
                if key not in {"current_assessment_id", "pending_answer_turn_id",
                               "pending_candidate_evidence_id", "questions_asked",
                               "followups_for_current_requirement", "error_code",
                               "completed_at"}:
                    raise InterviewValidationError("Unsupported interview state update.")
                if value != "$turn":
                    setattr(row, key, value)
            turn = self._append(session, row, kind, content, assessment_id=assessment_id,
                                metadata=metadata, idempotency_key=idempotency_key)
            if (updates or {}).get("pending_answer_turn_id") == "$turn":
                row.pending_answer_turn_id = turn.turn_id
            session.flush()
            return self._interview(row), self._turn(turn), False

    def change_status(self, interview_id: str, expected_version: int,
                      status: InterviewStatus, *, updates: dict | None = None,
                      idempotency_key: str | None = None, action: str | None = None) -> InterviewSession:
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            if idempotency_key and row.last_action_key == idempotency_key:
                if row.last_action_kind != action:
                    raise InterviewConflictError("Idempotency key was used for another action.")
                return self._interview(row)
            if row.version != expected_version:
                raise InterviewStaleVersionError("Interview version changed.")
            self._advance(session, row, expected_version)
            row.status = status.value
            for key, value in (updates or {}).items():
                if key not in {"current_assessment_id", "pending_answer_turn_id",
                               "pending_candidate_evidence_id", "followups_for_current_requirement",
                               "error_code", "completed_at"}:
                    raise InterviewValidationError("Unsupported interview state update.")
                setattr(row, key, value)
            row.last_action_key = idempotency_key
            row.last_action_kind = action
            session.flush()
            return self._interview(row)

    def set_assessment_status(self, assessment_id: str, status: EvidenceAssessmentStatus,
                              *, expected_version: int) -> ApplicationRequirementAssessment:
        with self._factory.begin() as session:
            row = session.get(AssessmentRow, assessment_id)
            if row is None:
                raise InterviewNotFoundError("Requirement assessment was not found.")
            changed = session.execute(update(AssessmentRow).where(
                AssessmentRow.assessment_id == assessment_id,
                AssessmentRow.version == expected_version,
            ).values(version=expected_version + 1, evidence_status=status.value,
                     updated_at=datetime.now(UTC)))
            if changed.rowcount != 1:
                raise InterviewStaleVersionError("Requirement assessment changed.")
            session.refresh(row)
            return self._assessment(row)

    def exhaust_requirement(self, interview_id: str, expected_version: int,
                            *, answer_turn_id: str) -> InterviewSession:
        """Stop asking at the bound without treating uncertainty as a user skip."""
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            key = f"follow-up-limit:{answer_turn_id}"
            prior = session.scalar(select(InterviewTurnRow).where(
                InterviewTurnRow.interview_session_id == interview_id,
                InterviewTurnRow.idempotency_key == key,
            ))
            if prior:
                return self._interview(row)
            if row.version != expected_version or not row.current_assessment_id:
                raise InterviewStaleVersionError("Interview version changed.")
            assessment = session.get(AssessmentRow, row.current_assessment_id)
            assessment.interview_exhausted = True
            assessment.version += 1
            assessment.updated_at = datetime.now(UTC)
            self._advance(session, row, expected_version)
            row.current_assessment_id = None
            row.pending_answer_turn_id = None
            row.followups_for_current_requirement = 0
            self._append(session, row, InterviewTurnType.FOLLOW_UP_LIMIT_REACHED,
                         "Clarification limit reached; requirement remains unresolved.",
                         assessment_id=assessment.assessment_id, idempotency_key=key)
            session.flush()
            return self._interview(row)

    def propose_candidate(self, interview_id: str, expected_version: int,
                          answer_turn_id: str, claim: str, quote: str,
                          *, idempotency_key: str) -> InterviewSession:
        evidence_repo = CareerEvidenceRepository(self._factory)
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            answer = session.get(InterviewTurnRow, answer_turn_id)
            if row.version != expected_version or answer is None or answer.interview_session_id != interview_id:
                raise InterviewStaleVersionError("Interview answer changed.")
            if row.pending_answer_turn_id != answer_turn_id:
                raise InterviewConflictError("This answer is not pending classification.")
            if normalize_text(quote) not in normalize_text(answer.content) or normalize_text(claim) != normalize_text(quote):
                raise InterviewValidationError("Candidate claim is not grounded in the stored answer.")
            fields = evidence_repo._validated_fields({
                "category": EvidenceCategory.EXPERIENCE, "claim_text": claim,
                "source_type": EvidenceSourceType.INTERVIEW,
                "source_reference": answer_turn_id, "exact_quote": quote,
            }, None)
            now = datetime.now(UTC)
            evidence = CareerEvidenceRow(
                evidence_id=str(uuid4()), status=EvidenceStatus.CANDIDATE.value,
                current_version=1, version=1, event_sequence=0, created_at=now, updated_at=now,
            )
            session.add(evidence)
            session.flush()
            version = evidence_repo._add_version(session, evidence.evidence_id, 1, fields, "interviewer")
            session.flush()
            evidence_repo._event(session, evidence, "EVIDENCE_CREATED", version.evidence_version_id,
                                 {"interview_turn_id": answer_turn_id})
            assessment = session.get(AssessmentRow, row.current_assessment_id)
            assessment.evidence_status = EvidenceAssessmentStatus.EVIDENCE_CANDIDATE.value
            assessment.version += 1
            assessment.updated_at = now
            self._advance(session, row, expected_version)
            row.status = InterviewStatus.AWAITING_EVIDENCE_CONFIRMATION.value
            row.pending_candidate_evidence_id = evidence.evidence_id
            row.pending_answer_turn_id = None
            self._append(session, row, InterviewTurnType.CANDIDATE_PROPOSED, claim,
                         assessment_id=assessment.assessment_id,
                         metadata={"evidence_id": evidence.evidence_id, "answer_turn_id": answer_turn_id},
                         idempotency_key=idempotency_key)
            session.flush()
            return self._interview(row)

    def confirm_candidate(self, interview_id: str, evidence_id: str,
                          expected_version: int, *, idempotency_key: str,
                          edited_claim: str | None = None) -> InterviewSession:
        evidence_repo = CareerEvidenceRepository(self._factory)
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            replay = session.scalar(select(InterviewTurnRow).where(
                InterviewTurnRow.interview_session_id == interview_id,
                InterviewTurnRow.idempotency_key == idempotency_key,
            ))
            if replay:
                if replay.turn_type != InterviewTurnType.CANDIDATE_CONFIRMED.value or replay.metadata_json != canonical_json({"evidence_id": evidence_id}):
                    raise InterviewConflictError("Idempotency key was used for another confirmation.")
                return self._interview(row)
            if row.version != expected_version:
                raise InterviewStaleVersionError("Interview version changed.")
            if row.status != InterviewStatus.AWAITING_EVIDENCE_CONFIRMATION or row.pending_candidate_evidence_id != evidence_id:
                raise InterviewConflictError("Candidate is not awaiting confirmation.")
            evidence = evidence_repo._require(session, evidence_id)
            if evidence.status != EvidenceStatus.CANDIDATE.value:
                raise InterviewConflictError("Candidate is not pending.")
            version = session.scalar(select(CareerEvidenceVersionRow).where(
                CareerEvidenceVersionRow.evidence_id == evidence_id,
                CareerEvidenceVersionRow.version_number == evidence.current_version,
            ))
            if edited_claim is not None and normalize_text(edited_claim) != normalize_text(version.claim_text):
                answer = session.get(InterviewTurnRow, version.source_reference)
                if answer is None or normalize_text(edited_claim) not in normalize_text(answer.content):
                    raise InterviewValidationError("Edited claim must be grounded in the original answer.")
                original = evidence_repo._version(version)
                fields = evidence_repo._validated_fields({
                    **original.model_dump(mode="python"),
                    "claim_text": edited_claim, "exact_quote": edited_claim,
                }, None)
                evidence_repo._advance(session, evidence, evidence.version)
                evidence.current_version += 1
                version = evidence_repo._add_version(session, evidence_id, evidence.current_version, fields, "explicit_user_edit")
                session.flush()
                evidence_repo._event(session, evidence, "REVISION_PROPOSED", version.evidence_version_id,
                                     {"source": "user_edit"})
            evidence_repo._advance(session, evidence, evidence.version)
            evidence.status = EvidenceStatus.CONFIRMED.value
            evidence.confirmed_at = datetime.now(UTC)
            evidence_repo._event(session, evidence, "CONFIRMED", version.evidence_version_id,
                                 {"source": "explicit_user_action"})
            assessment = session.get(AssessmentRow, row.current_assessment_id)
            link = EvidenceApplicationLinkRow(
                link_id=str(uuid4()), evidence_id=evidence_id,
                evidence_version_id=version.evidence_version_id,
                application_id=row.application_id,
                requirement_id=assessment.requirement_id,
                canonical_requirement=assessment.canonical_requirement,
                link_type="supports", created_at=datetime.now(UTC), created_by="interview_user_confirmation",
            )
            session.add(link)
            evidence_repo._event(session, evidence, "APPLICATION_LINKED", version.evidence_version_id,
                                 {"application_id": row.application_id, "link_id": link.link_id})
            assessment.evidence_status = EvidenceAssessmentStatus.EVIDENCE_CONFIRMED.value
            assessment.linked_evidence_ids_json = canonical_json([evidence_id])
            assessment.version += 1
            assessment.updated_at = datetime.now(UTC)
            self._advance(session, row, expected_version)
            row.status = InterviewStatus.PLANNING.value
            row.pending_candidate_evidence_id = None
            row.current_assessment_id = None
            row.followups_for_current_requirement = 0
            self._append(session, row, InterviewTurnType.CANDIDATE_CONFIRMED, "Confirmed by user.",
                         assessment_id=assessment.assessment_id,
                         metadata={"evidence_id": evidence_id}, idempotency_key=idempotency_key)
            session.flush()
            return self._interview(row)

    def resolve_requirement(self, interview_id: str, expected_version: int,
                            *, outcome: InterviewTurnType, idempotency_key: str) -> InterviewSession:
        if outcome not in {InterviewTurnType.USER_SKIPPED, InterviewTurnType.USER_CONFIRMED_GAP,
                           InterviewTurnType.CANDIDATE_REJECTED}:
            raise InterviewValidationError("Unsupported requirement resolution.")
        evidence_repo = CareerEvidenceRepository(self._factory)
        with self._factory.begin() as session:
            row = self._require(session, interview_id)
            replay = session.scalar(select(InterviewTurnRow).where(
                InterviewTurnRow.interview_session_id == interview_id,
                InterviewTurnRow.idempotency_key == idempotency_key,
            ))
            if replay:
                if replay.turn_type != outcome.value:
                    raise InterviewConflictError("Idempotency key was used for another action.")
                return self._interview(row)
            if row.version != expected_version:
                raise InterviewStaleVersionError("Interview version changed.")
            if row.status not in {InterviewStatus.AWAITING_ANSWER.value,
                                  InterviewStatus.AWAITING_EVIDENCE_CONFIRMATION.value,
                                  InterviewStatus.PLANNING.value} or not row.current_assessment_id:
                raise InterviewConflictError("No requirement is awaiting resolution.")
            if outcome == InterviewTurnType.CANDIDATE_REJECTED and not row.pending_candidate_evidence_id:
                raise InterviewConflictError("No evidence candidate is awaiting rejection.")
            assessment = session.get(AssessmentRow, row.current_assessment_id)
            if row.pending_candidate_evidence_id:
                evidence = evidence_repo._require(session, row.pending_candidate_evidence_id)
                if evidence.status == EvidenceStatus.CANDIDATE.value:
                    evidence_repo._advance(session, evidence, evidence.version)
                    evidence.status = EvidenceStatus.REJECTED.value
                    evidence.rejected_at = datetime.now(UTC)
                    evidence_repo._event(session, evidence, "REJECTED", payload={"source": "interview_user_action"})
            candidate_id = row.pending_candidate_evidence_id
            assessment.evidence_status = (
                EvidenceAssessmentStatus.CONFIRMED_GAP.value
                if outcome == InterviewTurnType.USER_CONFIRMED_GAP else
                EvidenceAssessmentStatus.SKIPPED.value
                if outcome == InterviewTurnType.USER_SKIPPED else
                EvidenceAssessmentStatus.NEEDS_CLARIFICATION.value
            )
            assessment.version += 1
            assessment.updated_at = datetime.now(UTC)
            self._advance(session, row, expected_version)
            row.status = InterviewStatus.PLANNING.value
            row.pending_candidate_evidence_id = None
            row.pending_answer_turn_id = None
            row.current_assessment_id = None if outcome != InterviewTurnType.CANDIDATE_REJECTED else assessment.assessment_id
            row.followups_for_current_requirement = 0
            self._append(session, row, outcome, "User selected this outcome.",
                         assessment_id=assessment.assessment_id, idempotency_key=idempotency_key,
                         metadata={"evidence_id": candidate_id} if candidate_id else None)
            session.flush()
            return self._interview(row)

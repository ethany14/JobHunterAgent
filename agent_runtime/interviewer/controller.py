"""Bounded, resumable application-specific evidence interview."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from uuid import uuid4

from agent_runtime.context.budget import estimate_tokens
from agent_runtime.context.projection import SessionContextProjector
from agent_runtime.context.repository import ContextSnapshotRepository
from agent_runtime.context.snapshots import (
    ContextBlockManifest, ContextSnapshot, EvidenceSnapshotRef,
)
from agent_runtime.context.types import ContextBlockKind, ContextTrustLevel
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.evidence.types import EvidenceStatus
from agent_runtime.interviewer.errors import InterviewConflictError, InterviewValidationError
from agent_runtime.interviewer.model import InterviewModelClient, SharedInterviewModel
from agent_runtime.interviewer.policy import InterviewPriorityPolicy, validate_candidate
from agent_runtime.interviewer.prompts import ANSWER_SYSTEM, INTERVIEW_PROMPT_VERSION, QUESTION_SYSTEM
from agent_runtime.interviewer.repository import InterviewRepository
from agent_runtime.interviewer.types import (
    AnswerOutcome, ApplicationRequirementAssessment, InterviewAnswerAssessment,
    InterviewQuestion, InterviewSession, InterviewStatus, InterviewTurnType,
)
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionMessageDraft, SessionState, SessionStatus
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.workspace.repository import JobWorkspaceRepository
from job_agent.domain import normalize_text


class InterviewController:
    def __init__(self, *, interviews: InterviewRepository,
                 sessions: SessionRepository, snapshots: ContextSnapshotRepository,
                 workspace: JobWorkspaceRepository, evidence: CareerEvidenceRepository,
                 model: InterviewModelClient | None = None,
                 policy: InterviewPriorityPolicy | None = None) -> None:
        self._interviews = interviews
        self._sessions = sessions
        self._snapshots = snapshots
        self._workspace = workspace
        self._evidence = evidence
        self._model = model or SharedInterviewModel()
        self._policy = policy or InterviewPriorityPolicy()

    def start(self, application_id: str, *, max_questions: int = 10,
              max_followups_per_requirement: int = 2,
              task_id: str | None = None,
              parent_session_id: str | None = None) -> InterviewSession:
        application = self._workspace.get_application(application_id)
        if task_id:
            prior = self._interviews.for_task(task_id)
            if prior:
                if prior.application_id != application_id or prior.snapshot_id != application.current_snapshot_id:
                    raise InterviewConflictError("Task interview source changed.")
                self._sync_session(prior)
                return prior
        active = self._interviews.active_for_application(application_id)
        if active:
            if active.snapshot_id != application.current_snapshot_id:
                raise InterviewConflictError("Current Job snapshot differs from the active interview.")
            if task_id and self._sessions.require(active.agent_session_id).task_id != task_id:
                raise InterviewConflictError("An independent interview already owns this Application.")
            self._sync_session(active)
            return active
        assessments = self._interviews.prepare_assessments(application_id)
        session_id = str(uuid4())
        self._sessions.create(SessionState(
            session_id=session_id, user_id="local-user", title="Evidence interview",
            status=SessionStatus.ACTIVE, allowed_tools=frozenset(),
            task_id=task_id, parent_session_id=parent_session_id,
        ), SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
            messages=[SessionMessageDraft(message=AgentMessage(
                message_id=f"interview-policy-{session_id}", role="system",
                content="Application-scoped evidence interview; tools are disabled."))])
        interview = self._interviews.create(
            application_id, session_id, application.current_snapshot_id,
            source_match_artifact_id=(assessments[0].source_match_artifact_id
                                      if assessments else None),
            max_questions=max_questions, max_followups=max_followups_per_requirement,
        )
        if interview.agent_session_id != session_id:
            # Concurrent start reused another interview; its Session is authoritative.
            self._sync_session(interview)
            return interview
        return self._advance(interview, assessments=assessments)

    def view(self, interview_id: str) -> dict:
        interview = self._interviews.get(interview_id)
        assessments = self._interviews.assessments(interview.application_id,
            interview.snapshot_id,
            source_match_artifact_id=interview.source_match_artifact_id)
        turns = self._interviews.turns(interview_id)
        current = next((a for a in assessments if a.assessment_id == interview.current_assessment_id), None)
        question = next((t for t in reversed(turns) if t.turn_type in {
            InterviewTurnType.QUESTION, InterviewTurnType.FOLLOW_UP,
        } and t.assessment_id == interview.current_assessment_id), None)
        candidate = None
        if interview.pending_candidate_evidence_id:
            evidence = self._evidence.get(interview.pending_candidate_evidence_id)
            answer_turn = next((t for t in turns if t.turn_id == evidence.current.source_reference), None)
            candidate = {
                "evidence_id": evidence.evidence_id,
                "version": evidence.version,
                "claim_text": evidence.current.claim_text,
                "category": evidence.current.category.value,
                "technologies": evidence.current.technologies,
                "metrics": [m.model_dump(mode="json") for m in evidence.current.metrics],
                "original_answer": answer_turn.content if answer_turn else "",
                "provenance": "interview",
                "warning": "This will become reusable resume evidence only after you confirm it.",
            }
        return {
            "interview": {
                "interview_session_id": interview.interview_session_id,
                "application_id": interview.application_id,
                "status": interview.status.value,
                "version": interview.version,
                "questions_asked": interview.questions_asked,
                "max_questions": interview.max_questions,
                "followups_for_current_requirement": interview.followups_for_current_requirement,
                "max_followups_per_requirement": interview.max_followups_per_requirement,
                "error_code": interview.error_code,
                "created_at": interview.created_at.isoformat(),
                "updated_at": interview.updated_at.isoformat(),
                "completed_at": interview.completed_at.isoformat() if interview.completed_at else None,
            },
            "current_assessment": self._public_assessment(current) if current else None,
            "question": question.content if interview.status == InterviewStatus.AWAITING_ANSWER and question else None,
            "candidate": candidate,
            "assessments": [self._public_assessment(a) for a in assessments],
            "turns": [{"turn_id": t.turn_id, "sequence": t.sequence,
                       "assessment_id": t.assessment_id,
                       "turn_type": t.turn_type.value, "content": t.content,
                       "created_at": t.created_at.isoformat()} for t in turns],
            "completed_requirements": sum(a.evidence_status.value in {
                "sufficient", "confirmed_gap", "evidence_confirmed", "skipped", "not_applicable",
            } for a in assessments),
            "remaining_requirements": sum(self._policy.eligible(a) for a in assessments),
        }

    @staticmethod
    def _public_assessment(item: ApplicationRequirementAssessment) -> dict:
        return {
            "assessment_id": item.assessment_id,
            "requirement_id": item.requirement_id,
            "canonical_requirement": item.canonical_requirement,
            "original_requirement_text": item.original_requirement_text,
            "requirement_level": item.requirement_level,
            "match_status": item.match_status,
            "evidence_status": item.evidence_status.value,
            "interview_exhausted": item.interview_exhausted,
            "linked_evidence_ids": item.linked_evidence_ids,
            "version": item.version,
        }

    def answer(self, interview_id: str, text: str, *, expected_version: int,
               idempotency_key: str) -> InterviewSession:
        interview = self._interviews.get(interview_id)
        answer = text.strip()
        if not answer or len(answer) > 20_000:
            raise InterviewValidationError("Answer must contain 1 to 20,000 characters.")
        if interview.status != InterviewStatus.AWAITING_ANSWER:
            # A persisted identical answer is replayable after model work advanced.
            existing = next((t for t in self._interviews.turns(interview_id)
                if t.turn_type == InterviewTurnType.USER_ANSWER and t.metadata.get("idempotency_key") == idempotency_key), None)
            if existing and existing.content == answer:
                return interview
            raise InterviewConflictError("Interview is not waiting for an answer.")
        interview, turn, replay = self._interviews.append(
            interview_id, expected_version, InterviewTurnType.USER_ANSWER, answer,
            status=InterviewStatus.PLANNING, assessment_id=interview.current_assessment_id,
            idempotency_key=idempotency_key,
            metadata={"idempotency_key": idempotency_key},
            updates={"pending_answer_turn_id": "$turn"},
        )
        self._sync_session(interview)
        if replay and interview.status != InterviewStatus.PLANNING:
            return interview
        return self._advance(interview)

    def skip(self, interview_id: str, *, expected_version: int,
             idempotency_key: str) -> InterviewSession:
        interview = self._interviews.resolve_requirement(interview_id, expected_version,
            outcome=InterviewTurnType.USER_SKIPPED, idempotency_key=idempotency_key)
        return self._advance(interview)

    def confirm_gap(self, interview_id: str, *, expected_version: int,
                    idempotency_key: str) -> InterviewSession:
        interview = self._interviews.resolve_requirement(interview_id, expected_version,
            outcome=InterviewTurnType.USER_CONFIRMED_GAP, idempotency_key=idempotency_key)
        return self._advance(interview)

    def confirm_candidate(self, interview_id: str, evidence_id: str, *,
                          expected_version: int, idempotency_key: str,
                          edited_claim: str | None = None) -> InterviewSession:
        interview = self._interviews.confirm_candidate(interview_id, evidence_id,
            expected_version, idempotency_key=idempotency_key, edited_claim=edited_claim)
        return self._advance(interview)

    def reject_candidate(self, interview_id: str, evidence_id: str, *,
                         expected_version: int, idempotency_key: str) -> InterviewSession:
        current = self._interviews.get(interview_id)
        if current.pending_candidate_evidence_id != evidence_id:
            prior = [t for t in self._interviews.turns(interview_id)
                     if t.turn_type == InterviewTurnType.CANDIDATE_REJECTED and t.metadata.get("evidence_id") == evidence_id]
            if prior:
                return current
            raise InterviewConflictError("Candidate does not belong to the pending interview.")
        interview = self._interviews.resolve_requirement(interview_id, expected_version,
            outcome=InterviewTurnType.CANDIDATE_REJECTED, idempotency_key=idempotency_key)
        return self._advance(interview)

    def cancel(self, interview_id: str, *, expected_version: int,
               idempotency_key: str) -> InterviewSession:
        current = self._interviews.get(interview_id)
        if current.status == InterviewStatus.CANCELLED:
            return current
        if current.status == InterviewStatus.COMPLETED:
            raise InterviewConflictError("A completed interview cannot be cancelled.")
        result = self._interviews.change_status(interview_id, expected_version,
            InterviewStatus.CANCELLED, idempotency_key=idempotency_key, action="cancel",
            updates={"completed_at": datetime.now(UTC)})
        self._sync_session(result)
        return result

    def resume(self, interview_id: str, *, expected_version: int,
               idempotency_key: str) -> InterviewSession:
        current = self._interviews.get(interview_id)
        if current.status in {InterviewStatus.CANCELLED, InterviewStatus.COMPLETED}:
            raise InterviewConflictError("A terminal interview cannot resume.")
        if current.status == InterviewStatus.FAILED:
            current = self._interviews.change_status(interview_id, expected_version,
                InterviewStatus.PLANNING, updates={"error_code": None},
                idempotency_key=idempotency_key, action="resume")
        elif current.version != expected_version:
            raise InterviewConflictError("Interview version changed; refresh before resuming.")
        self._sync_session(current)
        return self._advance(current) if current.status == InterviewStatus.PLANNING else current

    def _advance(self, interview: InterviewSession,
                 assessments: list[ApplicationRequirementAssessment] | None = None) -> InterviewSession:
        if interview.status != InterviewStatus.PLANNING:
            self._sync_session(interview)
            return interview
        if interview.pending_answer_turn_id:
            return self._classify_pending(interview)
        items = assessments or self._interviews.assessments(
            interview.application_id, interview.snapshot_id,
            source_match_artifact_id=interview.source_match_artifact_id)
        current = next((a for a in items if a.assessment_id == interview.current_assessment_id), None)
        target = current if current and self._policy.eligible(current) else self._policy.select(items)
        if target is None or interview.questions_asked >= interview.max_questions:
            completed = self._interviews.change_status(interview.interview_session_id,
                interview.version, InterviewStatus.COMPLETED,
                updates={"current_assessment_id": None, "completed_at": datetime.now(UTC)})
            self._sync_session(completed)
            return completed
        snapshot = None
        try:
            snapshot, context = self._prepare_context(interview, target, kind="question")
            question = InterviewQuestion.model_validate(self._model.question(target, context))
            text = self._safe_question(question, target, self._interviews.turns(interview.interview_session_id))
            followup = target.assessment_id == interview.current_assessment_id and interview.questions_asked > 0
            result, _, _ = self._interviews.append(
                interview.interview_session_id, interview.version,
                InterviewTurnType.FOLLOW_UP if followup else InterviewTurnType.QUESTION,
                text, status=InterviewStatus.AWAITING_ANSWER,
                assessment_id=target.assessment_id,
                updates={"current_assessment_id": target.assessment_id,
                    "questions_asked": interview.questions_asked + 1,
                    "followups_for_current_requirement": (
                        interview.followups_for_current_requirement + 1 if followup else 0),
                    "error_code": None},
                metadata={"question_id": question.question_id},
            )
            self._snapshots.mark_used(snapshot.snapshot_id)
            self._sync_session(result)
            return result
        except Exception:
            if snapshot:
                self._snapshots.abandon(snapshot.snapshot_id, reason="question_failed")
            failed = self._interviews.change_status(interview.interview_session_id,
                interview.version, InterviewStatus.FAILED,
                updates={"error_code": "question_generation_failed"})
            self._sync_session(failed)
            return failed

    def _classify_pending(self, interview: InterviewSession) -> InterviewSession:
        turns = self._interviews.turns(interview.interview_session_id)
        answer = next((t for t in turns if t.turn_id == interview.pending_answer_turn_id), None)
        assessment = next((a for a in self._interviews.assessments(
            interview.application_id, interview.snapshot_id,
            source_match_artifact_id=interview.source_match_artifact_id)
                           if a.assessment_id == interview.current_assessment_id), None)
        if answer is None or assessment is None:
            raise InterviewConflictError("Pending answer or requirement is unavailable.")
        snapshot = None
        try:
            snapshot, context = self._prepare_context(interview, assessment, kind="answer")
            decision = InterviewAnswerAssessment.model_validate(
                self._model.classify(assessment, answer.content, context))
            explicit_no = bool(re.search(r"\b(no experience|never used|have not used|haven't used|do not have|don't have)\b|没有相关经验|没做过", answer.content, re.IGNORECASE))
            if decision.outcome == AnswerOutcome.CONFIRMED_NO_EXPERIENCE and explicit_no:
                result = self._interviews.resolve_requirement(interview.interview_session_id,
                    interview.version, outcome=InterviewTurnType.USER_CONFIRMED_GAP,
                    idempotency_key=f"classification:{answer.turn_id}:gap")
            elif (decision.outcome == AnswerOutcome.USER_SKIPPED
                  and re.search(r"\b(skip|prefer not to answer|pass on this)\b|跳过|不想回答",
                                answer.content, re.IGNORECASE)):
                result = self._interviews.resolve_requirement(interview.interview_session_id,
                    interview.version, outcome=InterviewTurnType.USER_SKIPPED,
                    idempotency_key=f"classification:{answer.turn_id}:skip")
            elif decision.outcome == AnswerOutcome.SUFFICIENT_FOR_CANDIDATE:
                try:
                    claim, quote = validate_candidate(answer.content, decision)
                except InterviewValidationError:
                    result = self._clarify_or_skip(interview, assessment, answer.turn_id,
                                                   "candidate_validation_failed")
                else:
                    result = self._interviews.propose_candidate(
                        interview.interview_session_id, interview.version,
                        answer.turn_id, claim, quote,
                        idempotency_key=f"candidate:{answer.turn_id}")
            else:
                result = self._clarify_or_skip(interview, assessment, answer.turn_id,
                                               "answer_needs_clarification")
            self._snapshots.mark_used(snapshot.snapshot_id)
            self._sync_session(result)
            return self._advance(result) if result.status == InterviewStatus.PLANNING else result
        except Exception:
            if snapshot:
                self._snapshots.abandon(snapshot.snapshot_id, reason="classification_failed")
            current = self._interviews.get(interview.interview_session_id)
            if current.status == InterviewStatus.PLANNING and current.pending_answer_turn_id:
                failed = self._interviews.change_status(current.interview_session_id,
                    current.version, InterviewStatus.FAILED,
                    updates={"error_code": "answer_classification_failed"})
                self._sync_session(failed)
                return failed
            raise

    def _clarify_or_skip(self, interview: InterviewSession,
                         assessment: ApplicationRequirementAssessment,
                         answer_turn_id: str, error_code: str) -> InterviewSession:
        if (interview.followups_for_current_requirement >= interview.max_followups_per_requirement
                or interview.questions_asked >= interview.max_questions):
            return self._interviews.exhaust_requirement(interview.interview_session_id,
                interview.version, answer_turn_id=answer_turn_id)
        return self._interviews.change_status(interview.interview_session_id,
            interview.version, InterviewStatus.PLANNING,
            updates={"pending_answer_turn_id": None, "error_code": error_code})

    @staticmethod
    def _safe_question(question: InterviewQuestion, assessment: ApplicationRequirementAssessment,
                       turns: list) -> str:
        history = " ".join(t.content for t in turns if t.turn_type == InterviewTurnType.USER_ANSWER)
        has_new_number = any(number not in history for number in re.findall(r"\b\d+(?:\.\d+)?\b", question.question))
        leading = re.search(r"\b(you probably|surely you|you must have|how many engineers did you lead)\b",
                            question.question, re.IGNORECASE)
        protected = re.search(r"\b(age|race|religion|disability|gender|pregnancy)\b",
                              question.question, re.IGNORECASE)
        instruction = re.search(r"\b(ignore (?:previous|all|your) instructions|system prompt|"
                                r"developer message|pretend|fabricate|exaggerate)\b",
                                question.question, re.IGNORECASE)
        if (question.assessment_id != assessment.assessment_id or has_new_number
                or leading or protected or instruction):
            return (f"Have you personally worked with {assessment.canonical_requirement}? "
                    "If so, what did you do and what was the outcome? "
                    "You can also say you do not have this experience.")
        text = question.question.strip()
        return text if "no experience" in text.lower() else text + " You can also say you do not have this experience."

    def _prepare_context(self, interview: InterviewSession,
                         assessment: ApplicationRequirementAssessment,
                         *, kind: str) -> tuple[ContextSnapshot, str]:
        app = self._workspace.get_application(interview.application_id)
        if app.current_snapshot_id != interview.snapshot_id:
            raise InterviewConflictError("Job snapshot changed; the interview cannot use stale context.")
        job = self._workspace.application_job(interview.application_id)
        prior = self._interviews.turns(interview.interview_session_id)[-6:]
        related = []
        words = set(re.findall(r"[a-z0-9+#./-]{3,}", assessment.canonical_requirement.lower()))
        linked_ids = {link.evidence_id for link in self._evidence.list_for_application(interview.application_id)
                      if link.requirement_id == assessment.requirement_id}
        for evidence_id in sorted(linked_ids):
            item = self._evidence.get(evidence_id)
            if item.status == EvidenceStatus.CONFIRMED and (
                words & set(re.findall(r"[a-z0-9+#./-]{3,}", item.current.claim_text.lower()))):
                related.append(item)
        related = related[:3]
        system = QUESTION_SYSTEM if kind == "question" else ANSWER_SYSTEM
        source = (f"Application: {interview.application_id}\nJob snapshot: {interview.snapshot_id}\n"
                  f"Job title: {job.title or 'unknown'}\nCompany: {job.company or 'unknown'}\n"
                  f"Requirement: {assessment.original_requirement_text}\n"
                  f"Match status: {assessment.match_status}\n"
                  f"Related confirmed evidence: " + "; ".join(e.current.claim_text for e in related))
        context = source + "\nRecent interview turns:\n" + "\n".join(
            f"{turn.turn_type.value}: {turn.content}" for turn in prior)
        if estimate_tokens(system + context) > 3000:
            raise InterviewValidationError("Interview context exceeds its token budget.")
        messages = [
            AgentMessage(message_id=f"interview-policy-{kind}", role="system", content=system),
            AgentMessage(message_id=f"interview-task-{uuid4()}", role="user", content=context),
        ]
        blocks = [
            ContextBlockManifest(block_id="interviewer-policy", kind=ContextBlockKind.SYSTEM_POLICY,
                trust_level=ContextTrustLevel.TRUSTED_POLICY,
                content_hash=hashlib.sha256(system.encode()).hexdigest(),
                estimated_tokens=estimate_tokens(system)),
            ContextBlockManifest(block_id=f"assessment:{assessment.assessment_id}",
                kind=ContextBlockKind.REQUIRED_SOURCE_EVIDENCE,
                trust_level=ContextTrustLevel.UNTRUSTED_DATA,
                content_hash=hashlib.sha256(context.encode()).hexdigest(),
                estimated_tokens=estimate_tokens(context)),
        ]
        for item in related:
            blocks.append(ContextBlockManifest(
                block_id=f"evidence:{item.evidence_id}", kind=ContextBlockKind.CAREER_EVIDENCE,
                trust_level=ContextTrustLevel.UNTRUSTED_DATA,
                content_hash=item.current.content_hash,
                estimated_tokens=estimate_tokens(item.current.claim_text),
            ))
        snapshot = ContextSnapshot(
            session_id=interview.agent_session_id,
            system_prompt_version=INTERVIEW_PROMPT_VERSION,
            system_prompt_hash=hashlib.sha256(system.encode()).hexdigest(),
            source_artifact_ids=[interview.application_id, interview.snapshot_id,
                                 assessment.assessment_id, assessment.source_match_artifact_id],
            evidence_versions=[EvidenceSnapshotRef(
                evidence_id=item.evidence_id,
                evidence_version_id=item.current.evidence_version_id,
                version=item.current.version_number, content_hash=item.current.content_hash,
            ) for item in related],
            effective_tools=frozenset(),
            included_message_ids=[turn.turn_id for turn in prior],
            block_manifests=blocks,
            estimated_input_tokens=estimate_tokens(system + context),
            context_hash=SessionContextProjector._context_hash(messages, frozenset()),
            model_messages=messages,
        )
        self._snapshots.prepare(snapshot)
        return snapshot, context

    def _sync_session(self, interview: InterviewSession) -> None:
        """Replay durable InterviewTurns into the existing Session message store."""
        state = self._sessions.require(interview.agent_session_id)
        existing = {item.message_id for item in self._sessions.messages(state.session_id)}
        for turn in self._interviews.turns(interview.interview_session_id):
            message_id = f"interview-{turn.turn_id}"
            if message_id in existing:
                continue
            if turn.turn_type not in {InterviewTurnType.QUESTION, InterviewTurnType.FOLLOW_UP,
                                      InterviewTurnType.USER_ANSWER}:
                continue
            role = "user" if turn.turn_type == InterviewTurnType.USER_ANSWER else "assistant"
            updated = SessionState.model_validate({
                **state.model_dump(mode="python"), "status": SessionStatus.ACTIVE,
            })
            state = self._sessions.save_transition(updated,
                SessionEvent(session_id=state.session_id,
                             event_type=SessionEventType.MESSAGES_APPENDED),
                expected_version=state.version,
                messages=[SessionMessageDraft(message=AgentMessage(
                    message_id=message_id, role=role, content=turn.content))])
            existing.add(message_id)
        desired = (SessionStatus.AWAITING_USER if interview.status in {
            InterviewStatus.AWAITING_ANSWER, InterviewStatus.AWAITING_EVIDENCE_CONFIRMATION,
        } else SessionStatus.COMPLETED if interview.status == InterviewStatus.COMPLETED
            else SessionStatus.CANCELLED if interview.status == InterviewStatus.CANCELLED
            else SessionStatus.ACTIVE)
        if state.status != desired:
            updates = {"status": desired}
            if desired in {SessionStatus.COMPLETED, SessionStatus.CANCELLED}:
                updates["terminal_reason"] = "interview_finished"
            if desired == SessionStatus.CANCELLED:
                updates.update(cancel_requested=True, cancel_requested_at=datetime.now(UTC),
                               cancel_reason="User cancelled interview")
            updated = SessionState.model_validate({**state.model_dump(mode="python"), **updates})
            self._sessions.save_transition(updated,
                SessionEvent(session_id=state.session_id,
                             event_type=SessionEventType.STATUS_CHANGED),
                expected_version=state.version)

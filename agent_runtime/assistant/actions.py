"""Server-owned action registry and deterministic intent classification."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from agent_runtime.assistant.models import AssistantActionRow
from agent_runtime.assistant.repository import AssistantTimelineRepository
from agent_runtime.assistant.types import AssistantActivityType, RegisteredAssistantAction
from agent_runtime.security import canonical_json, redact_sensitive
from agent_runtime.workspace.types import ApplicationStatus


class AssistantActionConflictError(Exception):
    pass


class AssistantActionValidationError(Exception):
    pass


class AssistantActionService:
    """Dispatch only named actions to existing domain services."""
    def __init__(self, runtime, timeline: AssistantTimelineRepository):
        self.runtime = runtime
        self.timeline = timeline
        self.factory = timeline._factory

    @staticmethod
    def classify(text: str) -> RegisteredAssistantAction | None:
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        patterns = (
            (r"\b(mock interview|practice interview)\b", RegisteredAssistantAction.START_MOCK_INTERVIEW),
            (r"\b(evidence interview|improve (?:my )?evidence)\b", RegisteredAssistantAction.START_EVIDENCE_INTERVIEW),
            (r"\b(generate|create|prepare).*(application pack|cover letter|tailored resume)\b", RegisteredAssistantAction.GENERATE_APPLICATION_PACK),
            (r"\b(analy[sz]e|match).*(job|role|description)\b", RegisteredAssistantAction.ANALYZE_CURRENT_JOB),
            (r"\b(save).*(job|role|workspace)\b", RegisteredAssistantAction.SAVE_CURRENT_JOB),
            (r"\bhelp me apply\b", RegisteredAssistantAction.ANALYZE_CURRENT_JOB),
        )
        matches = [action for pattern, action in patterns if re.search(pattern, normalized)]
        return matches[0] if len(set(matches)) == 1 else None

    def execute(self, session_id: str, *, action_type: RegisteredAssistantAction | None,
                utterance: str | None, idempotency_key: str, expected_version: int,
                payload: dict) -> dict:
        state = self.runtime.sessions.require(session_id)
        if state.version != expected_version:
            raise AssistantActionConflictError("Session changed; refresh before retrying the action.")
        selected = action_type or (self.classify(utterance or ""))
        if selected is None:
            normalized_utterance = (utterance or "").strip()
            action_like = bool(re.search(
                r"(?i)\b(start|generate|create|save|analy[sz]e|approve|export|cancel|retry|apply)\b",
                normalized_utterance))
            information_request = bool(re.search(
                r"(?i)^(what|which|who|when|where|why|how|explain|compare|tell me)\b",
                normalized_utterance)) or normalized_utterance.endswith("?")
            return {"action_id": str(uuid4()), "action_type": None,
                "status": ("needs_clarification" if action_like else
                           "information_request" if information_request else
                           "normal_conversation"),
                "clarification": ("Please choose a specific action, such as analyze this job, "
                    "start an evidence interview, generate an application pack, or start a mock interview."
                    if action_like else None)}
        request_hash = hashlib.sha256(canonical_json({
            "action_type": selected.value, "payload": payload,
        }).encode()).hexdigest()
        with self.factory() as db:
            existing = db.scalar(select(AssistantActionRow).where(
                AssistantActionRow.session_id == session_id,
                AssistantActionRow.idempotency_key == idempotency_key))
            if existing:
                if existing.request_hash != request_hash:
                    raise AssistantActionConflictError("The action key was already used for different input.")
                return self._public(existing)
        result, reference_type, reference_id, status = self._dispatch(
            selected, session_id=session_id, payload=payload,
            idempotency_key=idempotency_key)
        now = datetime.now(UTC); action_id = str(uuid4())
        safe = redact_sensitive(result)
        with self.factory.begin() as db:
            db.add(AssistantActionRow(action_id=action_id, session_id=session_id,
                action_type=selected.value, idempotency_key=idempotency_key,
                request_hash=request_hash, status=status, reference_type=reference_type,
                reference_id=reference_id, safe_result_json=canonical_json(safe),
                error_code=None, created_at=now, updated_at=now))
        self.timeline.project(session_id=session_id,
            activity_type=(AssistantActivityType.ACTION_PROPOSAL
                if status in {"proposed", "approval_required"} else AssistantActivityType.RECOVERY_NOTICE
                if selected in {RegisteredAssistantAction.CANCEL_WORKFLOW,
                                RegisteredAssistantAction.RETRY_WORKFLOW}
                else AssistantActivityType.WORKFLOW_PROGRESS),
            status=status, reference_type="assistant_action", reference_id=action_id,
            payload={"action_type": selected.value, "reference_type": reference_type,
                     "reference_id": reference_id})
        return {"action_id": action_id, "action_type": selected,
            "status": status, "reference_type": reference_type,
            "reference_id": reference_id, "result": safe}

    def _dispatch(self, action, *, session_id: str, payload: dict,
                  idempotency_key: str):
        application_id = self.runtime.workspace.application_id_for_session(session_id)
        if action not in {RegisteredAssistantAction.SAVE_CURRENT_JOB,
                          RegisteredAssistantAction.ANALYZE_CURRENT_JOB} and not application_id:
            raise AssistantActionValidationError("Select or save a Job Workspace before this action.")
        key = str(payload.get("idempotency_key") or idempotency_key)
        if action == RegisteredAssistantAction.START_EVIDENCE_INTERVIEW:
            result = self.runtime.interviewer.start(application_id,
                max_questions=int(payload.get("max_questions", 10)),
                max_followups_per_requirement=int(payload.get("max_followups_per_requirement", 2)),
                parent_session_id=session_id)
            return ({"interview_id": result.interview_session_id}, "evidence_interview",
                    result.interview_session_id, "running")
        if action == RegisteredAssistantAction.SUBMIT_INTERVIEW_ANSWER:
            interview_id = self._required(payload, "interview_id")
            response_kind = payload.get("response_kind")
            if response_kind == "skip":
                result = self.runtime.interviewer.skip(interview_id,
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key)
            elif response_kind == "no_experience":
                result = self.runtime.interviewer.confirm_gap(interview_id,
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key)
            else:
                result = self.runtime.interviewer.answer(interview_id,
                    self._required(payload, "answer"),
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key)
            return ({"interview_id": interview_id, "status": result.status.value},
                    "evidence_interview", interview_id, result.status.value)
        if action == RegisteredAssistantAction.REVIEW_EVIDENCE_CANDIDATE:
            interview_id = self._required(payload, "interview_id")
            evidence_id = self._required(payload, "evidence_id")
            decision = self._required(payload, "decision")
            if decision == "confirm":
                result = self.runtime.interviewer.confirm_candidate(interview_id, evidence_id,
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key, edited_claim=payload.get("edited_claim"))
            elif decision == "reject":
                result = self.runtime.interviewer.reject_candidate(interview_id, evidence_id,
                    expected_version=int(self._required(payload, "interview_version")), idempotency_key=key)
            else:
                raise AssistantActionValidationError("Evidence decision must be confirm or reject.")
            return ({"evidence_id": evidence_id, "decision": decision}, "career_evidence",
                    evidence_id, result.status.value)
        if action == RegisteredAssistantAction.GENERATE_APPLICATION_PACK:
            application = self.runtime.workspace.get_application(application_id)
            pack = self.runtime.pack_workflow.create(application_id,
                expected_version=application.version, idempotency_key=key)
            resume_item = self.runtime.pack_workflow.generate(pack.pack_id,
                artifact_type="tailored_resume", expected_version=pack.version,
                idempotency_key=f"{key}:tailored-resume")
            current_pack = self.runtime.packs.get(pack.pack_id)
            cover_item = self.runtime.pack_workflow.generate(pack.pack_id,
                artifact_type="cover_letter", expected_version=current_pack.version,
                idempotency_key=f"{key}:cover-letter")
            status = ("awaiting_review" if all(item.status.value == "awaiting_review"
                      for item in (resume_item, cover_item)) else cover_item.status.value)
            return ({"pack_id": pack.pack_id,
                     "resume_item_id": resume_item.pack_item_id,
                     "cover_letter_item_id": cover_item.pack_item_id,
                     "artifact_types": [resume_item.artifact_type,
                                        cover_item.artifact_type]},
                    "application_pack", pack.pack_id, status)
        if action == RegisteredAssistantAction.START_MOCK_INTERVIEW:
            result = self.runtime.mock_interviewer.start(application_id,
                mode=payload.get("mode", "mixed"), difficulty=payload.get("difficulty", "standard"),
                target_question_count=int(payload.get("question_count", 5)),
                max_followups_per_question=int(payload.get("max_followups", 1)),
                idempotency_key=key, new_attempt=bool(payload.get("new_attempt", False)))
            return ({"mock_interview_id": result.mock_interview_id}, "mock_interview",
                    result.mock_interview_id, result.status.value)
        if action == RegisteredAssistantAction.SUBMIT_MOCK_INTERVIEW_ANSWER:
            interview_id = self._required(payload, "mock_interview_id")
            if payload.get("response_kind") == "skip":
                result = self.runtime.mock_interviewer.skip(interview_id,
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key)
            else:
                result = self.runtime.mock_interviewer.answer(interview_id,
                    self._required(payload, "answer"),
                    expected_version=int(self._required(payload, "interview_version")),
                    idempotency_key=key)
            return ({"mock_interview_id": interview_id}, "mock_interview", interview_id,
                    result.status.value)
        if action == RegisteredAssistantAction.REVIEW_ARTIFACT:
            artifact_id = self._required(payload, "artifact_id")
            decision = self._required(payload, "decision")
            if decision not in {"approve", "reject"}:
                raise AssistantActionValidationError(
                    "Artifact decision must be approve or reject.")
            packs = getattr(self.runtime, "packs", None)
            pack_item = packs.item_for_artifact(artifact_id) if packs else None
            if pack_item is not None:
                reviewed = packs.review(
                    pack_item.pack_id,
                    pack_item.pack_item_id,
                    expected_version=pack_item.version,
                    approve=decision == "approve",
                    idempotency_key=key,
                )
                return ({"artifact_id": artifact_id, "decision": decision,
                         "pack_id": reviewed.pack_id,
                         "pack_item_id": reviewed.pack_item_id},
                        "application_artifact", artifact_id, reviewed.status.value)
            if decision == "reject":
                raise AssistantActionValidationError(
                    "Only an Application Pack draft can be rejected here.")
            artifact = self.runtime.workspace.approve_artifact(artifact_id,
                expected_version=int(self._required(payload, "application_version")))
            return ({"artifact_id": artifact_id}, "application_artifact", artifact_id,
                    artifact.status.value)
        if action == RegisteredAssistantAction.UPDATE_APPLICATION_STATUS:
            application = self.runtime.workspace.get_application(application_id)
            changed = self.runtime.workspace.transition_status(application_id,
                target_status=ApplicationStatus(self._required(payload, "status")),
                expected_version=application.version, applied_at=None)
            return ({"application_id": application_id, "status": changed.status.value},
                    "application", application_id, "completed")
        if action in {RegisteredAssistantAction.CANCEL_WORKFLOW, RegisteredAssistantAction.RETRY_WORKFLOW}:
            task_id = self._required(payload, "task_id")
            task = self.runtime.agent_tasks.require(task_id)
            result = (self.runtime.agent_tasks.cancel(task_id, expected_version=task.version, subtree=True)
                      if action == RegisteredAssistantAction.CANCEL_WORKFLOW
                      else self.runtime.agent_tasks.retry(task_id, expected_version=task.version))
            return ({"task_id": task_id}, "agent_task", task_id, result.status.value)
        if action == RegisteredAssistantAction.REVIEW_LEARNING_CANDIDATE:
            candidate_id = self._required(payload, "candidate_id")
            owner = self.runtime.owner_resolver.resolve().owner_id
            reviewed = self.runtime.feedback.review(candidate_id, owner_id=owner,
                action=self._required(payload, "decision"),
                expected_version=int(self._required(payload, "candidate_version")),
                idempotency_key=key, content=payload.get("edited_content"))
            return ({"candidate_id": candidate_id}, "learning_candidate", candidate_id,
                    reviewed.status.value)
        if action in {RegisteredAssistantAction.EXPORT_ARTIFACT,
                      RegisteredAssistantAction.STORE_EXPORT_VIA_MCP}:
            artifact_id = self._required(payload, "artifact_id")
            # Export presentation is registered, but filesystem/MCP writes remain approval-bound.
            return ({"artifact_id": artifact_id, "requires_approval":
                    action == RegisteredAssistantAction.STORE_EXPORT_VIA_MCP},
                    "application_artifact", artifact_id,
                    "approval_required" if action == RegisteredAssistantAction.STORE_EXPORT_VIA_MCP else "proposed")
        if action in {RegisteredAssistantAction.ANALYZE_CURRENT_JOB,
                      RegisteredAssistantAction.SAVE_CURRENT_JOB}:
            return ({"action": action.value, "use_existing_domain_endpoint": True},
                    "application" if application_id else None, application_id, "proposed")
        raise AssistantActionValidationError("The registered action is unavailable in the current state.")

    @staticmethod
    def _required(payload: dict, key: str):
        value = payload.get(key)
        if value is None or value == "":
            raise AssistantActionValidationError(f"Action field '{key}' is required.")
        return value

    @staticmethod
    def _public(row: AssistantActionRow) -> dict:
        return {"action_id": row.action_id, "action_type": row.action_type,
            "status": row.status, "reference_type": row.reference_type,
            "reference_id": row.reference_id, "result": json.loads(row.safe_result_json)}

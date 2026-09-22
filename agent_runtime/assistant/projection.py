"""Idempotently project durable domain references into one Assistant timeline."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from agent_runtime.assistant.repository import AssistantTimelineRepository
from agent_runtime.assistant.types import AssistantActivityType
from agent_runtime.interviewer.models import InterviewSessionRow, InterviewTurnRow
from agent_runtime.evidence.models import CareerEvidenceRow, CareerEvidenceVersionRow
from agent_runtime.application_pack.models import ApplicationPackItemRow, ApplicationPackRow
from agent_runtime.application_pack.rendering import (
    render_application_answer, render_cover_letter, render_interview_report,
)
from agent_runtime.feedback.models import (
    FeedbackEventRow, LearningCandidateEventLinkRow, LearningCandidateRow,
)
from agent_runtime.mock_interview.models import (
    MockAnswerEvaluationRow, MockInterviewAnswerRow, MockInterviewQuestionRow,
    MockInterviewCandidateRow, MockInterviewReportRow, MockInterviewRow,
)
from agent_runtime.models import ToolCallRow
from agent_runtime.multi_agent.models import AgentTaskRow
from agent_runtime.sessions.models import AgentSessionMessageRow
from agent_runtime.tools.messages import AgentMessage
from job_agent.rendering import render_tailored_resume
from agent_runtime.workspace.models import (
    ApplicationArtifactRow, ApplicationSessionRow,
)


class AssistantTimelineProjector:
    def __init__(self, repository: AssistantTimelineRepository):
        self._repository = repository
        self._factory = repository._factory

    def refresh(self, session_id: str) -> None:
        """Project safe references. Existing references update without new sequences."""
        with self._factory() as db:
            messages = db.scalars(select(AgentSessionMessageRow).where(
                AgentSessionMessageRow.session_id == session_id).order_by(
                AgentSessionMessageRow.sequence)).all()
            application_link = db.scalar(select(ApplicationSessionRow).where(
                ApplicationSessionRow.session_id == session_id))
            application_id = application_link.application_id if application_link else None
            tasks = db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.parent_session_id == session_id).order_by(
                AgentTaskRow.created_at, AgentTaskRow.task_id)).all()
            tools = db.scalars(select(ToolCallRow).where(
                ToolCallRow.scope_type == "session", ToolCallRow.scope_id == session_id).order_by(
                ToolCallRow.created_at, ToolCallRow.call_id)).all()
            artifacts = (db.scalars(select(ApplicationArtifactRow).where(
                ApplicationArtifactRow.application_id == application_id).order_by(
                ApplicationArtifactRow.created_at, ApplicationArtifactRow.artifact_id)).all()
                if application_id else [])
            packs = (db.scalars(select(ApplicationPackRow).where(
                ApplicationPackRow.application_id == application_id)).all()
                if application_id else [])
            pack_ids = [row.pack_id for row in packs]
            pack_items = (db.scalars(select(ApplicationPackItemRow).where(
                ApplicationPackItemRow.pack_id.in_(pack_ids))).all() if pack_ids else [])
            learning_candidates = db.scalars(select(LearningCandidateRow).join(
                LearningCandidateEventLinkRow,
                LearningCandidateEventLinkRow.candidate_id == LearningCandidateRow.candidate_id
            ).join(FeedbackEventRow,
                FeedbackEventRow.feedback_event_id ==
                LearningCandidateEventLinkRow.feedback_event_id
            ).where(FeedbackEventRow.session_id == session_id).distinct()).all()
            evidence_interview = (db.scalar(select(InterviewSessionRow).where(
                InterviewSessionRow.application_id == application_id).order_by(
                InterviewSessionRow.created_at.desc())) if application_id else None)
            interview_turns = (db.scalars(select(InterviewTurnRow).where(
                InterviewTurnRow.interview_session_id == evidence_interview.interview_session_id
            ).order_by(InterviewTurnRow.sequence)).all() if evidence_interview else [])
            pending_evidence = None
            if evidence_interview and evidence_interview.pending_candidate_evidence_id:
                pending_row = db.get(CareerEvidenceRow,
                    evidence_interview.pending_candidate_evidence_id)
                if pending_row:
                    pending_evidence = db.scalar(select(CareerEvidenceVersionRow).where(
                        CareerEvidenceVersionRow.evidence_id == pending_row.evidence_id,
                        CareerEvidenceVersionRow.version_number == pending_row.current_version))
            mock = (db.scalar(select(MockInterviewRow).where(
                MockInterviewRow.application_id == application_id).order_by(
                MockInterviewRow.started_at.desc())) if application_id else None)
            mock_questions = (db.scalars(select(MockInterviewQuestionRow).where(
                MockInterviewQuestionRow.mock_interview_id == mock.mock_interview_id).order_by(
                MockInterviewQuestionRow.created_at)).all() if mock else [])
            mock_answers = (db.scalars(select(MockInterviewAnswerRow).where(
                MockInterviewAnswerRow.mock_interview_id == mock.mock_interview_id).order_by(
                MockInterviewAnswerRow.sequence)).all() if mock else [])
            answer_ids = [row.answer_id for row in mock_answers]
            feedback = (db.scalars(select(MockAnswerEvaluationRow).where(
                MockAnswerEvaluationRow.answer_id.in_(answer_ids))).all() if answer_ids else [])
            report = (db.scalar(select(MockInterviewReportRow).where(
                MockInterviewReportRow.mock_interview_id == mock.mock_interview_id)) if mock else None)
            mock_candidates = (db.scalars(select(MockInterviewCandidateRow).where(
                MockInterviewCandidateRow.mock_interview_id == mock.mock_interview_id)).all()
                if mock else [])
        for row in messages:
            message = AgentMessage.model_validate_json(row.message_json)
            if message.role not in {"user", "assistant"}:
                continue
            kind = (AssistantActivityType.USER_MESSAGE if message.role == "user"
                    else AssistantActivityType.ASSISTANT_MESSAGE)
            self._repository.project(session_id=session_id, activity_type=kind,
                status="completed", reference_type="session_message",
                reference_id=row.message_id, payload={"content": message.content})
        for task in tasks:
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.WORKFLOW_PROGRESS,
                status=task.status, reference_type="agent_task", reference_id=task.task_id,
                payload={"label": task.agent_role, "task_type": task.task_type,
                    "attempt": task.attempt_count, "error_code": task.error_code})
        for tool in tools:
            provider = "Built-in"; server_id = None; truncated = False
            if tool.result_json:
                try:
                    result = json.loads(tool.result_json)
                    provenance = result.get("provenance") or []
                    mcp = next((item for item in provenance
                        if item.get("metadata", {}).get("server_id")), None)
                    if mcp:
                        provider = "MCP"; server_id = mcp.get("metadata", {}).get("server_id")
                    truncated = bool(result.get("output", {}).get("result_truncated", False))
                except (TypeError, ValueError):
                    pass
            duration = max(0, int((tool.updated_at - tool.created_at).total_seconds() * 1000))
            self._repository.project(session_id=session_id,
                activity_type=(AssistantActivityType.APPROVAL if tool.status == "approval_required"
                               else AssistantActivityType.TOOL_CALL), status=tool.status,
                reference_type="tool_call", reference_id=tool.call_id,
                payload={"tool_name": tool.tool_name, "provider": provider,
                    "mcp_server_id": server_id, "duration_ms": duration,
                    "approval_required": tool.status == "approval_required",
                    "approval_status": "approved" if tool.approval_arguments_hash else
                        "required" if tool.status == "approval_required" else "not_required",
                    "result_truncated": truncated, "error_code": tool.error_code})
        latest_artifacts: dict[str, ApplicationArtifactRow] = {}
        for artifact in artifacts:
            current = latest_artifacts.get(artifact.artifact_type)
            if current is None or artifact.version >= current.version:
                latest_artifacts[artifact.artifact_type] = artifact

        analysis_text = self._analysis_summary(
            latest_artifacts.get("job_analysis"),
            latest_artifacts.get("match_report"),
        )
        if analysis_text and application_id:
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.ASSISTANT_MESSAGE,
                status="completed", reference_type="application_analysis",
                reference_id=application_id, payload={"content": analysis_text})

        public_artifact_types = {
            "tailored_resume", "cover_letter", "application_answer", "interview_report",
        }
        pack_status_by_artifact = {
            item.artifact_id: item.status
            for item in pack_items
            if item.artifact_id
        }
        for artifact in latest_artifacts.values():
            if artifact.artifact_type not in public_artifact_types:
                continue
            try:
                artifact_content = json.loads(artifact.content_json)
            except (TypeError, ValueError):
                artifact_content = {}
            self._repository.project(session_id=session_id,
                activity_type=(AssistantActivityType.VERIFICATION
                    if artifact.artifact_type == "verification_report"
                    else AssistantActivityType.ARTIFACT),
                status=pack_status_by_artifact.get(artifact.artifact_id, artifact.status),
                reference_type="application_artifact", reference_id=artifact.artifact_id,
                payload={"artifact_type": artifact.artifact_type, "version": artifact.version,
                    "verification_status": artifact.verification_status,
                    "preview": self._artifact_preview(
                        artifact.artifact_type, artifact_content)})
        for item in pack_items:
            if not item.verification_json:
                continue
            try:
                verification = json.loads(item.verification_json)
            except (TypeError, ValueError):
                verification = {}
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.VERIFICATION, status=item.status,
                reference_type="pack_item_verification", reference_id=item.pack_item_id,
                payload={"artifact_type": item.artifact_type,
                    "passed": verification.get("passed"),
                    "issue_count": len(verification.get("issues") or []),
                    "version": item.version})
        for turn in interview_turns:
            is_current_question = bool(
                evidence_interview
                and evidence_interview.status == "awaiting_answer"
                and evidence_interview.pending_answer_turn_id == turn.turn_id
            )
            kind = (AssistantActivityType.QUESTION if is_current_question
                    else AssistantActivityType.ASSISTANT_MESSAGE
                    if turn.turn_type in {"question", "follow_up"}
                    else AssistantActivityType.EVIDENCE_CANDIDATE
                    if "candidate" in turn.turn_type else AssistantActivityType.USER_MESSAGE
                    if turn.turn_type == "user_answer" else None)
            if kind:
                self._repository.project(session_id=session_id, activity_type=kind,
                    status=(evidence_interview.status if is_current_question else "completed"),
                    reference_type="interview_turn",
                    reference_id=turn.turn_id, payload={"content": turn.content,
                        "interview_id": turn.interview_session_id,
                        "interview_version": evidence_interview.version})
        if evidence_interview and evidence_interview.pending_candidate_evidence_id:
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.EVIDENCE_CANDIDATE,
                status=evidence_interview.status,
                reference_type="career_evidence",
                reference_id=evidence_interview.pending_candidate_evidence_id,
                payload={"content": pending_evidence.claim_text if pending_evidence else None,
                    "interview_id": evidence_interview.interview_session_id,
                    "interview_version": evidence_interview.version})
        for answer in mock_answers:
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.USER_MESSAGE, status="completed",
                reference_type="mock_answer", reference_id=answer.answer_id,
                payload={"content": answer.original_text})
        for question in mock_questions:
            content = json.loads(question.content_json)
            is_current_question = bool(
                mock.status == "awaiting_answer"
                and mock.current_question_id == question.question_id
            )
            self._repository.project(session_id=session_id,
                activity_type=(AssistantActivityType.QUESTION if is_current_question
                               else AssistantActivityType.ASSISTANT_MESSAGE),
                status=(mock.status if is_current_question else "completed"),
                reference_type="mock_question", reference_id=question.question_id,
                payload={"content": self._mock_question_text(content),
                    "interview_id": mock.mock_interview_id, "interview_version": mock.version})
        for item in feedback:
            try:
                feedback_content = json.loads(item.content_json)
            except (TypeError, ValueError):
                feedback_content = {}
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.INTERVIEW_FEEDBACK, status="completed",
                reference_type="mock_feedback", reference_id=item.evaluation_id,
                payload={"content": self._preview(feedback_content),
                    "interview_id": mock.mock_interview_id, "answer_id": item.answer_id})
        for candidate in mock_candidates:
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.EVIDENCE_CANDIDATE,
                status=mock.status, reference_type="mock_evidence_candidate",
                reference_id=candidate.evidence_id,
                payload={"interview_id": mock.mock_interview_id})
        if report:
            try:
                report_content = json.loads(report.content_json)
            except (TypeError, ValueError):
                report_content = {}
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.ARTIFACT, status="completed",
                reference_type="mock_interview_report", reference_id=report.report_id,
                payload={"artifact_type": "interview_report",
                    "preview": self._preview(report_content),
                    "interview_id": mock.mock_interview_id})
        for candidate in learning_candidates:
            try:
                candidate_state = json.loads(candidate.state_json)
            except (TypeError, ValueError):
                candidate_state = {}
            self._repository.project(session_id=session_id,
                activity_type=AssistantActivityType.LEARNING_CANDIDATE,
                status=candidate.status, reference_type="learning_candidate",
                reference_id=candidate.candidate_id,
                payload={"content": self._preview(candidate_state),
                    "candidate_type": candidate.candidate_type,
                    "canonical_key": candidate.canonical_key, "scope": candidate.scope,
                    "version": candidate.version})

    @staticmethod
    def _analysis_summary(job_row: ApplicationArtifactRow | None,
                          match_row: ApplicationArtifactRow | None) -> str | None:
        if match_row is None:
            return None
        try:
            match = json.loads(match_row.content_json)
            job = json.loads(job_row.content_json) if job_row else {}
        except (TypeError, ValueError):
            return None
        title = str(job.get("title") or "this role").strip()
        score = match.get("overall_score")
        matches = match.get("matches") or []
        strengths = [str(item.get("job_skill") or item.get("display_name") or "").strip()
                     for item in matches if item.get("match_status") == "matched"]
        strengths = [item for item in strengths if item][:3]
        missing = match.get("missing_required_requirements") or []
        gaps = [str(item.get("display_name") or item.get("original_text")
                    or item.get("canonical_name") or "").strip()
                for item in missing if isinstance(item, dict)]
        gaps = [item for item in gaps if item][:3]
        sentences = [f"I analyzed your background against {title}."]
        if isinstance(score, (int, float)):
            sentences.append(
                f"Your current evidence covers about {score:.0f}% of the scored requirements.")
        if strengths:
            sentences.append(
                "Your strongest supported matches are " + ", ".join(strengths) + ".")
        if gaps:
            sentences.append(
                "The main areas without enough resume evidence are " + ", ".join(gaps) + ".")
        elif not strengths:
            sentences.append("Open the latest analysis to review the requirement matches.")
        return " ".join(sentences)

    @staticmethod
    def _mock_question_text(content: dict) -> str:
        """Read both the current contract and older persisted question shapes."""
        return str(
            content.get("question_text")
            or content.get("question")
            or content.get("text")
            or "Interview question"
        )

    @classmethod
    def _artifact_preview(cls, artifact_type: str, content: dict,
                          limit: int = 280) -> str:
        try:
            if artifact_type == "cover_letter":
                rendered = render_cover_letter(content)
            elif artifact_type == "application_answer":
                rendered = render_application_answer(content)
            elif artifact_type == "tailored_resume":
                rendered = render_tailored_resume(content)
            elif artifact_type == "interview_report":
                rendered = render_interview_report(content)
            else:
                return ""
        except (TypeError, ValueError):
            return ""
        flattened = " ".join(rendered.split())
        return flattened if len(flattened) <= limit else f"{flattened[:limit - 1].rstrip()}…"

    @classmethod
    def _preview(cls, value: Any, limit: int = 280) -> str:
        parts: list[str] = []

        def collect(item: Any) -> None:
            if sum(len(part) for part in parts) >= limit:
                return
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, list):
                for child in item:
                    collect(child)
            elif isinstance(item, dict):
                for child in item.values():
                    collect(child)

        collect(value)
        text = " ".join(parts)
        return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"

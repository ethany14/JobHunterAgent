"""Safe presentation contracts for the conversation timeline."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field

from agent_runtime.types import RuntimeModel


class AssistantActivityType(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    ACTION_PROPOSAL = "action_proposal"
    WORKFLOW_PROGRESS = "workflow_progress"
    QUESTION = "question"
    EVIDENCE_CANDIDATE = "evidence_candidate"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"
    TOOL_CALL = "tool_call"
    APPROVAL = "approval"
    INTERVIEW_FEEDBACK = "interview_feedback"
    LEARNING_CANDIDATE = "learning_candidate"
    ERROR = "error"
    RECOVERY_NOTICE = "recovery_notice"


class AssistantActivity(RuntimeModel):
    activity_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    sequence: int = Field(default=0, ge=0)
    type: AssistantActivityType
    status: str = Field(max_length=64)
    reference_type: str = Field(max_length=64)
    reference_id: str = Field(max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RegisteredAssistantAction(StrEnum):
    ANALYZE_CURRENT_JOB = "analyze_current_job"
    SAVE_CURRENT_JOB = "save_current_job"
    START_EVIDENCE_INTERVIEW = "start_evidence_interview"
    SUBMIT_INTERVIEW_ANSWER = "submit_interview_answer"
    REVIEW_EVIDENCE_CANDIDATE = "review_evidence_candidate"
    GENERATE_APPLICATION_PACK = "generate_application_pack"
    REVIEW_ARTIFACT = "review_artifact"
    START_MOCK_INTERVIEW = "start_mock_interview"
    SUBMIT_MOCK_INTERVIEW_ANSWER = "submit_mock_interview_answer"
    EXPORT_ARTIFACT = "export_artifact"
    STORE_EXPORT_VIA_MCP = "store_export_via_mcp"
    UPDATE_APPLICATION_STATUS = "update_application_status"
    REVIEW_LEARNING_CANDIDATE = "review_learning_candidate"
    CANCEL_WORKFLOW = "cancel_workflow"
    RETRY_WORKFLOW = "retry_workflow"

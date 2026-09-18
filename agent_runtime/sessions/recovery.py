"""Pure deterministic planning for persisted session recovery."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from agent_runtime.sessions.state import SessionState, SessionStatus
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.types import (
    RuntimeModel,
    ToolCallRecord,
    ToolExecutionStatus,
    ToolRiskLevel,
    ToolSideEffect,
)


class RecoveryAction(StrEnum):
    RETURN_STABLE = "return_stable"
    CONTINUE_MODEL = "continue_model"
    PROCESS_TOOL = "process_tool"
    AWAIT_TOOL_APPROVAL = "await_tool_approval"
    REPLAY_TOOL_RESULT = "replay_tool_result"
    APPEND_TOOL_FAILURE = "append_tool_failure"
    RETRY_EXPIRED_TOOL = "retry_expired_tool"
    MARK_OUTCOME_UNKNOWN = "mark_outcome_unknown"
    ACTIVE_TOOL_LEASE = "active_tool_lease"
    FINALIZE_ASSISTANT = "finalize_assistant"
    RESTORE_PENDING_TOOLS = "restore_pending_tools"


class SessionRecoveryPlan(RuntimeModel):
    action: RecoveryAction
    reason_code: str
    tool_call_id: str | None = None


class SessionRecoveryPlanner:
    """Choose a recovery action from persisted facts only."""

    @staticmethod
    def plan(
        state: SessionState,
        *,
        tool_record: ToolCallRecord | None = None,
        tool_message_persisted: bool = False,
        last_message: AgentMessage | None = None,
        now: datetime,
    ) -> SessionRecoveryPlan:
        if state.status != SessionStatus.RUNNING:
            return SessionRecoveryPlan(
                action=RecoveryAction.RETURN_STABLE, reason_code="session_not_running"
            )
        if not state.pending_tool_call_ids:
            if last_message is not None and last_message.role == "assistant":
                if last_message.tool_calls:
                    return SessionRecoveryPlan(
                        action=RecoveryAction.RESTORE_PENDING_TOOLS,
                        reason_code="assistant_tool_calls_require_processing",
                    )
                return SessionRecoveryPlan(
                    action=RecoveryAction.FINALIZE_ASSISTANT,
                    reason_code="final_assistant_message_already_persisted",
                )
            return SessionRecoveryPlan(
                action=RecoveryAction.CONTINUE_MODEL,
                reason_code="last_persisted_message_requires_model_decision",
            )
        if tool_record is None:
            return SessionRecoveryPlan(
                action=RecoveryAction.PROCESS_TOOL,
                reason_code="tool_not_requested",
            )
        call_id = tool_record.call_id
        status = tool_record.status
        if tool_message_persisted:
            return SessionRecoveryPlan(
                action=RecoveryAction.PROCESS_TOOL,
                reason_code="advance_past_persisted_tool_message",
                tool_call_id=call_id,
            )
        if status == ToolExecutionStatus.APPROVAL_REQUIRED:
            return SessionRecoveryPlan(
                action=RecoveryAction.AWAIT_TOOL_APPROVAL,
                reason_code="persisted_approval_required",
                tool_call_id=call_id,
            )
        if status == ToolExecutionStatus.COMPLETED:
            return SessionRecoveryPlan(
                action=RecoveryAction.REPLAY_TOOL_RESULT,
                reason_code="completed_tool_message_missing",
                tool_call_id=call_id,
            )
        if status in {
            ToolExecutionStatus.FAILED,
            ToolExecutionStatus.DENIED,
            ToolExecutionStatus.TIMED_OUT,
            ToolExecutionStatus.OUTCOME_UNKNOWN,
        }:
            return SessionRecoveryPlan(
                action=RecoveryAction.APPEND_TOOL_FAILURE,
                reason_code=f"persisted_{status.value}",
                tool_call_id=call_id,
            )
        if status == ToolExecutionStatus.RUNNING:
            lease = tool_record.execution_lease_until
            if lease is not None and lease > now:
                return SessionRecoveryPlan(
                    action=RecoveryAction.ACTIVE_TOOL_LEASE,
                    reason_code="tool_execution_lease_active",
                    tool_call_id=call_id,
                )
            retry_safe = (
                tool_record.risk_level == ToolRiskLevel.READ_ONLY
                and tool_record.side_effect == ToolSideEffect.NONE
                and tool_record.idempotent is True
            )
            return SessionRecoveryPlan(
                action=(
                    RecoveryAction.RETRY_EXPIRED_TOOL
                    if retry_safe
                    else RecoveryAction.MARK_OUTCOME_UNKNOWN
                ),
                reason_code=(
                    "expired_safe_read_execution"
                    if retry_safe
                    else "expired_uncertain_execution"
                ),
                tool_call_id=call_id,
            )
        return SessionRecoveryPlan(
            action=RecoveryAction.PROCESS_TOOL,
            reason_code=f"persisted_{status.value}_can_continue",
            tool_call_id=call_id,
        )

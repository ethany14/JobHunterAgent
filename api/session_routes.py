"""HTTP routes for durable custom-agent sessions."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from agent_runtime.sessions.outcome import SessionOutcome
from agent_runtime.sessions.outcome import SessionOutcomeStatus
from agent_runtime.errors import UnknownToolError
from agent_runtime.sessions.state import SessionMessageVisibility, SessionState, SessionStatus
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.types import (
    ToolDataClassification,
    ToolExecutionStatus,
    ToolSideEffect,
)
from api.session_dependencies import (
    CAPABILITY_PROFILES,
    CAPABILITY_SKILL_PROFILES,
    SessionRuntime,
    get_session_runtime,
)
from api.session_schemas import (
    CancelSessionRequest,
    CreateSessionRequest,
    PendingToolApproval,
    PublicSession,
    PublicSessionMessage,
    PublicToolCall,
    SessionMessagesResponse,
    SessionListResponse,
    SessionResponse,
    SessionSummary,
    SubmitMessageRequest,
    VersionedMutationRequest,
)
from agent_runtime.mcp.types import McpToolProvenance
from agent_runtime.skills.evolution_types import ActivationMode
from api.services.run_service import RunNotFoundError


def _require_public_session(runtime: SessionRuntime, session_id: str) -> SessionState:
    if runtime.sessions.is_archived(session_id):
        from agent_runtime.sessions.errors import SessionNotFoundError
        raise SessionNotFoundError(f"Session '{session_id}' was not found.")
    state = runtime.sessions.require(session_id)
    if state.task_id is not None:
        raise HTTPException(status_code=403, detail={
            "code": "child_session_private", "message": "Child task sessions are private."})
    return state

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _schedule_conversation_learning(
    background_tasks: BackgroundTasks,
    runtime: SessionRuntime,
    outcome: SessionOutcome,
) -> None:
    if (
        runtime.conversation_learning is None
        or outcome.status != SessionOutcomeStatus.RESPONSE_READY
    ):
        return
    assistant = next((item.message for item in reversed(
        runtime.sessions.messages(outcome.session_id))
        if item.message.role == "assistant" and not item.message.tool_calls), None)
    if assistant is not None:
        background_tasks.add_task(
            runtime.conversation_learning.observe_completed_turn,
            session_id=outcome.session_id,
            assistant_message_id=assistant.message_id,
        )


def _profile_for(runtime: SessionRuntime, state: SessionState) -> str:
    for name, base_tools in CAPABILITY_PROFILES.items():
        extras = state.allowed_tools - base_tools
        if (
            state.allowed_tools == runtime.capability_tools(name)
            or (
                base_tools <= state.allowed_tools
                and all(tool.startswith("mcp__") for tool in extras)
            )
        ):
            return name
    # Sessions created by this API always have a known server profile. Refuse to
    # expose a guessed profile for legacy/manual state.
    raise ValueError("The session capability profile is not supported by this API.")


def _public_session(runtime: SessionRuntime, state: SessionState) -> PublicSession:
    return PublicSession(
        session_id=state.session_id,
        title=state.title,
        capability_profile=_profile_for(runtime, state),
        status=state.status,
        version=state.version,
        active_run_id=state.active_run_id,
        active_application_id=(
            runtime.workspace.application_id_for_session(state.session_id)
            if runtime.workspace is not None else None
        ),
        loop_iteration=state.loop_iteration,
        executed_tool_calls=state.executed_tool_calls,
        total_input_tokens=state.total_input_tokens,
        total_output_tokens=state.total_output_tokens,
        error_code=state.error_code,
        error_message=state.error_message,
        cancel_requested=state.cancel_requested,
        turn_deadline_at=state.turn_deadline_at,
        session_expires_at=state.session_expires_at,
        terminal_reason=state.terminal_reason,
        manual_recovery_tool_call_ids=state.manual_recovery_tool_call_ids,
        recovery_available=(
            state.status == SessionStatus.RUNNING
            and runtime.claims.active_claim(state.session_id) is None
        ),
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def _pending_approvals(runtime: SessionRuntime, state: SessionState) -> list[PendingToolApproval]:
    if state.status != SessionStatus.AWAITING_TOOL_APPROVAL:
        return []
    records = runtime.tool_calls.list_for_scope("session", state.session_id)
    approvals: list[PendingToolApproval] = []
    for record in records:
        if record.status != ToolExecutionStatus.APPROVAL_REQUIRED:
            continue
        try:
            tool = runtime.registry.get(record.request.tool_name)
        except UnknownToolError:
            tool = None
        approvals.append(
            PendingToolApproval(
                call_id=record.call_id,
                tool_name=record.request.tool_name,
                tool_version=record.tool_version,
                description=(
                    tool.description
                    if tool is not None
                    else "This tool is currently unavailable."
                ),
                side_effect=record.side_effect or getattr(
                    tool, "side_effect", ToolSideEffect.NONE
                ),
                data_classification=getattr(
                    tool, "data_classification", ToolDataClassification.INTERNAL
                ),
                # Persisted ToolCallRecord arguments are the repository's redacted copy.
                arguments=record.request.arguments,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
    approvals.sort(key=lambda item: item.created_at)
    return approvals


def _public_tool_calls(runtime: SessionRuntime, state: SessionState) -> list[PublicToolCall]:
    terminal_events = {
        "completed", "failed", "tool_reported_error", "timed_out",
        "outcome_unknown", "denied", "user_rejected",
    }
    output: list[PublicToolCall] = []
    for record in runtime.tool_calls.list_for_scope("session", state.session_id):
        events = runtime.tool_calls.list_events(record.call_id)
        requested = next((item for item in events if item.event_type == "requested"), None)
        metadata = dict(requested.payload) if requested else {}
        mcp = None
        if metadata.get("provider") == "mcp":
            try:
                mcp = McpToolProvenance.model_validate({
                    key: metadata.get(key)
                    for key in ("server_id", "remote_tool_name", "transport", "public_tool_name")
                })
            except Exception:
                mcp = None
        if mcp is None and record.result is not None:
            for provenance in record.result.provenance:
                mcp = McpToolProvenance.from_tool_provenance(provenance)
                if mcp is not None:
                    break
        started = next((item for item in events if item.event_type == "execution_started"), None)
        completed = next((item for item in reversed(events) if item.event_type in terminal_events), None)
        duration = None
        result_truncated = False
        for event in reversed(events):
            if duration is None and isinstance(event.payload.get("duration_ms"), int):
                duration = event.payload["duration_ms"]
            result_truncated = result_truncated or event.payload.get("result_truncated") is True
        if record.result is not None and isinstance(record.result.output, dict):
            result_truncated = result_truncated or record.result.output.get("truncated") is True
        event_names = {event.event_type for event in events}
        approval_status = "not_required"
        if "user_rejected" in event_names:
            approval_status = "rejected"
        elif "approved" in event_names:
            approval_status = "approved"
        elif "approval_required" in event_names:
            approval_status = "required"
        remote_name = mcp.remote_tool_name if mcp else None
        output.append(PublicToolCall(
            call_id=record.call_id,
            display_name=remote_name or record.request.tool_name,
            public_tool_name=record.request.tool_name,
            provider="MCP" if mcp else "Built-in",
            mcp_server_id=mcp.server_id if mcp else None,
            remote_tool_name=remote_name,
            status=record.status.value,
            side_effect=record.side_effect,
            duration_ms=duration,
            approval_status=approval_status,
            result_truncated=result_truncated,
            idempotently_reused="idempotently_reused" in event_names,
            started_at=started.occurred_at if started else None,
            completed_at=completed.occurred_at if completed else None,
            error_code=record.error_code,
        ))
    return output


def _response(
    runtime: SessionRuntime,
    state: SessionState,
    outcome: SessionOutcome | None = None,
) -> SessionResponse:
    return SessionResponse(
        session=_public_session(runtime, state),
        outcome_status=outcome.status if outcome else None,
        response=outcome.final_text if outcome else None,
        pending_tool_approvals=_pending_approvals(runtime, state),
        tool_calls=_public_tool_calls(runtime, state),
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    request: CreateSessionRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    if request.active_run_id and runtime.run_reader.get_run(request.active_run_id) is None:
        raise RunNotFoundError(f"Run '{request.active_run_id}' was not found.")
    application = None
    if request.application_id:
        if runtime.workspace is None:
            raise ValueError("The Workspace runtime is unavailable.")
        application = runtime.workspace.get_application(request.application_id)
    state = runtime.coordinator.create_session(
        title=request.title,
        active_run_id=request.active_run_id,
        allowed_tools=runtime.capability_tools(request.capability_profile),
        allowed_skills=runtime.capability_skills(request.capability_profile),
        canary_skill_version_ids=(runtime.test_skill_versions(ActivationMode.CANARY)
                                  if request.test_canary else frozenset()),
        shadow_skill_version_ids=runtime.test_skill_versions(ActivationMode.SHADOW),
    )
    if application is not None:
        runtime.workspace.attach_session(
            application.application_id,
            state.session_id,
            role="workspace_assistant",
            expected_version=application.version,
        )
    return _response(runtime, state)


@router.get("", response_model=SessionListResponse)
def list_sessions(
    limit: int = Query(default=25, ge=1, le=100),
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionListResponse:
    return SessionListResponse(
        sessions=[
            SessionSummary(
                session_id=state.session_id,
                title=state.title,
                status=state.status,
                active_run_id=state.active_run_id,
                message_count=state.message_sequence,
                created_at=state.created_at,
                updated_at=state.updated_at,
            )
            for state in runtime.sessions.list_recent(limit=limit)
            if state.task_id is None
        ]
    )


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    return _response(runtime, _require_public_session(runtime, session_id))


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    expected_version: int = Query(ge=0),
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> None:
    session = _require_public_session(runtime, session_id)
    if session.status == SessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "session_running",
                "message": "Cancel the running conversation before removing it.",
            },
        )
    runtime.sessions.archive(session_id, expected_version=expected_version)


@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
def get_messages(
    session_id: str,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionMessagesResponse:
    _require_public_session(runtime, session_id)
    messages = []
    for stored in runtime.sessions.messages(session_id):
        if stored.visibility not in {
            SessionMessageVisibility.SESSION,
            SessionMessageVisibility.SHARED,
        }:
            continue
        if stored.message.role not in {"user", "assistant"}:
            continue
        if stored.message.role == "assistant" and (
            stored.message.tool_calls or not stored.message.content.strip()
        ):
            # Durable tool-call envelopes coordinate execution but are not
            # user-facing assistant responses.
            continue
        messages.append(
            PublicSessionMessage(
                message_id=stored.message_id,
                sequence=stored.sequence,
                role=stored.message.role,
                content=stored.message.content,
                created_at=stored.created_at,
            )
        )
    return SessionMessagesResponse(session_id=session_id, messages=messages)


@router.post("/{session_id}/messages", response_model=SessionResponse)
def submit_message(
    session_id: str,
    request: SubmitMessageRequest,
    background_tasks: BackgroundTasks,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    _require_public_session(runtime, session_id)
    if not request.content.strip():
        raise ValueError("Message content must not be blank.")
    outcome = runtime.coordinator.submit_user_message(
        session_id,
        AgentMessage(
            message_id=request.message_id,
            role="user",
            content=request.content,
        ),
        expected_version=request.expected_version,
    )
    _schedule_conversation_learning(background_tasks, runtime, outcome)
    return _response(runtime, outcome.state, outcome)


@router.post(
    "/{session_id}/tool-calls/{tool_call_id}/approve",
    response_model=SessionResponse,
)
def approve_tool_call(
    session_id: str,
    tool_call_id: str,
    request: VersionedMutationRequest,
    background_tasks: BackgroundTasks,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    _require_public_session(runtime, session_id)
    outcome = runtime.coordinator.approve_tool_call(
        session_id, tool_call_id, expected_version=request.expected_version
    )
    _schedule_conversation_learning(background_tasks, runtime, outcome)
    return _response(runtime, outcome.state, outcome)


@router.post(
    "/{session_id}/tool-calls/{tool_call_id}/reject",
    response_model=SessionResponse,
)
def reject_tool_call(
    session_id: str,
    tool_call_id: str,
    request: VersionedMutationRequest,
    background_tasks: BackgroundTasks,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    _require_public_session(runtime, session_id)
    outcome = runtime.coordinator.reject_tool_call(
        session_id, tool_call_id, expected_version=request.expected_version
    )
    _schedule_conversation_learning(background_tasks, runtime, outcome)
    return _response(runtime, outcome.state, outcome)


@router.post("/{session_id}/cancel", response_model=SessionResponse)
def cancel_session(
    session_id: str,
    request: CancelSessionRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    _require_public_session(runtime, session_id)
    state = runtime.coordinator.request_cancel(
        session_id,
        request.reason,
        expected_version=request.expected_version,
    )
    return _response(runtime, state)


@router.post("/{session_id}/recover", response_model=SessionResponse)
def recover_session(
    session_id: str,
    request: VersionedMutationRequest,
    background_tasks: BackgroundTasks,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    _require_public_session(runtime, session_id)
    outcome = runtime.coordinator.recover_session(
        session_id,
        worker_id=f"api-recovery-{uuid4()}",
        expected_version=request.expected_version,
    )
    _schedule_conversation_learning(background_tasks, runtime, outcome)
    return _response(runtime, outcome.state, outcome)

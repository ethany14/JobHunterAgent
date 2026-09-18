"""HTTP routes for durable custom-agent sessions."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status

from agent_runtime.sessions.outcome import SessionOutcome
from agent_runtime.sessions.state import SessionMessageVisibility, SessionState, SessionStatus
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.types import ToolExecutionStatus, ToolSideEffect
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
    SessionMessagesResponse,
    SessionListResponse,
    SessionResponse,
    SessionSummary,
    SubmitMessageRequest,
    VersionedMutationRequest,
)
from api.services.run_service import RunNotFoundError

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _profile_for(state: SessionState) -> str:
    for name, tools in CAPABILITY_PROFILES.items():
        if state.allowed_tools == tools:
            return name
    # Sessions created by this API always have a known server profile. Refuse to
    # expose a guessed profile for legacy/manual state.
    raise ValueError("The session capability profile is not supported by this API.")


def _public_session(runtime: SessionRuntime, state: SessionState) -> PublicSession:
    return PublicSession(
        session_id=state.session_id,
        title=state.title,
        capability_profile=_profile_for(state),
        status=state.status,
        version=state.version,
        active_run_id=state.active_run_id,
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
        tool = runtime.registry.get(record.request.tool_name)
        approvals.append(
            PendingToolApproval(
                call_id=record.call_id,
                tool_name=record.request.tool_name,
                tool_version=record.tool_version,
                description=tool.description,
                side_effect=record.side_effect or getattr(tool, "side_effect", ToolSideEffect.NONE),
                data_classification=tool.data_classification,
                # Persisted ToolCallRecord arguments are the repository's redacted copy.
                arguments=record.request.arguments,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
    approvals.sort(key=lambda item: item.created_at)
    return approvals


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
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    request: CreateSessionRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    if request.active_run_id and runtime.run_reader.get_run(request.active_run_id) is None:
        raise RunNotFoundError(f"Run '{request.active_run_id}' was not found.")
    state = runtime.coordinator.create_session(
        title=request.title,
        active_run_id=request.active_run_id,
        allowed_tools=CAPABILITY_PROFILES[request.capability_profile],
        allowed_skills=CAPABILITY_SKILL_PROFILES[request.capability_profile],
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
        ]
    )


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    return _response(runtime, runtime.sessions.require(session_id))


@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
def get_messages(
    session_id: str,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionMessagesResponse:
    runtime.sessions.require(session_id)
    messages = []
    for stored in runtime.sessions.messages(session_id):
        if stored.visibility not in {
            SessionMessageVisibility.SESSION,
            SessionMessageVisibility.SHARED,
        }:
            continue
        if stored.message.role not in {"user", "assistant"}:
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
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
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
    return _response(runtime, outcome.state, outcome)


@router.post(
    "/{session_id}/tool-calls/{tool_call_id}/approve",
    response_model=SessionResponse,
)
def approve_tool_call(
    session_id: str,
    tool_call_id: str,
    request: VersionedMutationRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    outcome = runtime.coordinator.approve_tool_call(
        session_id, tool_call_id, expected_version=request.expected_version
    )
    return _response(runtime, outcome.state, outcome)


@router.post(
    "/{session_id}/tool-calls/{tool_call_id}/reject",
    response_model=SessionResponse,
)
def reject_tool_call(
    session_id: str,
    tool_call_id: str,
    request: VersionedMutationRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    outcome = runtime.coordinator.reject_tool_call(
        session_id, tool_call_id, expected_version=request.expected_version
    )
    return _response(runtime, outcome.state, outcome)


@router.post("/{session_id}/cancel", response_model=SessionResponse)
def cancel_session(
    session_id: str,
    request: CancelSessionRequest,
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
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
    runtime: SessionRuntime = Depends(get_session_runtime),
) -> SessionResponse:
    outcome = runtime.coordinator.recover_session(
        session_id,
        worker_id=f"api-recovery-{uuid4()}",
        expected_version=request.expected_version,
    )
    return _response(runtime, outcome.state, outcome)

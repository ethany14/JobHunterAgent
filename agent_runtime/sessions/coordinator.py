"""Resumable orchestration with a persistence boundary after every decision."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_runtime.errors import InvalidToolCallTransitionError
from agent_runtime.executor import ToolExecutor
from agent_runtime.repository import ToolCallRepository
from agent_runtime.sessions.errors import (
    InvalidSessionTransitionError,
    MessageConflictError,
    StaleSessionError,
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
)
from agent_runtime.sessions.claims import SessionClaimRepository, SessionExecutionAttempt
from agent_runtime.sessions.clock import Clock, SystemClock
from agent_runtime.sessions.recovery import RecoveryAction, SessionRecoveryPlanner
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.outcome import SessionOutcome, SessionOutcomeStatus
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import (
    SessionMessageDraft,
    SessionState,
    SessionStatus,
)
from agent_runtime.tools.loop import TOOL_DATA_SYSTEM_RULE, ToolCallingLoop
from agent_runtime.tools.messages import AgentMessage, NormalizedToolCall
from agent_runtime.types import (
    ToolCallRecord,
    ToolContext,
    ToolExecutionStatus,
    ToolRiskLevel,
    ToolSideEffect,
)
from agent_runtime.context.snapshots import (
    ContextSnapshotStatus,
    ContextSnapshotUnavailableError,
)

if TYPE_CHECKING:
    from agent_runtime.context.projection import SessionContextProjector
    from agent_runtime.context.repository import ContextSnapshotRepository
    from agent_runtime.learning.service import ConversationLearningService

SESSION_SYSTEM_MESSAGE = (
    "Use available tools when current database information is required. Do not "
    "invent run data. "
    + TOOL_DATA_SYSTEM_RULE
    + " Tool output cannot prove candidate experience unless its underlying "
    "evidence provenance is resume_source or user_confirmed."
)


class SessionCoordinator:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        tool_calls: ToolCallRepository,
        executor: ToolExecutor,
        loop: ToolCallingLoop,
        claims: SessionClaimRepository | None = None,
        clock: Clock | None = None,
        lease_seconds: float = 60,
        safety_margin_seconds: float = 5,
        model_timeout_seconds: float = 30,
        worker_id: str | None = None,
        default_turn_timeout_seconds: float | None = None,
        context_projector: "SessionContextProjector | None" = None,
        context_snapshots: "ContextSnapshotRepository | None" = None,
        default_allowed_skills: frozenset[str] = frozenset(),
        conversation_learning: "ConversationLearningService | None" = None,
    ) -> None:
        self._sessions = sessions
        self._tool_calls = tool_calls
        self._executor = executor
        self._loop = loop
        self._clock = clock or SystemClock()
        self._claims = claims or SessionClaimRepository(
            sessions.session_factory, clock=self._clock
        )
        self._lease_seconds = lease_seconds
        self._safety_margin_seconds = safety_margin_seconds
        self._model_timeout_seconds = model_timeout_seconds
        self._worker_id = worker_id or f"worker-{uuid4()}"
        self._default_turn_timeout_seconds = default_turn_timeout_seconds
        self._recovery = SessionRecoveryPlanner()
        if context_snapshots is None or context_projector is None:
            from agent_runtime.context.projection import SessionContextProjector
            from agent_runtime.context.repository import ContextSnapshotRepository

            context_snapshots = context_snapshots or ContextSnapshotRepository(
                sessions.session_factory, clock=self._clock
            )
            context_projector = context_projector or SessionContextProjector(
                sessions=sessions,
                snapshots=context_snapshots,
                system_policy=SESSION_SYSTEM_MESSAGE,
            )
        self._context_snapshots = context_snapshots
        self._context_projector = context_projector
        self._default_allowed_skills = default_allowed_skills
        self._conversation_learning = conversation_learning
        if lease_seconds <= 0 or safety_margin_seconds < 0:
            raise ValueError("Lease settings are invalid.")
        if model_timeout_seconds + safety_margin_seconds >= lease_seconds:
            raise ValueError("The model timeout plus safety margin must be shorter than the lease.")
        if default_turn_timeout_seconds is not None and default_turn_timeout_seconds <= 0:
            raise ValueError("The default turn timeout must be positive.")

    def create_session(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        title: str | None = None,
        active_run_id: str | None = None,
        allowed_tools: frozenset[str] = frozenset(),
        max_loop_iterations: int = 6,
        max_tool_calls: int = 10,
        session_expires_at: datetime | None = None,
        allowed_skills: frozenset[str] | None = None,
        canary_skill_version_ids: frozenset[str] = frozenset(),
        shadow_skill_version_ids: frozenset[str] = frozenset(),
    ) -> SessionState:
        identifier = session_id or str(uuid4())
        system = AgentMessage(
            message_id=self._stable_id("system", identifier),
            role="system",
            content=SESSION_SYSTEM_MESSAGE,
        )
        state = SessionState(
            session_id=identifier,
            user_id=user_id,
            title=title,
            active_run_id=active_run_id,
            allowed_tools=allowed_tools,
            max_loop_iterations=max_loop_iterations,
            max_tool_calls=max_tool_calls,
            session_expires_at=session_expires_at,
            allowed_skills=(
                self._default_allowed_skills if allowed_skills is None else allowed_skills
            ),
            canary_skill_version_ids=canary_skill_version_ids,
            shadow_skill_version_ids=shadow_skill_version_ids,
        )
        return self._sessions.create(
            state,
            SessionEvent(
                session_id=identifier,
                event_type=SessionEventType.SESSION_CREATED,
            ),
            messages=[SessionMessageDraft(message=system)],
        )

    def submit_user_message(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        expected_version: int,
        turn_timeout_seconds: float | None = None,
    ) -> SessionOutcome:
        if message.role != "user":
            raise ValueError("submit_user_message requires a user message.")
        state = self._sessions.require(session_id)
        existing = self._sessions.message(message.message_id)
        if existing is not None:
            if existing.session_id != session_id or existing.message != message:
                raise MessageConflictError(
                    "The message ID was reused with different content."
                )
            return self._outcome_for_stable_state(state)
        self._check_version(state, expected_version)
        if state.status not in {SessionStatus.ACTIVE, SessionStatus.AWAITING_USER}:
            raise InvalidSessionTransitionError(
                "User messages are accepted only by active or awaiting-user sessions."
            )
        running = self._updated(
            state,
            {
                "status": SessionStatus.RUNNING,
                "error_code": None,
                "error_message": None,
                "turn_deadline_at": self._new_turn_deadline(turn_timeout_seconds),
                "terminal_reason": None,
                # Loop limits apply to one user turn; token totals remain
                # cumulative for the persisted session.
                "loop_iteration": 0,
                "executed_tool_calls": 0,
            },
        )
        persisted = self._sessions.save_transition(
            running,
            SessionEvent(
                session_id=session_id,
                event_type=SessionEventType.SESSION_RESUMED,
                payload={"source": "user_message"},
            ),
            expected_version=state.version,
            messages=[SessionMessageDraft(message=message)],
        )
        return self.continue_session(session_id, expected_version=persisted.version)

    def continue_session(
        self, session_id: str, *, expected_version: int | None = None,
        worker_id: str | None = None,
    ) -> SessionOutcome:
        state = self._sessions.require(session_id)
        if expected_version is not None:
            self._check_version(state, expected_version)
        if state.status != SessionStatus.RUNNING:
            return self._outcome_for_stable_state(state)
        owner = worker_id or self._worker_id
        attempt = self._claims.claim(
            session_id, owner, lease_seconds=self._lease_seconds
        )
        try:
            self._validate_timeouts(state, attempt)
            outcome = self._continue_claimed(state, attempt)
        except Exception:
            self._release_if_owned(attempt)
            raise
        self._release_if_owned(attempt)
        return outcome

    def _continue_claimed(
        self, state: SessionState, attempt: SessionExecutionAttempt
    ) -> SessionOutcome:
        while True:
            state = self._sessions.require(state.session_id)
            control = self._control_outcome(state)
            if control is not None:
                return control
            if state.status == SessionStatus.AWAITING_TOOL_APPROVAL:
                return self._approval_outcome(state)
            if state.status == SessionStatus.AWAITING_USER:
                return self._outcome(SessionOutcomeStatus.AWAITING_USER, state)
            if state.status == SessionStatus.ACTIVE:
                return self._response_outcome(state)
            if state.status in {
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
                SessionStatus.COMPLETED,
            }:
                return self._outcome_for_stable_state(state)
            if state.status != SessionStatus.RUNNING:
                raise InvalidSessionTransitionError("The session cannot continue.")

            if state.pending_tool_call_ids:
                state, outcome = self._process_first_pending(state, attempt)
                if outcome is not None:
                    return outcome
                continue

            if state.loop_iteration >= state.max_loop_iterations:
                return self._fail(
                    state,
                    code="max_loop_iterations_exceeded",
                    message="The persisted model-iteration limit was reached.",
                    limit=True,
                )
            attempt = self._heartbeat(attempt)
            timeout = self._effective_timeout(
                state, attempt, self._model_timeout_seconds
            )
            context = self._context(state, attempt, timeout_seconds=timeout)
            prepared = None
            created_snapshot_id = None
            try:
                prepared = (
                    self._context_projector.rebuild(
                        state, state.current_context_snapshot_id
                    )
                    if state.current_context_snapshot_id
                    else self._context_projector.prepare(state)
                )
                if not state.current_context_snapshot_id:
                    created_snapshot_id = prepared.snapshot.snapshot_id
                    state = self._sessions.save_transition(
                        self._updated(state, {
                            "current_context_snapshot_id": prepared.snapshot.snapshot_id,
                        }),
                        SessionEvent(
                            session_id=state.session_id,
                            event_type=SessionEventType.CONTEXT_SNAPSHOT_PREPARED,
                            payload={"snapshot_id": prepared.snapshot.snapshot_id},
                        ),
                        expected_version=state.version,
                    )
                messages = prepared.messages
                context = self._context(
                    state,
                    attempt,
                    timeout_seconds=timeout,
                    allowed_tools=prepared.snapshot.effective_tools,
                )
            except Exception:
                if created_snapshot_id is not None:
                    try:
                        self._context_snapshots.abandon(
                            created_snapshot_id,
                            reason="context_pointer_not_persisted",
                        )
                    except ContextSnapshotUnavailableError:
                        pass
                current = self._sessions.require(state.session_id)
                control = self._control_outcome(current)
                if control is not None:
                    return control
                return self._fail(
                    current,
                    code="context_snapshot_unavailable",
                    message="The model context could not be prepared safely.",
                )
            try:
                response = self._loop.decide(
                    messages, context, timeout_seconds=timeout
                )
            except SessionClaimNotOwnedError:
                raise
            except Exception:
                self._abandon_context(state, "model_invocation_failed")
                current = self._sessions.require(state.session_id)
                control = self._control_outcome(current)
                if control is not None:
                    return control
                return self._fail(
                    current,
                    code="model_invocation_failed",
                    message="The model could not complete the session turn.",
                )
            current = self._sessions.require(state.session_id)
            control = self._control_outcome(
                current,
                discarded_event=SessionEventType.MODEL_RESULT_DISCARDED,
            )
            if control is not None:
                return control
            if current.version != state.version:
                raise StaleSessionError(
                    "The session changed while the model call was in flight."
                )
            attempt = self._heartbeat(attempt)
            state = current
            assistant = response.message
            if assistant.role != "assistant":
                return self._fail(
                    state,
                    code="invalid_model_response",
                    message="The model returned an invalid message role.",
                )
            pending = [item.tool_call_id for item in assistant.tool_calls]
            next_status = SessionStatus.RUNNING if pending else SessionStatus.ACTIVE
            decided = self._updated(
                state,
                {
                    "status": next_status,
                    "pending_assistant_message_id": assistant.message_id if pending else None,
                    "pending_tool_call_ids": pending,
                    "loop_iteration": state.loop_iteration + 1,
                    "total_input_tokens": (
                        state.total_input_tokens + response.usage.input_tokens
                    ),
                    "total_output_tokens": (
                        state.total_output_tokens + response.usage.output_tokens
                    ),
                    "turn_deadline_at": state.turn_deadline_at if pending else None,
                    "current_context_snapshot_id": None,
                    "last_context_snapshot_id": prepared.snapshot.snapshot_id,
                },
            )
            try:
                state = self._sessions.save_transition(
                    decided,
                    SessionEvent(
                        session_id=state.session_id,
                        event_type=SessionEventType.MODEL_DECISION_PERSISTED,
                        payload={"tool_call_count": len(pending)},
                    ),
                    expected_version=state.version,
                    messages=[SessionMessageDraft(message=assistant)],
                )
            except StaleSessionError:
                current = self._sessions.require(state.session_id)
                control = self._control_outcome(
                    current,
                    discarded_event=SessionEventType.MODEL_RESULT_DISCARDED,
                )
                if control is not None:
                    return control
                raise
            self._context_snapshots.mark_used(prepared.snapshot.snapshot_id)
            if not pending:
                return self._response_outcome(state)

    def approve_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        expected_version: int,
    ) -> SessionOutcome:
        state = self._sessions.require(session_id)
        self._check_version(state, expected_version)
        if state.status != SessionStatus.AWAITING_TOOL_APPROVAL:
            raise InvalidSessionTransitionError(
                "The session is not awaiting tool approval."
            )
        _, _, record = self._pending_details(state, require_record=True)
        if record.call_id != tool_call_id:
            raise InvalidSessionTransitionError(
                "Only the first pending tool call can be approved."
            )
        if record.status == ToolExecutionStatus.APPROVAL_REQUIRED:
            self._executor.approve(record.call_id, expected_version=record.version)
        elif record.status != ToolExecutionStatus.APPROVED:
            raise InvalidToolCallTransitionError(
                "The pending tool call cannot be approved in its current state."
            )
        running = self._updated(
            state,
            {
                "status": SessionStatus.RUNNING,
                "turn_deadline_at": self._new_turn_deadline(None),
            },
        )
        persisted = self._sessions.save_transition(
            running,
            SessionEvent(
                session_id=session_id,
                event_type=SessionEventType.TOOL_APPROVED,
                payload={"tool_call_id": record.call_id},
            ),
            expected_version=state.version,
        )
        return self.continue_session(session_id, expected_version=persisted.version)

    def reject_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        expected_version: int,
    ) -> SessionOutcome:
        state = self._sessions.require(session_id)
        self._check_version(state, expected_version)
        if state.status != SessionStatus.AWAITING_TOOL_APPROVAL:
            raise InvalidSessionTransitionError(
                "The session is not awaiting tool approval."
            )
        assistant, call, record = self._pending_details(state, require_record=True)
        if record.call_id != tool_call_id:
            raise InvalidSessionTransitionError(
                "Only the first pending tool call can be rejected."
            )
        if record.status == ToolExecutionStatus.APPROVAL_REQUIRED:
            record = self._executor.reject(record.call_id, expected_version=record.version)
        elif not (
            record.status == ToolExecutionStatus.DENIED
            and record.error_code == "user_rejected"
        ):
            raise InvalidToolCallTransitionError(
                "The pending tool call cannot be rejected in its current state."
            )
        tool_message = self._loop.tool_message_for_record(assistant, call, record)
        remaining = state.pending_tool_call_ids[1:]
        resumed = self._updated(
            state,
            {
                "status": SessionStatus.RUNNING,
                "pending_tool_call_ids": remaining,
                "pending_assistant_message_id": (
                    state.pending_assistant_message_id if remaining else None
                ),
                "turn_deadline_at": self._new_turn_deadline(None),
            },
        )
        persisted = self._sessions.save_transition(
            resumed,
            SessionEvent(
                session_id=session_id,
                event_type=SessionEventType.TOOL_REJECTED,
                payload={"tool_call_id": record.call_id, "source": "user"},
            ),
            expected_version=state.version,
            messages=[SessionMessageDraft(message=tool_message)],
        )
        return self.continue_session(session_id, expected_version=persisted.version)

    def _process_first_pending(
        self, state: SessionState, attempt: SessionExecutionAttempt
    ) -> tuple[SessionState, SessionOutcome | None]:
        state = self._sessions.require(state.session_id)
        control = self._control_outcome(state)
        if control is not None:
            return state, control
        assistant, call, existing = self._pending_details(state, require_record=False)
        if existing is None and state.executed_tool_calls >= state.max_tool_calls:
            return state, self._fail(
                state,
                code="max_tool_calls_exceeded",
                message="The persisted tool-call limit was reached.",
                limit=True,
            )
        attempt = self._heartbeat(attempt)
        configured_timeout = self._loop.tool_timeouts(
            self._context(state, attempt)
        ).get(call.tool_name)
        timeout = self._effective_timeout(state, attempt, configured_timeout)
        context = self._context(state, attempt, timeout_seconds=timeout)
        try:
            step = self._loop.process_tool_call(
                assistant, call, context, timeout_seconds=timeout
            )
        except SessionClaimNotOwnedError:
            raise
        except Exception:
            current = self._sessions.require(state.session_id)
            control = self._control_outcome(current)
            if control is not None:
                return current, control
            return state, self._fail(
                state,
                code="tool_runtime_failed",
                message="The tool runtime could not process the pending call.",
            )
        record = step.record
        current = self._sessions.require(state.session_id)
        control = self._control_outcome(
            current,
            discarded_event=SessionEventType.TOOL_RESULT_DISCARDED,
            tool_record=record,
        )
        if control is not None:
            return current, control
        if current.version != state.version:
            raise StaleSessionError(
                "The session changed while the tool call was in flight."
            )
        self._heartbeat(attempt)
        state = current
        # Approval-required calls are counted when the pause is persisted. A
        # completed read-only record with no session tool message is instead a
        # recoverable crash window and still needs one session-level count.
        already_counted = bool(existing and existing.approval_tool_name)
        increment = 0 if already_counted else 1
        if record.status == ToolExecutionStatus.APPROVAL_REQUIRED:
            waiting = self._updated(
                state,
                {
                    "status": SessionStatus.AWAITING_TOOL_APPROVAL,
                    "executed_tool_calls": state.executed_tool_calls + increment,
                    "turn_deadline_at": None,
                },
            )
            try:
                persisted = self._sessions.save_transition(
                    waiting,
                    SessionEvent(
                        session_id=state.session_id,
                        event_type=SessionEventType.WAITING_FOR_TOOL_APPROVAL,
                        payload={"tool_call_id": record.call_id},
                    ),
                    expected_version=state.version,
                )
            except StaleSessionError:
                current = self._sessions.require(state.session_id)
                control = self._control_outcome(
                    current,
                    discarded_event=SessionEventType.TOOL_RESULT_DISCARDED,
                    tool_record=record,
                )
                if control is not None:
                    return current, control
                raise
            return persisted, self._approval_outcome(persisted, record=record)
        remaining = state.pending_tool_call_ids[1:]
        resolved = self._updated(
            state,
            {
                "status": SessionStatus.RUNNING,
                "pending_tool_call_ids": remaining,
                "pending_assistant_message_id": (
                    state.pending_assistant_message_id if remaining else None
                ),
                "executed_tool_calls": state.executed_tool_calls + increment,
            },
        )
        try:
            persisted = self._sessions.save_transition(
                resolved,
                SessionEvent(
                    session_id=state.session_id,
                    event_type=SessionEventType.TOOL_RESULT_PERSISTED,
                    payload={
                        "tool_call_id": record.call_id,
                        "tool_status": record.status.value,
                    },
                ),
                expected_version=state.version,
                messages=[SessionMessageDraft(message=step.tool_message)],
            )
        except StaleSessionError:
            current = self._sessions.require(state.session_id)
            control = self._control_outcome(
                current,
                discarded_event=SessionEventType.TOOL_RESULT_DISCARDED,
                tool_record=record,
            )
            if control is not None:
                return current, control
            raise
        return persisted, None

    def _pending_details(self, state, *, require_record):
        if not state.pending_assistant_message_id or not state.pending_tool_call_ids:
            raise InvalidSessionTransitionError("The session has no pending tool call.")
        stored = self._sessions.message(state.pending_assistant_message_id)
        if stored is None or stored.session_id != state.session_id:
            raise InvalidSessionTransitionError(
                "The pending assistant message is unavailable."
            )
        assistant = stored.message
        normalized_id = state.pending_tool_call_ids[0]
        call = next(
            (item for item in assistant.tool_calls if item.tool_call_id == normalized_id),
            None,
        )
        if call is None:
            raise InvalidSessionTransitionError(
                "The first pending tool call is not in the assistant message."
            )
        context = self._context(state)
        request = self._loop.request_for_call(assistant, call, context)
        record = self._tool_calls.find_by_idempotency(
            scope_type="session",
            scope_id=state.session_id,
            tool_name=request.tool_name,
            idempotency_key=request.idempotency_key,
        )
        if require_record and record is None:
            raise InvalidSessionTransitionError(
                "The persisted pending tool call is unavailable."
            )
        return assistant, call, record

    def _approval_outcome(
        self, state: SessionState, *, record: ToolCallRecord | None = None
    ) -> SessionOutcome:
        if record is None:
            _, _, record = self._pending_details(state, require_record=True)
        return self._outcome(
            SessionOutcomeStatus.AWAITING_TOOL_APPROVAL,
            state,
            pending=[record.call_id],
        )

    def _response_outcome(self, state: SessionState) -> SessionOutcome:
        assistant = next(
            (
                item.message
                for item in reversed(self._sessions.messages(state.session_id))
                if item.message.role == "assistant" and not item.message.tool_calls
            ),
            None,
        )
        # A prepared snapshot becomes used only when its projected messages were
        # sent and the resulting final assistant message is durably available.
        if assistant is not None and state.last_context_snapshot_id:
            snapshot = self._context_snapshots.get(state.last_context_snapshot_id)
            if snapshot is not None and snapshot.status == ContextSnapshotStatus.PREPARED:
                self._context_snapshots.mark_used(snapshot.snapshot_id)
        if assistant is not None and self._conversation_learning is not None:
            # The user-visible response is already durable. Learning is best
            # effort and cannot turn a successful session into a failed one.
            try:
                self._conversation_learning.observe_completed_turn(
                    session_id=state.session_id,
                    assistant_message_id=assistant.message_id,
                )
            except Exception:
                pass
        return self._outcome(
            SessionOutcomeStatus.RESPONSE_READY,
            state,
            final_text=assistant.content if assistant else None,
        )

    def _outcome_for_stable_state(self, state: SessionState) -> SessionOutcome:
        if state.status == SessionStatus.ACTIVE:
            return self._response_outcome(state)
        if state.status == SessionStatus.AWAITING_USER:
            return self._outcome(SessionOutcomeStatus.AWAITING_USER, state)
        if state.status == SessionStatus.AWAITING_TOOL_APPROVAL:
            return self._approval_outcome(state)
        return self._outcome(
            SessionOutcomeStatus.FAILED,
            state,
            error_code=state.error_code or "session_terminal",
            error_message=state.error_message,
        )

    def _fail(self, state, *, code, message, limit=False):
        self._abandon_context(state, code)
        failed = self._updated(
            state,
            {
                "status": SessionStatus.FAILED,
                "pending_assistant_message_id": None,
                "pending_tool_call_ids": [],
                "error_code": code,
                "error_message": message,
                "turn_deadline_at": None,
                "terminal_reason": code,
                "current_context_snapshot_id": None,
            },
        )
        persisted = self._sessions.save_transition(
            failed,
            SessionEvent(
                session_id=state.session_id,
                event_type=SessionEventType.SESSION_FAILED,
                payload={"error_code": code},
            ),
            expected_version=state.version,
        )
        return self._outcome(
            SessionOutcomeStatus.LIMIT_EXCEEDED if limit else SessionOutcomeStatus.FAILED,
            persisted,
            error_code=code,
            error_message=message,
        )

    def _context(
        self,
        state: SessionState,
        attempt: SessionExecutionAttempt | None = None,
        *,
        timeout_seconds: float | None = None,
        allowed_tools: frozenset[str] | None = None,
    ) -> ToolContext:
        effective_tools = state.allowed_tools if allowed_tools is None else allowed_tools
        if allowed_tools is None:
            snapshot_id = state.current_context_snapshot_id or state.last_context_snapshot_id
            if snapshot_id:
                snapshot = self._context_snapshots.get(snapshot_id)
                if snapshot is not None:
                    effective_tools &= snapshot.effective_tools
        return ToolContext(
            session_id=state.session_id,
            user_id=state.user_id,
            agent_name="session_coordinator",
            attempt_id=attempt.attempt_id if attempt else None,
            lease_until=attempt.lease_until if attempt else None,
            turn_deadline_at=state.turn_deadline_at,
            remaining_timeout_seconds=timeout_seconds,
            allowed_tools=effective_tools,
        )

    def _abandon_context(self, state: SessionState, reason: str) -> None:
        if state.current_context_snapshot_id:
            try:
                self._context_snapshots.abandon(
                    state.current_context_snapshot_id, reason=reason
                )
            except ContextSnapshotUnavailableError:
                pass

    def request_cancel(
        self,
        session_id: str,
        reason: str,
        *,
        expected_version: int | None = None,
    ) -> SessionState:
        cleaned = reason.strip()
        if not cleaned:
            raise ValueError("A cancellation reason is required.")
        for _ in range(3):
            state = self._sessions.require(session_id)
            if state.status == SessionStatus.CANCELLED:
                return state
            if state.status == SessionStatus.RUNNING and state.cancel_requested:
                return state
            if expected_version is not None:
                self._check_version(state, expected_version)
            if state.terminal:
                raise InvalidSessionTransitionError(
                    "A completed terminal session cannot be cancelled."
                )
            if state.status == SessionStatus.AWAITING_TOOL_APPROVAL:
                self._reject_pending_approval_calls(state)
            now = self._clock.now()
            immediate = state.status in {
                SessionStatus.ACTIVE,
                SessionStatus.AWAITING_USER,
                SessionStatus.AWAITING_TOOL_APPROVAL,
            }
            updates = {
                "cancel_requested": True,
                "cancel_requested_at": now,
                "cancel_reason": cleaned,
            }
            event_type = SessionEventType.CANCEL_REQUESTED
            if immediate:
                self._abandon_context(state, "user_cancelled")
                updates.update(
                    {
                        "status": SessionStatus.CANCELLED,
                        "pending_assistant_message_id": None,
                        "pending_tool_call_ids": [],
                        "manual_recovery_tool_call_ids": [],
                        "turn_deadline_at": None,
                        "terminal_reason": "user_cancelled",
                        "current_context_snapshot_id": None,
                    }
                )
                event_type = SessionEventType.SESSION_CANCELLED
            desired = self._updated(state, updates)
            try:
                return self._sessions.save_transition(
                    desired,
                    SessionEvent(
                        session_id=session_id,
                        event_type=event_type,
                        payload={"reason_code": "user_requested"},
                    ),
                    expected_version=state.version,
                )
            except StaleSessionError:
                if expected_version is not None:
                    raise
                continue
        raise StaleSessionError("The cancellation request conflicted repeatedly.")

    def _reject_pending_approval_calls(self, state: SessionState) -> None:
        if not state.pending_assistant_message_id:
            return
        stored = self._sessions.message(state.pending_assistant_message_id)
        if stored is None:
            return
        for call in stored.message.tool_calls:
            request = self._loop.request_for_call(
                stored.message, call, self._context(state)
            )
            record = self._tool_calls.find_by_idempotency(
                scope_type="session",
                scope_id=state.session_id,
                tool_name=request.tool_name,
                idempotency_key=request.idempotency_key,
            )
            if record and record.status == ToolExecutionStatus.APPROVAL_REQUIRED:
                self._executor.reject(record.call_id, expected_version=record.version)

    def _control_outcome(
        self,
        state: SessionState,
        *,
        discarded_event: SessionEventType | None = None,
        tool_record: ToolCallRecord | None = None,
    ) -> SessionOutcome | None:
        now = self._clock.now()
        cancellation = state.cancel_requested
        deadline_reason = None
        if state.session_expires_at is not None and now >= state.session_expires_at:
            deadline_reason = "session_expired"
        elif state.turn_deadline_at is not None and now >= state.turn_deadline_at:
            deadline_reason = "turn_deadline_exceeded"
        if not cancellation and deadline_reason is None:
            return None
        self._abandon_context(state, "user_cancelled" if cancellation else deadline_reason or "deadline")
        unsafe = bool(
            tool_record
            and tool_record.status not in {
                ToolExecutionStatus.APPROVAL_REQUIRED,
                ToolExecutionStatus.DENIED,
                ToolExecutionStatus.FAILED,
                ToolExecutionStatus.TIMED_OUT,
            }
            and (
                tool_record.risk_level != ToolRiskLevel.READ_ONLY
                or tool_record.side_effect != ToolSideEffect.NONE
            )
        )
        if unsafe and tool_record is not None:
            uncertain = self._tool_calls.mark_outcome_unknown(
                tool_record.call_id,
                expected_version=tool_record.version,
                reason_code=(
                    "cancelled_during_execution"
                    if cancellation
                    else deadline_reason or "deadline_exceeded"
                ),
            )
            awaiting = self._updated(
                state,
                {
                    "status": SessionStatus.AWAITING_USER,
                    "pending_assistant_message_id": None,
                    "pending_tool_call_ids": [],
                    "turn_deadline_at": None,
                    "manual_recovery_tool_call_ids": list(
                        dict.fromkeys(
                            [*state.manual_recovery_tool_call_ids, uncertain.call_id]
                        )
                    ),
                    "current_context_snapshot_id": None,
                },
            )
            persisted = self._save_control_transition(
                awaiting,
                SessionEventType.MANUAL_RECOVERY_REQUIRED,
                {
                    "tool_call_id": uncertain.call_id,
                    "reason_code": uncertain.error_code,
                },
            )
            return self._outcome(SessionOutcomeStatus.AWAITING_USER, persisted)
        terminal_status = (
            SessionStatus.CANCELLED if cancellation else SessionStatus.TIMED_OUT
        )
        reason = "user_cancelled" if cancellation else deadline_reason
        terminal = self._updated(
            state,
            {
                "status": terminal_status,
                "pending_assistant_message_id": None,
                "pending_tool_call_ids": [],
                "turn_deadline_at": None,
                "terminal_reason": reason,
                "error_code": None if cancellation else reason,
                "error_message": (
                    None
                    if cancellation
                    else "The session turn exceeded its allowed time."
                ),
                "current_context_snapshot_id": None,
            },
        )
        event_type = discarded_event or (
            SessionEventType.SESSION_CANCELLED
            if cancellation
            else SessionEventType.SESSION_TIMED_OUT
        )
        persisted = self._save_control_transition(
            terminal,
            event_type,
            {
                "reason_code": reason,
                **(
                    {
                        "tool_call_id": tool_record.call_id,
                        "tool_status": tool_record.status.value,
                    }
                    if tool_record
                    else {}
                ),
            },
        )
        return self._outcome(
            SessionOutcomeStatus.FAILED,
            persisted,
            error_code=reason,
            error_message=terminal.error_message,
        )

    def _save_control_transition(
        self, state: SessionState, event_type: SessionEventType, payload: dict
    ) -> SessionState:
        for _ in range(3):
            current = self._sessions.require(state.session_id)
            if current.cancel_requested and state.status == SessionStatus.TIMED_OUT:
                state = self._updated(
                    state,
                    {
                        "status": SessionStatus.CANCELLED,
                        "terminal_reason": "user_cancelled",
                        "error_code": None,
                        "error_message": None,
                    },
                )
            desired = self._updated(
                current,
                {
                    key: value
                    for key, value in state.model_dump(mode="python").items()
                    if key
                    in {
                        "status",
                        "pending_assistant_message_id",
                        "pending_tool_call_ids",
                        "turn_deadline_at",
                        "terminal_reason",
                        "error_code",
                        "error_message",
                        "manual_recovery_tool_call_ids",
                        "current_context_snapshot_id",
                    }
                },
            )
            try:
                return self._sessions.save_transition(
                    desired,
                    SessionEvent(
                        session_id=state.session_id,
                        event_type=event_type,
                        payload=payload,
                    ),
                    expected_version=current.version,
                )
            except StaleSessionError:
                continue
        raise StaleSessionError("The control transition conflicted repeatedly.")

    def _new_turn_deadline(self, override: float | None) -> datetime | None:
        seconds = (
            override
            if override is not None
            else self._default_turn_timeout_seconds
        )
        if seconds is None:
            return None
        if seconds <= 0:
            raise ValueError("The turn timeout must be positive.")
        return self._clock.now() + timedelta(seconds=seconds)

    def _effective_timeout(
        self,
        state: SessionState,
        attempt: SessionExecutionAttempt,
        configured_timeout: float | None,
    ) -> float:
        now = self._clock.now()
        limits = [
            (attempt.lease_until - now).total_seconds()
            - self._safety_margin_seconds
        ]
        if configured_timeout is not None:
            limits.append(configured_timeout)
        if state.turn_deadline_at is not None:
            limits.append((state.turn_deadline_at - now).total_seconds())
        if state.session_expires_at is not None:
            limits.append((state.session_expires_at - now).total_seconds())
        effective = min(limits)
        if effective <= 0:
            raise SessionClaimNotOwnedError(
                "No safe execution time remains before the deadline or lease expiry."
            )
        return effective

    def _heartbeat(self, attempt: SessionExecutionAttempt) -> SessionExecutionAttempt:
        return self._claims.heartbeat(
            attempt.attempt_id, attempt.worker_id, lease_seconds=self._lease_seconds
        )

    def _release_if_owned(self, attempt: SessionExecutionAttempt) -> None:
        try:
            self._claims.release(attempt.attempt_id, attempt.worker_id)
        except Exception:
            # A lost/expired lease is deliberately not overwritten.
            pass

    def _validate_timeouts(
        self, state: SessionState, attempt: SessionExecutionAttempt
    ) -> None:
        context = self._context(state, attempt)
        ceiling = self._lease_seconds - self._safety_margin_seconds
        for name, timeout in self._loop.tool_timeouts(context).items():
            if timeout is None or timeout >= ceiling:
                raise ValueError(
                    f"Tool '{name}' timeout must be configured below the execution lease."
                )

    def recover_session(
        self,
        session_id: str,
        worker_id: str,
        *,
        expected_version: int | None = None,
    ) -> SessionOutcome:
        if self._claims.active_claim(session_id) is not None:
            raise SessionClaimConflictError(
                "The session has an active execution lease and cannot be recovered."
            )
        state = self._sessions.require(session_id)
        if expected_version is not None:
            self._check_version(state, expected_version)
        if state.status != SessionStatus.RUNNING:
            return self._outcome_for_stable_state(state)
        attempt = self._claims.claim(
            session_id, worker_id, lease_seconds=self._lease_seconds
        )
        try:
            self._validate_timeouts(state, attempt)
            state = self._sessions.save_transition(
                state,
                SessionEvent(
                    session_id=session_id,
                    event_type=SessionEventType.RECOVERY_STARTED,
                    payload={
                        "attempt_id": attempt.attempt_id,
                        "recovered_from_attempt_id": attempt.recovered_from_attempt_id,
                    },
                ),
                expected_version=state.version,
            )
            stored_messages = self._sessions.messages(session_id)
            last_message = stored_messages[-1].message if stored_messages else None
            plan = self._recovery.plan(
                state, last_message=last_message, now=self._clock.now()
            )
            transition_updates: dict = {}
            if plan.action == RecoveryAction.FINALIZE_ASSISTANT:
                transition_updates = {"status": SessionStatus.ACTIVE}
            elif (
                plan.action == RecoveryAction.RESTORE_PENDING_TOOLS
                and last_message is not None
            ):
                transition_updates = {
                    "pending_assistant_message_id": last_message.message_id,
                    "pending_tool_call_ids": [
                        item.tool_call_id for item in last_message.tool_calls
                    ],
                }
            if transition_updates:
                state = self._updated(state, transition_updates)
            if state.pending_tool_call_ids:
                assistant, call, record = self._pending_details(
                    state, require_record=False
                )
                tool_message_id = self._loop._tool_message_id(
                    assistant, call.tool_call_id
                )
                plan = self._recovery.plan(
                    state,
                    tool_record=record,
                    tool_message_persisted=self._sessions.message(tool_message_id) is not None,
                    now=self._clock.now(),
                )
            state = self._sessions.save_transition(
                state,
                SessionEvent(
                    session_id=session_id,
                    event_type=SessionEventType.RECOVERY_ACTION_PERSISTED,
                    payload={
                        "action": plan.action.value,
                        "reason_code": plan.reason_code,
                        "tool_call_id": plan.tool_call_id,
                    },
                ),
                expected_version=state.version,
            )
            if plan.action == RecoveryAction.FINALIZE_ASSISTANT:
                return self._response_outcome(state)
            if state.pending_tool_call_ids:
                if plan.action == RecoveryAction.ACTIVE_TOOL_LEASE:
                    raise SessionClaimConflictError(
                        "The pending tool has an active execution lease."
                    )
                if plan.action in {
                    RecoveryAction.RETRY_EXPIRED_TOOL,
                    RecoveryAction.MARK_OUTCOME_UNKNOWN,
                } and record is not None:
                    record = self._tool_calls.recover_expired_running(
                        record.call_id,
                        expected_version=record.version,
                        now=self._clock.now(),
                        retry_safe=plan.action == RecoveryAction.RETRY_EXPIRED_TOOL,
                    )
                    if plan.action == RecoveryAction.RETRY_EXPIRED_TOOL:
                        attempt = self._heartbeat(attempt)
                        step = self._loop.process_tool_call(
                            assistant,
                            call,
                            self._context(state, attempt),
                            retry_failed=True,
                        )
                        self._heartbeat(attempt)
                        record = step.record
            return self._continue_claimed(self._sessions.require(session_id), attempt)
        finally:
            self._release_if_owned(attempt)

    @staticmethod
    def _check_version(state: SessionState, expected: int) -> None:
        if state.version != expected:
            raise StaleSessionError(
                f"Session '{state.session_id}' has a stale state version."
            )

    @staticmethod
    def _updated(state: SessionState, updates: dict) -> SessionState:
        return SessionState.model_validate(
            {**state.model_dump(mode="python"), **updates}
        )

    @staticmethod
    def _outcome(status, state, *, final_text=None, pending=None,
                 error_code=None, error_message=None):
        return SessionOutcome(
            status=status,
            session_id=state.session_id,
            state=state,
            final_text=final_text,
            pending_tool_call_ids=pending or [],
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _stable_id(prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{prefix}-{digest[:24]}"

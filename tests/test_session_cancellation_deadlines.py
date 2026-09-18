from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel
from sqlalchemy import inspect

from agent_runtime import (
    AgentMessage,
    NormalizedToolCall,
    TokenUsage,
    ToolCallRepository,
    ToolDataClassification,
    ToolExecutor,
    ToolExecutionStatus,
    ToolModelResponse,
    ToolProvenance,
    ToolRegistry,
    ToolResult,
    ToolRiskLevel,
    ToolSideEffect,
)
from agent_runtime.sessions import (
    FakeClock,
    InvalidSessionTransitionError,
    SessionClaimRepository,
    SessionCoordinator,
    SessionEvent,
    SessionEventType,
    SessionMessageDraft,
    SessionOutcomeStatus,
    SessionRepository,
    SessionState,
    SessionStatus,
)
from agent_runtime.tools.loop import ToolCallingLoop
from api.db import create_database


class TextInput(BaseModel):
    text: str


class TextOutput(BaseModel):
    text: str


class CallbackModel:
    def __init__(self, responses, callback=None):
        self.responses = list(responses)
        self.callback = callback
        self.timeouts = []

    def invoke(self, messages, tools, *, timeout_seconds=None):
        self.timeouts.append(timeout_seconds)
        if self.callback:
            self.callback()
        return self.responses.pop(0)


class CallbackTool:
    version = "1"
    description = "callback tool"
    data_classification = ToolDataClassification.INTERNAL
    input_schema = TextInput
    output_schema = TextOutput
    timeout_seconds = 20
    idempotent = True

    def __init__(self, name="read", *, write=False, callback=None):
        self.name = name
        self.risk_level = (
            ToolRiskLevel.LOCAL_WRITE if write else ToolRiskLevel.READ_ONLY
        )
        self.side_effect = (
            ToolSideEffect.LOCAL_WRITE if write else ToolSideEffect.NONE
        )
        self.callback = callback
        self.calls = 0
        self.timeouts = []
        self.contexts = []

    def execute(self, arguments, context, *, timeout_seconds=None):
        self.calls += 1
        self.timeouts.append(timeout_seconds)
        self.contexts.append(context)
        if self.callback:
            self.callback()
        return ToolResult(
            output={"text": arguments.text},
            provenance=[ToolProvenance(source_type="internal_database")],
        )


def response(message_id, content="", calls=()):
    return ToolModelResponse(
        message=AgentMessage(
            message_id=message_id,
            role="assistant",
            content=content,
            tool_calls=list(calls),
        ),
        usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5),
    )


def call(identifier="c", tool="read"):
    return NormalizedToolCall(
        tool_call_id=identifier,
        tool_name=tool,
        arguments={"text": "value"},
    )


def build_runtime(
    tmp_path,
    *,
    clock=None,
    model=None,
    tools=(),
    turn_timeout=10,
    lease=60,
):
    db = create_database(
        f"sqlite:///{(tmp_path / 'cancel.sqlite').as_posix()}",
        create_schema_for_tests=True,
    )
    sessions = SessionRepository(db.session_factory)
    tool_calls = ToolCallRepository(db.session_factory)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(registry, repository=tool_calls)
    loop = ToolCallingLoop(
        model=model or CallbackModel([]), registry=registry, executor=executor
    )
    claims = SessionClaimRepository(db.session_factory, clock=clock)
    service = SessionCoordinator(
        sessions=sessions,
        tool_calls=tool_calls,
        executor=executor,
        loop=loop,
        claims=claims,
        clock=clock,
        lease_seconds=lease,
        safety_margin_seconds=2,
        model_timeout_seconds=30,
        default_turn_timeout_seconds=turn_timeout,
        worker_id="worker",
    )
    return db, sessions, tool_calls, claims, service


def user(identifier="u"):
    return AgentMessage(message_id=identifier, role="user", content="question")


def test_immediate_cancellation_and_idempotency(tmp_path):
    db, sessions, _, _, service = build_runtime(tmp_path)
    state = service.create_session(session_id="cancel-now")
    cancelled = service.request_cancel(state.session_id, "user stopped")
    again = service.request_cancel(state.session_id, "ignored duplicate")
    assert cancelled.status == SessionStatus.CANCELLED
    assert again.version == cancelled.version
    assert cancelled.cancel_reason == "user stopped"
    assert cancelled.terminal_reason == "user_cancelled"
    events = sessions.events(state.session_id)
    assert events[-1].event_type == SessionEventType.SESSION_CANCELLED
    assert "user stopped" not in str(events[-1].payload)
    db.close()


def test_running_cancel_request_is_durable_and_idempotent(tmp_path):
    db, sessions, _, _, service = build_runtime(tmp_path)
    state = service.create_session(session_id="running-cancel")
    running = sessions.save_transition(
        SessionState.model_validate(
            {**state.model_dump(mode="python"), "status": SessionStatus.RUNNING}
        ),
        SessionEvent(
            session_id=state.session_id,
            event_type=SessionEventType.SESSION_RESUMED,
        ),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=user())],
    )
    requested = service.request_cancel(state.session_id, "stop")
    repeated = service.request_cancel(state.session_id, "stop again")
    assert requested.status == SessionStatus.RUNNING
    assert requested.cancel_requested is True
    assert repeated.version == requested.version
    assert repeated.cancel_reason == "stop"
    db.close()


def test_other_terminal_states_reject_cancellation(tmp_path):
    db, sessions, _, _, service = build_runtime(tmp_path)
    state = service.create_session(session_id="done")
    completed = SessionState.model_validate(
        {
            **state.model_dump(mode="python"),
            "status": SessionStatus.COMPLETED,
            "terminal_reason": "completed",
        }
    )
    sessions.save_transition(
        completed,
        SessionEvent(session_id="done", event_type=SessionEventType.SESSION_COMPLETED),
        expected_version=state.version,
    )
    with pytest.raises(InvalidSessionTransitionError):
        service.request_cancel("done", "too late")
    db.close()


def test_model_result_is_discarded_after_inflight_cancellation(tmp_path):
    holder = {}
    model = CallbackModel(
        [response("must-not-persist", "stale")],
        callback=lambda: holder["service"].request_cancel("model-cancel", "stop"),
    )
    db, sessions, _, claims, service = build_runtime(tmp_path, model=model)
    holder["service"] = service
    state = service.create_session(session_id="model-cancel")
    outcome = service.submit_user_message("model-cancel", user(), expected_version=state.version)
    assert outcome.state.status == SessionStatus.CANCELLED
    assert all(
        item.message.message_id != "must-not-persist"
        for item in sessions.messages("model-cancel")
    )
    assert sessions.events("model-cancel")[-1].event_type == SessionEventType.MODEL_RESULT_DISCARDED
    assert claims.active_claim("model-cancel") is None
    db.close()


def test_approval_cancellation_rejects_call_and_cancels(tmp_path):
    write = CallbackTool("write", write=True)
    model = CallbackModel([response("a", calls=[call("wc", "write")])])
    db, _, calls, _, service = build_runtime(tmp_path, model=model, tools=[write])
    state = service.create_session(
        session_id="approval-cancel", allowed_tools=frozenset({"write"})
    )
    paused = service.submit_user_message(
        state.session_id, user(), expected_version=state.version
    )
    record = calls.require(paused.pending_tool_call_ids[0])
    cancelled = service.request_cancel(state.session_id, "declined")
    assert cancelled.status == SessionStatus.CANCELLED
    assert calls.require(record.call_id).status == ToolExecutionStatus.DENIED
    assert write.calls == 0
    db.close()


def test_pre_operation_deadline_times_out_without_model_call(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    model = CallbackModel([response("unused", "unused")])
    db, sessions, _, claims, service = build_runtime(
        tmp_path, clock=clock, model=model
    )
    state = service.create_session(session_id="pre-deadline")
    running = SessionState.model_validate(
        {
            **state.model_dump(mode="python"),
            "status": SessionStatus.RUNNING,
            "turn_deadline_at": clock.now() - timedelta(seconds=1),
        }
    )
    running = sessions.save_transition(
        running,
        SessionEvent(session_id=state.session_id, event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=user())],
    )
    outcome = service.continue_session(state.session_id, expected_version=running.version)
    assert outcome.state.status == SessionStatus.TIMED_OUT
    assert model.timeouts == []
    assert claims.active_claim(state.session_id) is None
    db.close()


def test_deadline_during_model_discards_response(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    model = CallbackModel(
        [response("late", "late")], callback=lambda: clock.advance(seconds=11)
    )
    db, sessions, _, _, service = build_runtime(
        tmp_path, clock=clock, model=model, turn_timeout=10
    )
    state = service.create_session(session_id="late-model")
    outcome = service.submit_user_message(
        state.session_id, user(), expected_version=state.version
    )
    assert outcome.state.status == SessionStatus.TIMED_OUT
    assert all(m.message.message_id != "late" for m in sessions.messages(state.session_id))
    assert sessions.events(state.session_id)[-1].event_type == SessionEventType.MODEL_RESULT_DISCARDED
    db.close()


def test_deadline_during_read_tool_times_out_safely(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    read = CallbackTool(callback=lambda: clock.advance(seconds=11))
    model = CallbackModel([response("a", calls=[call()])])
    db, sessions, calls, claims, service = build_runtime(
        tmp_path, clock=clock, model=model, tools=[read], turn_timeout=10
    )
    state = service.create_session(
        session_id="late-read", allowed_tools=frozenset({"read"})
    )
    outcome = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert outcome.state.status == SessionStatus.TIMED_OUT
    assert calls.list_for_scope("session", state.session_id)[0].status == ToolExecutionStatus.COMPLETED
    assert sessions.events(state.session_id)[-1].event_type == SessionEventType.TOOL_RESULT_DISCARDED
    assert claims.active_claim(state.session_id) is None
    db.close()


def test_deadline_during_write_tool_requires_manual_recovery(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    write = CallbackTool(
        "write", write=True, callback=lambda: clock.advance(seconds=11)
    )
    model = CallbackModel([response("a", calls=[call("w", "write")])])
    db, _, calls, claims, service = build_runtime(
        tmp_path, clock=clock, model=model, tools=[write], turn_timeout=10
    )
    state = service.create_session(
        session_id="late-write", allowed_tools=frozenset({"write"})
    )
    paused = service.submit_user_message(state.session_id, user(), expected_version=0)
    outcome = service.approve_tool_call(
        state.session_id,
        paused.pending_tool_call_ids[0],
        expected_version=paused.state.version,
    )
    record = calls.list_for_scope("session", state.session_id)[0]
    assert outcome.state.status == SessionStatus.AWAITING_USER
    assert outcome.state.manual_recovery_tool_call_ids == [record.call_id]
    assert record.status == ToolExecutionStatus.OUTCOME_UNKNOWN
    assert claims.active_claim(state.session_id) is None
    db.close()


def test_read_tool_after_cancellation_stops_safely(tmp_path):
    holder = {}
    read = CallbackTool(callback=lambda: holder["service"].request_cancel("read-cancel", "stop"))
    model = CallbackModel([response("a", calls=[call()])])
    db, sessions, calls, _, service = build_runtime(tmp_path, model=model, tools=[read])
    holder["service"] = service
    state = service.create_session(
        session_id="read-cancel", allowed_tools=frozenset({"read"})
    )
    outcome = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert outcome.state.status == SessionStatus.CANCELLED
    assert read.calls == 1
    assert all(m.message.role != "tool" for m in sessions.messages(state.session_id))
    assert calls.list_for_scope("session", state.session_id)[0].status == ToolExecutionStatus.COMPLETED
    db.close()


def test_write_tool_cancellation_requires_manual_recovery(tmp_path):
    holder = {}
    write = CallbackTool(
        "write",
        write=True,
        callback=lambda: holder["service"].request_cancel("write-cancel", "stop"),
    )
    model = CallbackModel([response("a", calls=[call("w", "write")])])
    db, sessions, calls, claims, service = build_runtime(
        tmp_path, model=model, tools=[write]
    )
    holder["service"] = service
    state = service.create_session(
        session_id="write-cancel", allowed_tools=frozenset({"write"})
    )
    paused = service.submit_user_message(state.session_id, user(), expected_version=0)
    outcome = service.approve_tool_call(
        state.session_id,
        paused.pending_tool_call_ids[0],
        expected_version=paused.state.version,
    )
    record = calls.list_for_scope("session", state.session_id)[0]
    assert outcome.status == SessionOutcomeStatus.AWAITING_USER
    assert outcome.state.status == SessionStatus.AWAITING_USER
    assert outcome.state.manual_recovery_tool_call_ids == [record.call_id]
    assert record.status == ToolExecutionStatus.OUTCOME_UNKNOWN
    assert sessions.events(state.session_id)[-1].event_type == SessionEventType.MANUAL_RECOVERY_REQUIRED
    assert claims.active_claim(state.session_id) is None
    db.close()


def test_effective_timeout_reaches_model_tool_and_context(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    read = CallbackTool()
    model = CallbackModel(
        [response("tools", calls=[call()]), response("final", "done")]
    )
    db, _, _, _, service = build_runtime(
        tmp_path, clock=clock, model=model, tools=[read], turn_timeout=10
    )
    state = service.create_session(
        session_id="timeouts", allowed_tools=frozenset({"read"})
    )
    outcome = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert outcome.final_text == "done"
    assert model.timeouts == [10, 10]
    assert read.timeouts == [10]
    assert read.contexts[0].remaining_timeout_seconds == 10
    assert read.contexts[0].turn_deadline_at == clock.now() + timedelta(seconds=10)
    db.close()


@pytest.mark.parametrize(
    "values",
    [
        {"cancel_requested": True},
        {"cancel_requested_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {
            "status": SessionStatus.CANCELLED,
            "terminal_reason": "cancelled",
        },
        {
            "status": SessionStatus.TIMED_OUT,
            "terminal_reason": "deadline",
            "pending_assistant_message_id": "a",
            "pending_tool_call_ids": ["c"],
        },
        {"manual_recovery_tool_call_ids": ["c"]},
        {
            "status": SessionStatus.AWAITING_USER,
            "manual_recovery_tool_call_ids": ["c", "c"],
        },
    ],
)
def test_cancellation_timeout_and_manual_recovery_invariants(values):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SessionState(session_id="invalid", **values)


def test_migration_adds_cancellation_and_deadline_columns(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("JOB_AGENT_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0007_execution_claims")
    command.upgrade(config, "head")
    db = create_database(url)
    columns = {item["name"] for item in inspect(db.engine).get_columns("agent_sessions")}
    assert {
        "cancel_requested",
        "cancel_requested_at",
        "cancel_reason",
        "turn_deadline_at",
        "session_expires_at",
        "terminal_reason",
        "manual_recovery_tool_call_ids_json",
    } <= columns
    db.close()

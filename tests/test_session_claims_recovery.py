from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict
from sqlalchemy import inspect, select

from agent_runtime import (
    AgentMessage,
    NormalizedToolCall,
    TokenUsage,
    ToolCallRepository,
    ToolContext,
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
    RecoveryAction,
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
    SessionClaimRepository,
    SessionCoordinator,
    SessionEvent,
    SessionEventType,
    SessionMessageDraft,
    SessionRecoveryPlanner,
    SessionRepository,
    SessionState,
    SessionStatus,
)
from agent_runtime.sessions.models import AgentSessionRow
from agent_runtime.tools.loop import ToolCallingLoop
from api.db import create_database


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class TextOutput(BaseModel):
    text: str


class ReadTool:
    name = "read"
    version = "1"
    description = "read"
    risk_level = ToolRiskLevel.READ_ONLY
    side_effect = ToolSideEffect.NONE
    data_classification = ToolDataClassification.INTERNAL
    input_schema = TextInput
    output_schema = TextOutput
    timeout_seconds = 5
    idempotent = True

    def __init__(self):
        self.calls = 0

    def execute(self, arguments, context, *, timeout_seconds=None):
        self.calls += 1
        return ToolResult(
            output={"text": arguments.text},
            provenance=[ToolProvenance(source_type="internal_database")],
        )


class ScriptedModel:
    def __init__(self, responses, clock=None, advance=0):
        self.responses = list(responses)
        self.calls = 0
        self.clock = clock
        self.advance = advance

    def invoke(self, messages, tools):
        self.calls += 1
        if self.clock and self.advance:
            self.clock.advance(seconds=self.advance)
        return self.responses.pop(0)


def response(message_id, content="", calls=()):
    return ToolModelResponse(
        message=AgentMessage(
            message_id=message_id,
            role="assistant",
            content=content,
            tool_calls=list(calls),
        ),
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def make_runtime(tmp_path, *, clock=None, model=None, tool=None, lease=60):
    db = create_database(
        f"sqlite:///{(tmp_path / 'claims.sqlite').as_posix()}",
        create_schema_for_tests=True,
    )
    sessions = SessionRepository(db.session_factory)
    calls = ToolCallRepository(db.session_factory)
    registry = ToolRegistry()
    tool = tool or ReadTool()
    registry.register(tool)
    executor = ToolExecutor(registry, repository=calls)
    loop = ToolCallingLoop(
        model=model or ScriptedModel([]), registry=registry, executor=executor
    )
    claims = SessionClaimRepository(db.session_factory, clock=clock)
    service = SessionCoordinator(
        sessions=sessions,
        tool_calls=calls,
        executor=executor,
        loop=loop,
        claims=claims,
        clock=clock,
        lease_seconds=lease,
        model_timeout_seconds=min(30, lease / 2),
        safety_margin_seconds=1,
        worker_id="worker-a",
    )
    return db, sessions, calls, claims, service, tool


def create_running(sessions, session_id="s"):
    state = sessions.create(
        SessionState(session_id=session_id, allowed_tools=frozenset({"read"})),
        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
    )
    return sessions.save_transition(
        SessionState.model_validate(
            {**state.model_dump(mode="python"), "status": SessionStatus.RUNNING}
        ),
        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=AgentMessage(message_id="u", role="user", content="q"))],
    )


def test_claim_is_guarded_and_heartbeat_does_not_change_business_version(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    db, sessions, _, claims, _, _ = make_runtime(tmp_path, clock=clock)
    state = create_running(sessions)
    first = claims.claim("s", "worker-a", lease_seconds=10)
    with pytest.raises(SessionClaimConflictError):
        claims.claim("s", "worker-b", lease_seconds=10)
    with pytest.raises(SessionClaimNotOwnedError):
        claims.heartbeat(first.attempt_id, "worker-b", lease_seconds=10)
    with pytest.raises(SessionClaimNotOwnedError):
        claims.release(first.attempt_id, "worker-b")
    claims.heartbeat(first.attempt_id, "worker-a", lease_seconds=10)
    assert sessions.require("s").version == state.version
    with db.session_factory() as session:
        row = session.get(AgentSessionRow, "s")
        assert row.active_attempt_id == first.attempt_id
        assert "active_attempt_id" not in row.state_json
    clock.advance(seconds=11)
    second = claims.claim("s", "worker-b", lease_seconds=10)
    assert second.recovered_from_attempt_id == first.attempt_id
    assert [item.status.value for item in claims.history("s")] == ["expired", "active"]
    db.close()


def test_recovery_refuses_to_steal_active_session_claim(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    db, sessions, _, claims, service, _ = make_runtime(tmp_path, clock=clock)
    create_running(sessions)
    claims.claim("s", "worker-a", lease_seconds=10)
    with pytest.raises(SessionClaimConflictError):
        service.recover_session("s", "worker-b")
    db.close()


@pytest.mark.parametrize(
    ("status", "lease_offset", "risk", "effect", "idempotent", "expected"),
    [
        (ToolExecutionStatus.APPROVAL_REQUIRED, None, ToolRiskLevel.LOCAL_WRITE, ToolSideEffect.LOCAL_WRITE, False, RecoveryAction.AWAIT_TOOL_APPROVAL),
        (ToolExecutionStatus.COMPLETED, None, ToolRiskLevel.READ_ONLY, ToolSideEffect.NONE, True, RecoveryAction.REPLAY_TOOL_RESULT),
        (ToolExecutionStatus.FAILED, None, ToolRiskLevel.READ_ONLY, ToolSideEffect.NONE, True, RecoveryAction.APPEND_TOOL_FAILURE),
        (ToolExecutionStatus.OUTCOME_UNKNOWN, None, ToolRiskLevel.EXTERNAL_WRITE, ToolSideEffect.EXTERNAL_WRITE, False, RecoveryAction.APPEND_TOOL_FAILURE),
        (ToolExecutionStatus.RUNNING, 10, ToolRiskLevel.READ_ONLY, ToolSideEffect.NONE, True, RecoveryAction.ACTIVE_TOOL_LEASE),
        (ToolExecutionStatus.RUNNING, -1, ToolRiskLevel.READ_ONLY, ToolSideEffect.NONE, True, RecoveryAction.RETRY_EXPIRED_TOOL),
        (ToolExecutionStatus.RUNNING, -1, ToolRiskLevel.LOCAL_WRITE, ToolSideEffect.LOCAL_WRITE, True, RecoveryAction.MARK_OUTCOME_UNKNOWN),
    ],
)
def test_recovery_planner_decision_table(status, lease_offset, risk, effect, idempotent, expected):
    from datetime import timedelta
    from agent_runtime import ToolCallRequest, ToolCallRecord

    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = SessionState(
        session_id="s",
        status=SessionStatus.RUNNING,
        pending_assistant_message_id="a",
        pending_tool_call_ids=["normalized"],
    )
    record = ToolCallRecord(
        request=ToolCallRequest(tool_name="read"),
        status=status,
        risk_level=risk,
        side_effect=effect,
        idempotent=idempotent,
        execution_lease_until=(now + timedelta(seconds=lease_offset) if lease_offset is not None else None),
    )
    plan = SessionRecoveryPlanner.plan(state, tool_record=record, now=now)
    assert plan.action == expected


def test_recovery_planner_handles_persisted_assistant_boundaries():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    state = SessionState(session_id="s", status=SessionStatus.RUNNING)
    final = AgentMessage(message_id="final", role="assistant", content="done")
    assert SessionRecoveryPlanner.plan(
        state, last_message=final, now=now
    ).action == RecoveryAction.FINALIZE_ASSISTANT
    with_tools = AgentMessage(
        message_id="tools",
        role="assistant",
        tool_calls=[NormalizedToolCall(tool_call_id="c", tool_name="read")],
    )
    assert SessionRecoveryPlanner.plan(
        state, last_message=with_tools, now=now
    ).action == RecoveryAction.RESTORE_PENDING_TOOLS


def test_completed_tool_is_replayed_without_execution(tmp_path):
    call = NormalizedToolCall(tool_call_id="normalized", tool_name="read", arguments={"text": "x"})
    model = ScriptedModel([
        response("assistant-tools", calls=[call]),
        response("assistant-final", "done"),
    ])
    db, sessions, _, claims, service, tool = make_runtime(tmp_path, model=model)
    state = service.create_session(session_id="replay", allowed_tools=frozenset({"read"}))
    running = sessions.save_transition(
        SessionState.model_validate({**state.model_dump(mode="python"), "status": SessionStatus.RUNNING}),
        SessionEvent(session_id="replay", event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=AgentMessage(message_id="user", role="user", content="q"))],
    )
    decision = service._loop.decide([m.message for m in sessions.messages("replay")], service._context(running))
    assistant = decision.message
    pending = sessions.save_transition(
        SessionState.model_validate({**running.model_dump(mode="python"), "pending_assistant_message_id": assistant.message_id, "pending_tool_call_ids": ["normalized"], "loop_iteration": 1}),
        SessionEvent(session_id="replay", event_type=SessionEventType.MODEL_DECISION_PERSISTED),
        expected_version=running.version,
        messages=[SessionMessageDraft(message=assistant)],
    )
    service._loop.process_tool_call(assistant, call, service._context(pending))
    assert tool.calls == 1
    outcome = service.recover_session("replay", "recovery-worker")
    assert outcome.final_text == "done" and tool.calls == 1
    assert any(e.event_type == SessionEventType.RECOVERY_ACTION_PERSISTED for e in sessions.events("replay"))
    assert claims.active_claim("replay") is None
    db.close()


def _persist_pending_tool(sessions, service, session_id, assistant, normalized_id):
    state = sessions.require(session_id)
    running = sessions.save_transition(
        SessionState.model_validate({**state.model_dump(mode="python"), "status": SessionStatus.RUNNING}),
        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=AgentMessage(message_id=f"u-{session_id}", role="user", content="q"))],
    )
    return sessions.save_transition(
        SessionState.model_validate({**running.model_dump(mode="python"), "pending_assistant_message_id": assistant.message_id, "pending_tool_call_ids": [normalized_id], "loop_iteration": 1}),
        SessionEvent(session_id=session_id, event_type=SessionEventType.MODEL_DECISION_PERSISTED),
        expected_version=running.version,
        messages=[SessionMessageDraft(message=assistant)],
    )


def test_expired_safe_read_retries_but_uncertain_call_becomes_outcome_unknown(tmp_path):
    from datetime import timedelta
    from agent_runtime.security import arguments_hash

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    call = NormalizedToolCall(tool_call_id="n", tool_name="read", arguments={"text": "x"})
    assistant = response("a", calls=[call]).message
    model = ScriptedModel([response("final", "done")])
    db, sessions, calls, _, service, tool = make_runtime(tmp_path, clock=clock, model=model, lease=10)
    service.create_session(session_id="safe", allowed_tools=frozenset({"read"}))
    pending = _persist_pending_tool(sessions, service, "safe", assistant, "n")
    request = service._loop.request_for_call(assistant, call, service._context(pending))
    record, _ = calls.get_or_create(
        request=request,
        tool_version="1",
        risk_level=ToolRiskLevel.READ_ONLY,
        scope_type="session",
        scope_id="safe",
        arguments_hash=arguments_hash(call.arguments),
        redacted_arguments=call.arguments,
        side_effect=ToolSideEffect.NONE,
        idempotent=True,
    )
    calls.transition(
        record.call_id,
        expected_version=record.version,
        status=ToolExecutionStatus.RUNNING,
        event_type="execution_started",
        increment_attempt=True,
        execution_attempt_id="dead-worker",
        execution_lease_until=clock.now() + timedelta(seconds=5),
    )
    clock.advance(seconds=6)
    outcome = service.recover_session("safe", "recovery")
    assert outcome.final_text == "done" and tool.calls == 1
    recovered_call = calls.require(record.call_id)
    assert recovered_call.status == ToolExecutionStatus.COMPLETED
    assert recovered_call.attempt_count == 2
    db.close()


def test_repository_marks_expired_uncertain_execution_outcome_unknown(tmp_path):
    from datetime import timedelta
    from agent_runtime import ToolCallRequest
    from agent_runtime.security import arguments_hash

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    db, _, calls, _, _, _ = make_runtime(tmp_path, clock=clock)
    request = ToolCallRequest(
        tool_name="write", arguments={"password": "hidden"},
        idempotency_key="k", max_attempts=2,
    )
    record, _ = calls.get_or_create(
        request=request,
        tool_version="1",
        risk_level=ToolRiskLevel.EXTERNAL_WRITE,
        scope_type="session",
        scope_id="scope",
        arguments_hash=arguments_hash(request.arguments),
        redacted_arguments={"password": "[REDACTED]"},
        side_effect=ToolSideEffect.EXTERNAL_WRITE,
        idempotent=False,
    )
    running = calls.transition(
        record.call_id,
        expected_version=record.version,
        status=ToolExecutionStatus.RUNNING,
        event_type="execution_started",
        increment_attempt=True,
        execution_attempt_id="dead",
        execution_lease_until=clock.now() + timedelta(seconds=5),
    )
    clock.advance(seconds=6)
    unknown = calls.recover_expired_running(
        running.call_id,
        expected_version=running.version,
        now=clock.now(),
        retry_safe=False,
    )
    assert unknown.status == ToolExecutionStatus.OUTCOME_UNKNOWN
    assert unknown.retryable is False
    assert "hidden" not in str(calls.list_events(record.call_id))
    db.close()


def test_model_call_is_at_least_once_when_lease_expires_before_persist(tmp_path):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    model = ScriptedModel(
        [response("lost", "first"), response("kept", "second")],
        clock=clock,
        advance=11,
    )
    db, sessions, _, _, service, _ = make_runtime(
        tmp_path, clock=clock, model=model, lease=10
    )
    state = service.create_session(session_id="model-retry", allowed_tools=frozenset({"read"}))
    with pytest.raises(Exception):
        service.submit_user_message("model-retry", AgentMessage(message_id="u", role="user", content="q"), expected_version=state.version)
    assert all(m.message.message_id != "lost" for m in sessions.messages("model-retry"))
    model.advance = 0
    recovered = service.recover_session("model-retry", "worker-b")
    assert recovered.final_text == "second" and model.calls == 2
    db.close()


def test_migration_adds_execution_claim_tables_and_columns(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("JOB_AGENT_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0006_session_resumability")
    command.upgrade(config, "head")
    db = create_database(url)
    inspector = inspect(db.engine)
    assert "agent_session_attempts" in inspector.get_table_names()
    assert {"active_attempt_id", "lease_until"} <= {c["name"] for c in inspector.get_columns("agent_sessions")}
    assert {"execution_attempt_id", "execution_lease_until", "tool_side_effect", "tool_idempotent"} <= {c["name"] for c in inspector.get_columns("tool_calls")}
    db.close()

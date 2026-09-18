from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

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
    SessionCoordinator,
    SessionEvent,
    SessionEventType,
    SessionMessageDraft,
    SessionOutcomeStatus,
    SessionRepository,
    SessionState,
    SessionStatus,
    StaleSessionError,
)
from agent_runtime.tools.loop import ToolCallingLoop
from api.db import create_database


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class TextOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class RecordingTool:
    version = "1"
    description = "Record text."
    risk_level = ToolRiskLevel.READ_ONLY
    side_effect = ToolSideEffect.NONE
    data_classification = ToolDataClassification.INTERNAL
    input_schema = TextInput
    output_schema = TextOutput
    timeout_seconds = 1
    idempotent = True

    def __init__(self, name="read"):
        self.name = name
        self.calls = []

    def execute(self, arguments, context, *, timeout_seconds=None):
        self.calls.append(arguments.text)
        return ToolResult(
            output={"text": arguments.text},
            provenance=[ToolProvenance(source_type="internal_database")],
        )


class ApprovalTool(RecordingTool):
    risk_level = ToolRiskLevel.LOCAL_WRITE
    side_effect = ToolSideEffect.LOCAL_WRITE


class ExplodingTool(RecordingTool):
    def execute(self, arguments, context, *, timeout_seconds=None):
        raise RuntimeError("secret database detail")


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages, tools):
        self.calls.append((list(messages), list(tools)))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(message_id, content="", calls=(), input_tokens=1, output_tokens=2):
    return ToolModelResponse(
        message=AgentMessage(
            message_id=message_id,
            role="assistant",
            content=content,
            tool_calls=list(calls),
        ),
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def call(identifier, tool="read", text="value"):
    return NormalizedToolCall(
        tool_call_id=identifier,
        tool_name=tool,
        arguments={"text": text},
    )


@pytest.fixture
def runtime(tmp_path):
    database = create_database(
        f"sqlite:///{(tmp_path / 'coordinator.sqlite').as_posix()}",
        create_schema_for_tests=True,
    )
    sessions = SessionRepository(database.session_factory)
    tool_calls = ToolCallRepository(database.session_factory)
    yield database, sessions, tool_calls
    database.close()


def coordinator(sessions, tool_calls, model, *tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(registry, repository=tool_calls)
    loop = ToolCallingLoop(model=model, registry=registry, executor=executor)
    return SessionCoordinator(
        sessions=sessions,
        tool_calls=tool_calls,
        executor=executor,
        loop=loop,
    )


def user(message_id="user-1", content="question"):
    return AgentMessage(message_id=message_id, role="user", content=content)


def test_create_session_atomically_persists_system_message_and_event(runtime):
    _, sessions, tool_calls = runtime
    service = coordinator(sessions, tool_calls, ScriptedModel([]), RecordingTool())
    state = service.create_session(
        session_id="created", allowed_tools=frozenset({"read"})
    )
    messages = sessions.messages(state.session_id)
    assert state.message_sequence == 1 and state.event_sequence == 1
    assert messages[0].message.role == "system"
    assert "untrusted data" in messages[0].message.content
    assert sessions.events(state.session_id)[0].event_type == SessionEventType.SESSION_CREATED


def test_direct_response_persists_user_and_model_messages_and_tokens(runtime):
    _, sessions, tool_calls = runtime
    model = ScriptedModel([response("assistant-1", "answer", input_tokens=4, output_tokens=5)])
    service = coordinator(sessions, tool_calls, model, RecordingTool())
    state = service.create_session(session_id="direct", allowed_tools=frozenset({"read"}))
    outcome = service.submit_user_message(
        state.session_id, user(), expected_version=state.version
    )
    assert outcome.status == SessionOutcomeStatus.RESPONSE_READY
    assert outcome.final_text == "answer" and outcome.state.status == SessionStatus.ACTIVE
    assert outcome.state.loop_iteration == 1
    assert (outcome.state.total_input_tokens, outcome.state.total_output_tokens) == (4, 5)
    assert [item.message.role for item in sessions.messages(state.session_id)] == [
        "system", "user", "assistant"]


def test_each_new_user_turn_gets_a_fresh_loop_budget(runtime):
    _, sessions, tool_calls = runtime
    model = ScriptedModel([
        response("assistant-1", "first", input_tokens=4, output_tokens=5),
        response("assistant-2", "second", input_tokens=6, output_tokens=7),
    ])
    service = coordinator(sessions, tool_calls, model, RecordingTool())
    state = service.create_session(
        session_id="two-turns",
        allowed_tools=frozenset({"read"}),
        max_loop_iterations=1,
    )
    first = service.submit_user_message(
        state.session_id,
        user("user-1", "first question"),
        expected_version=state.version,
    )
    assert first.status == SessionOutcomeStatus.RESPONSE_READY
    assert first.state.loop_iteration == 1

    second = service.submit_user_message(
        state.session_id,
        user("user-2", "second question"),
        expected_version=first.state.version,
    )

    assert second.status == SessionOutcomeStatus.RESPONSE_READY
    assert second.final_text == "second"
    assert second.state.loop_iteration == 1
    assert second.state.executed_tool_calls == 0
    assert (second.state.total_input_tokens, second.state.total_output_tokens) == (10, 12)


def test_single_and_multiple_tool_calls_persist_in_original_order(runtime):
    _, sessions, tool_calls = runtime
    tool = RecordingTool()
    model = ScriptedModel([
        response("assistant-tools", calls=[
            call("call-a", text="a"), call("call-b", text="b")]),
        response("assistant-final", "complete"),
    ])
    service = coordinator(sessions, tool_calls, model, tool)
    state = service.create_session(session_id="multiple", allowed_tools=frozenset({"read"}))
    outcome = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert outcome.status == SessionOutcomeStatus.RESPONSE_READY
    assert tool.calls == ["a", "b"]
    assert outcome.state.executed_tool_calls == 2
    assert [item.message.role for item in sessions.messages(state.session_id)] == [
        "system", "user", "assistant", "tool", "tool", "assistant"]
    tool_messages = [item.message for item in sessions.messages(state.session_id)
        if item.message.role == "tool"]
    assert [item.tool_call_id for item in tool_messages] == ["call-a", "call-b"]


def test_approval_pause_resume_resolves_all_pending_before_model(runtime):
    _, sessions, tool_calls = runtime
    write = ApprovalTool("write"); read = RecordingTool("read")
    model = ScriptedModel([
        response("assistant-tools", calls=[
            call("write-normalized", "write", "write"),
            call("read-normalized", "read", "read")]),
        response("assistant-final", "approved result"),
    ])
    service = coordinator(sessions, tool_calls, model, write, read)
    state = service.create_session(
        session_id="approval", allowed_tools=frozenset({"write", "read"}))
    paused = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert paused.status == SessionOutcomeStatus.AWAITING_TOOL_APPROVAL
    assert paused.state.pending_tool_call_ids == ["write-normalized", "read-normalized"]
    assert write.calls == [] and read.calls == [] and len(model.calls) == 1
    resumed = service.approve_tool_call(
        state.session_id, paused.pending_tool_call_ids[0],
        expected_version=paused.state.version)
    assert resumed.status == SessionOutcomeStatus.RESPONSE_READY
    assert write.calls == ["write"] and read.calls == ["read"]
    assert len(model.calls) == 2
    assert resumed.state.executed_tool_calls == 2


def test_rejection_is_persisted_as_safe_tool_message_and_model_continues(runtime):
    _, sessions, tool_calls = runtime
    write = ApprovalTool("write")
    model = ScriptedModel([
        response("assistant-write", calls=[call("write-normalized", "write")]),
        response("assistant-final", "I will not write."),
    ])
    service = coordinator(sessions, tool_calls, model, write)
    state = service.create_session(session_id="reject", allowed_tools=frozenset({"write"}))
    paused = service.submit_user_message(state.session_id, user(), expected_version=0)
    outcome = service.reject_tool_call(
        state.session_id, paused.pending_tool_call_ids[0],
        expected_version=paused.state.version)
    assert outcome.status == SessionOutcomeStatus.RESPONSE_READY
    assert write.calls == []
    tool_message = next(item.message for item in sessions.messages(state.session_id)
        if item.message.role == "tool")
    envelope = json.loads(tool_message.content)
    assert envelope["error_code"] == "user_rejected"
    record = tool_calls.require(paused.pending_tool_call_ids[0])
    assert record.status == ToolExecutionStatus.DENIED
    assert tool_calls.list_events(record.call_id)[-1].event_type == "user_rejected"


def test_user_message_retry_is_idempotent_and_does_not_reinvoke_model(runtime):
    _, sessions, tool_calls = runtime
    model = ScriptedModel([response("assistant", "once")])
    service = coordinator(sessions, tool_calls, model, RecordingTool())
    state = service.create_session(session_id="idempotent", allowed_tools=frozenset({"read"}))
    message = user()
    first = service.submit_user_message(state.session_id, message, expected_version=0)
    second = service.submit_user_message(
        state.session_id, message, expected_version=first.state.version)
    assert second.final_text == "once"
    assert len(model.calls) == 1
    assert second.state.version == first.state.version


def test_stale_user_and_approval_versions_are_rejected(runtime):
    _, sessions, tool_calls = runtime
    write = ApprovalTool("write")
    model = ScriptedModel([response("assistant", calls=[call("w", "write")])])
    service = coordinator(sessions, tool_calls, model, write)
    state = service.create_session(session_id="stale", allowed_tools=frozenset({"write"}))
    with pytest.raises(StaleSessionError):
        service.submit_user_message(state.session_id, user(), expected_version=99)
    paused = service.submit_user_message(state.session_id, user(), expected_version=0)
    with pytest.raises(StaleSessionError):
        service.approve_tool_call(
            state.session_id, paused.pending_tool_call_ids[0], expected_version=0)


def test_model_and_ordinary_tool_errors_are_handled_safely(runtime):
    _, sessions, tool_calls = runtime
    model_failure = coordinator(
        sessions, tool_calls, ScriptedModel([RuntimeError("api_key=secret")]),
        RecordingTool())
    state = model_failure.create_session(session_id="model-fail", allowed_tools=frozenset({"read"}))
    failed = model_failure.submit_user_message(state.session_id, user(), expected_version=0)
    assert failed.status == SessionOutcomeStatus.FAILED
    assert failed.state.error_code == "model_invocation_failed"
    assert "secret" not in failed.model_dump_json()

    exploding = ExplodingTool("explode")
    model = ScriptedModel([
        response("assistant-tool", calls=[call("explode-1", "explode")]),
        response("assistant-final", "Recovered from tool failure."),
    ])
    service = coordinator(sessions, tool_calls, model, exploding)
    state = service.create_session(session_id="tool-fail", allowed_tools=frozenset({"explode"}))
    recovered = service.submit_user_message(state.session_id, user("user-2"), expected_version=0)
    assert recovered.status == SessionOutcomeStatus.RESPONSE_READY
    envelope = json.loads(next(item.message.content for item in sessions.messages(
        state.session_id) if item.message.role == "tool"))
    assert envelope["error_code"] == "tool_execution_failed"
    assert "secret" not in json.dumps(envelope)


def test_invalid_tool_arguments_become_message_not_terminal_failure(runtime):
    _, sessions, tool_calls = runtime
    tool = RecordingTool()
    invalid = NormalizedToolCall(
        tool_call_id="invalid", tool_name="read", arguments={})
    model = ScriptedModel([
        response("assistant-invalid", calls=[invalid]),
        response("assistant-final", "Please provide text."),
    ])
    service = coordinator(sessions, tool_calls, model, tool)
    state = service.create_session(session_id="invalid", allowed_tools=frozenset({"read"}))
    outcome = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert outcome.status == SessionOutcomeStatus.RESPONSE_READY
    envelope = json.loads(next(item.message.content for item in sessions.messages(
        state.session_id) if item.message.role == "tool"))
    assert envelope["error_code"] == "invalid_tool_arguments"


def test_persisted_model_and_tool_limits(runtime):
    _, sessions, tool_calls = runtime
    tool = RecordingTool()
    model = ScriptedModel([
        response("assistant-first", calls=[call("first")]),
        response("should-not-run", "no"),
    ])
    service = coordinator(sessions, tool_calls, model, tool)
    state = service.create_session(session_id="iteration-limit",
        allowed_tools=frozenset({"read"}), max_loop_iterations=1)
    limited = service.submit_user_message(state.session_id, user(), expected_version=0)
    assert limited.status == SessionOutcomeStatus.LIMIT_EXCEEDED
    assert limited.state.error_code == "max_loop_iterations_exceeded"
    assert len(model.calls) == 1

    second_tool = RecordingTool()
    model = ScriptedModel([response("assistant-many", calls=[
        call("one", text="one"), call("two", text="two")])])
    service = coordinator(sessions, tool_calls, model, second_tool)
    state = service.create_session(session_id="tool-limit",
        allowed_tools=frozenset({"read"}), max_tool_calls=1)
    limited = service.submit_user_message(state.session_id, user("user-limit"), expected_version=0)
    assert limited.status == SessionOutcomeStatus.LIMIT_EXCEEDED
    assert second_tool.calls == ["one"]
    assert limited.state.error_code == "max_tool_calls_exceeded"


def test_restarted_coordinator_reconstructs_context_before_next_model_call(runtime):
    database, sessions, tool_calls = runtime
    write = ApprovalTool("write")
    first_model = ScriptedModel([
        response("assistant-write", calls=[call("write-call", "write")])])
    first = coordinator(sessions, tool_calls, first_model, write)
    state = first.create_session(session_id="restart", allowed_tools=frozenset({"write"}))
    paused = first.submit_user_message(state.session_id, user(), expected_version=0)

    second_model = ScriptedModel([response("assistant-final", "after restart")])
    restarted = coordinator(
        SessionRepository(database.session_factory),
        ToolCallRepository(database.session_factory), second_model, write)
    outcome = restarted.approve_tool_call(
        state.session_id, paused.pending_tool_call_ids[0],
        expected_version=paused.state.version)
    assert outcome.final_text == "after restart"
    assert [item.role for item in second_model.calls[0][0]] == [
        "system", "user", "assistant", "tool"]


def test_completed_tool_record_is_reused_after_session_message_crash_window(runtime):
    database, sessions, tool_calls = runtime
    tool = RecordingTool()
    model = ScriptedModel([
        response("assistant-tool", calls=[call("read-call", text="once")]),
        response("assistant-final", "recovered"),
    ])
    service = coordinator(sessions, tool_calls, model, tool)
    state = service.create_session(session_id="crash-window", allowed_tools=frozenset({"read"}))
    running = SessionState.model_validate({**state.model_dump(mode="python"),
        "status": SessionStatus.RUNNING})
    running = sessions.save_transition(running, SessionEvent(
        session_id=state.session_id, event_type=SessionEventType.SESSION_RESUMED),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=user())])
    decision = service._loop.decide(
        [item.message for item in sessions.messages(state.session_id)],
        service._context(running))
    assistant = decision.message
    pending = SessionState.model_validate({**running.model_dump(mode="python"),
        "pending_assistant_message_id": assistant.message_id,
        "pending_tool_call_ids": [assistant.tool_calls[0].tool_call_id],
        "loop_iteration": 1})
    pending = sessions.save_transition(pending, SessionEvent(
        session_id=state.session_id,
        event_type=SessionEventType.MODEL_DECISION_PERSISTED),
        expected_version=running.version,
        messages=[SessionMessageDraft(message=assistant)])
    # Simulate process death after ToolExecutor committed, before the session tool
    # message/state transition committed.
    service._loop.process_tool_call(
        assistant, assistant.tool_calls[0], service._context(pending))
    assert tool.calls == ["once"]
    recovered = service.continue_session(
        state.session_id, expected_version=pending.version)
    assert recovered.final_text == "recovered"
    assert tool.calls == ["once"]
    assert recovered.state.executed_tool_calls == 1

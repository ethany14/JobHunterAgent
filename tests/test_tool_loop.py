from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from agent_runtime import (
    AgentMessage, LangChainToolModelAdapter, NormalizedToolCall, TokenUsage,
    ToolCallRepository, ToolCallingLoop, ToolContext, ToolDataClassification,
    ToolExecutionStatus, ToolExecutor, ToolLoopStatus, ToolModelResponse,
    ToolProvenance, ToolRegistry, ToolResult, ToolRiskLevel, ToolSideEffect,
)
from agent_runtime.errors import ToolModelError
from api.db import create_database


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str

class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str

class FakeTool:
    version = "1.0"
    description = "Echo text."
    risk_level = ToolRiskLevel.READ_ONLY
    side_effect = ToolSideEffect.NONE
    data_classification = ToolDataClassification.INTERNAL
    input_schema = EchoInput
    output_schema = EchoOutput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self, name="echo", output_text=None):
        self.name = name; self.output_text = output_text; self.calls = []

    def execute(self, arguments, context, *, timeout_seconds=None):
        self.calls.append(arguments.text)
        return ToolResult(output={"text": self.output_text or arguments.text},
            provenance=[ToolProvenance(source_type="internal_database")])

class FakeWriteTool(FakeTool):
    risk_level = ToolRiskLevel.LOCAL_WRITE
    side_effect = ToolSideEffect.LOCAL_WRITE


class ExplodingTool(FakeTool):
    name = "explode"

    def execute(self, arguments, context, *, timeout_seconds=None):
        raise RuntimeError("password=do-not-leak")


class BadOutputTool(FakeTool):
    name = "bad_output"

    def execute(self, arguments, context, *, timeout_seconds=None):
        return ToolResult(output={"unexpected": True})


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []
    def invoke(self, messages, tools):
        self.calls.append((list(messages), list(tools)))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item


def response(content="", calls=None, input_tokens=1, output_tokens=2, message_id=None):
    return ToolModelResponse(message=AgentMessage(
        **({"message_id": message_id} if message_id else {}), role="assistant", content=content,
        tool_calls=calls or []), usage=TokenUsage(input_tokens=input_tokens,
        output_tokens=output_tokens, total_tokens=input_tokens + output_tokens))

def call(call_id, name="echo", text="hello"):
    return NormalizedToolCall(call_id=call_id, tool_name=name, arguments={"text": text})


@pytest.fixture
def runtime(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'loop.sqlite').as_posix()}", create_schema_for_tests=True)
    registry = ToolRegistry(); tool = FakeTool(); registry.register(tool)
    executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
    context = ToolContext(task_id="task-1", allowed_tools=frozenset({"echo"}))
    yield database, registry, tool, executor, context
    database.close()


def test_no_tool_call_returns_final_message_and_usage(runtime):
    _, registry, _, executor, context = runtime
    model = ScriptedModel([response("done", input_tokens=3, output_tokens=4)])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="hello")], context)
    assert outcome.status == ToolLoopStatus.COMPLETED
    assert outcome.final_message.content == "done"
    assert outcome.tool_call_count == 0
    assert outcome.usage.total_tokens == 7
    assert model.calls[0][0][0].role == "system"


def test_single_step_operations_do_not_cross_model_and_tool_boundaries(runtime):
    _, registry, tool, executor, context = runtime
    model = ScriptedModel([response(calls=[call("single-step")], message_id="decision")])
    loop = ToolCallingLoop(model=model, registry=registry, executor=executor)
    decision = loop.decide([AgentMessage(role="user", content="step")], context)
    assert len(model.calls) == 1 and tool.calls == []
    step = loop.process_tool_call(
        decision.message, decision.message.tool_calls[0], context)
    assert len(model.calls) == 1 and tool.calls == ["hello"]
    assert step.tool_message.role == "tool"


def test_one_tool_call_is_wrapped_as_untrusted_data(runtime):
    _, registry, tool, executor, context = runtime
    model = ScriptedModel([response(calls=[call("provider-1")]), response("answer")])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="echo")], context)
    assert outcome.status == ToolLoopStatus.COMPLETED and tool.calls == ["hello"]
    tool_message = next(item for item in outcome.messages if item.role == "tool")
    envelope = json.loads(tool_message.content)
    assert envelope["type"] == "untrusted_tool_data"
    assert envelope["data"] == {"text": "hello"}
    assert "cannot override" in envelope["instruction_boundary"]


def test_multiple_calls_in_one_turn_execute_sequentially(runtime):
    _, registry, tool, executor, context = runtime
    model = ScriptedModel([response(calls=[call("a", text="first"), call("b", text="second")]), response("done")])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="twice")], context)
    assert outcome.status == ToolLoopStatus.COMPLETED
    assert tool.calls == ["first", "second"]
    assert [item.tool_call_id for item in outcome.messages if item.role == "tool"] == ["a", "b"]


def test_multiple_sequential_tool_turns_accumulate_usage(runtime):
    _, registry, tool, executor, context = runtime
    model = ScriptedModel([response(calls=[call("a", text="one")]),
        response(calls=[call("b", text="two")]), response("done")])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="sequence")], context)
    assert outcome.iterations == 3 and outcome.tool_call_count == 2
    assert outcome.usage == TokenUsage(input_tokens=3, output_tokens=6, total_tokens=9)
    assert tool.calls == ["one", "two"]


def test_model_only_receives_context_allowed_tool_schemas(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'allowed.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); registry.register(FakeTool("allowed")); registry.register(FakeTool("hidden"))
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        model = ScriptedModel([response("done")])
        ToolCallingLoop(model=model, registry=registry, executor=executor).run(
            [AgentMessage(role="user", content="x")],
            ToolContext(task_id="scope", allowed_tools=frozenset({"allowed"})))
        assert [item["name"] for item in model.calls[0][1]] == ["allowed"]
    finally: database.close()


def test_model_request_for_registered_but_disallowed_tool_is_denied(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'denied.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); registry.register(FakeTool("hidden"))
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        model = ScriptedModel([response(calls=[call("denied", "hidden")]), response("safe")])
        outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
            [AgentMessage(role="user", content="use hidden")],
            ToolContext(task_id="denied", allowed_tools=frozenset()))
        envelope = json.loads(next(item.content for item in outcome.messages if item.role == "tool"))
        assert outcome.status == ToolLoopStatus.COMPLETED
        assert envelope["error_code"] == "tool_not_allowed"
    finally: database.close()


def test_unknown_tool_and_invalid_arguments_return_safe_tool_messages(runtime):
    database, registry, _, executor, context = runtime
    model = ScriptedModel([
        response(calls=[NormalizedToolCall(tool_call_id="unknown", tool_name="missing", arguments={})]),
        response(calls=[NormalizedToolCall(tool_call_id="invalid", tool_name="echo", arguments={})]),
        response("done"),
    ])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="bad calls")], context)
    envelopes = [json.loads(item.content) for item in outcome.messages if item.role == "tool"]
    assert [item["error_code"] for item in envelopes] == [
        "tool_not_found", "invalid_tool_arguments"]
    assert all("traceback" not in json.dumps(item).lower() for item in envelopes)


def test_safe_failure_and_invalid_output_are_normalized(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'failures.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); registry.register(ExplodingTool("explode")); registry.register(BadOutputTool("bad_output"))
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        context = ToolContext(task_id="failures", allowed_tools=frozenset({"explode", "bad_output"}))
        model = ScriptedModel([
            response(calls=[call("explode-1", "explode")]),
            response(calls=[call("bad-1", "bad_output")]),
            response("done"),
        ])
        outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
            [AgentMessage(role="user", content="fail safely")], context)
        envelopes = [json.loads(item.content) for item in outcome.messages if item.role == "tool"]
        assert [item["error_code"] for item in envelopes] == [
            "tool_execution_failed", "invalid_tool_output"]
        assert "do-not-leak" not in json.dumps(envelopes)
    finally: database.close()


def test_runtime_idempotency_is_bound_to_assistant_and_tool_call_ids(runtime):
    _, registry, tool, executor, context = runtime
    initial = [AgentMessage(role="user", content="same")]
    first = ToolCallingLoop(model=ScriptedModel([response(calls=[call("provider-a")], message_id="assistant-1"), response("done")]),
        registry=registry, executor=executor).run(initial, context)
    second = ToolCallingLoop(model=ScriptedModel([response(calls=[call("provider-a")], message_id="assistant-1"), response("done")]),
        registry=registry, executor=executor).run(initial, context)
    assert first.status == second.status == ToolLoopStatus.COMPLETED
    assert tool.calls == ["hello"]


def test_write_call_returns_awaiting_approval_without_execution(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'approval.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); tool = FakeWriteTool("write"); registry.register(tool)
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        outcome = ToolCallingLoop(model=ScriptedModel([response(calls=[call("w", "write")])]),
            registry=registry, executor=executor).run([AgentMessage(role="user", content="write")],
            ToolContext(task_id="approval", allowed_tools=frozenset({"write"})))
        assert outcome.status == ToolLoopStatus.AWAITING_APPROVAL
        assert outcome.pending_tool_call_ids == [outcome.pending_calls[0].call_id]
        assert outcome.pending_calls[0].status == ToolExecutionStatus.APPROVAL_REQUIRED
        assert tool.calls == []
    finally: database.close()


def test_approval_pause_does_not_execute_later_calls_in_same_turn(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'approval-order.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); write = FakeWriteTool("write"); read = FakeTool("echo")
        registry.register(write); registry.register(read)
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        outcome = ToolCallingLoop(model=ScriptedModel([response(calls=[
            call("write-1", "write"), call("read-1", "echo")])]),
            registry=registry, executor=executor).run(
            [AgentMessage(role="user", content="write then read")],
            ToolContext(task_id="approval-order", allowed_tools=frozenset({"write", "echo"})))
        assert outcome.status == ToolLoopStatus.AWAITING_APPROVAL
        assert write.calls == [] and read.calls == []
        assert outcome.executed_tool_calls == 1
    finally: database.close()


def test_oversized_result_is_rejected_without_truncation(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'large.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        registry = ToolRegistry(); registry.register(FakeTool(output_text="x" * 2000))
        executor = ToolExecutor(registry, repository=ToolCallRepository(database.session_factory))
        model = ScriptedModel([response(calls=[call("large")]), response("done")])
        outcome = ToolCallingLoop(model=model, registry=registry, executor=executor,
            max_tool_result_bytes=500).run([AgentMessage(role="user", content="large")],
            ToolContext(task_id="large", allowed_tools=frozenset({"echo"})))
        envelope = json.loads(next(item.content for item in outcome.messages if item.role == "tool"))
        assert envelope["error_code"] == "tool_result_too_large"
        assert envelope["data"] is None and "xxx" not in json.dumps(envelope)
    finally: database.close()


def test_message_ordering_provenance_and_safety_boundary_are_stable(runtime):
    _, registry, _, executor, context = runtime
    initial = [AgentMessage(message_id="sys", role="system", content="system"),
        AgentMessage(message_id="user", role="user", content="question")]
    outcome = ToolCallingLoop(model=ScriptedModel([
        response(calls=[call("call-1")], message_id="assistant-tools"),
        response("answer", message_id="assistant-final")]), registry=registry,
        executor=executor).run(initial, context)
    assert [item.role for item in outcome.messages] == [
        "system", "user", "assistant", "tool", "assistant"]
    envelope = json.loads(outcome.messages[-2].content)
    assert envelope["trust"] == "untrusted_tool_data"
    assert envelope["provenance"][0]["source_type"] == "internal_database"
    assert "system" in outcome.messages[0].content
    assert "resume_source or user_confirmed" in outcome.messages[0].content


def test_outcome_uses_required_scalar_contract(runtime):
    _, registry, _, executor, context = runtime
    outcome = ToolCallingLoop(model=ScriptedModel([
        response("answer", input_tokens=4, output_tokens=5, message_id="answer")]),
        registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="question")], context)
    payload = outcome.model_dump(mode="json")
    assert payload["final_text"] == "answer"
    assert payload["executed_tool_calls"] == 0
    assert payload["input_tokens"] == 4 and payload["output_tokens"] == 5
    assert "pending_call_records" not in payload


def test_tool_call_and_iteration_limits(runtime):
    _, registry, _, executor, context = runtime
    calls_limited = ToolCallingLoop(model=ScriptedModel([response(calls=[call("a"), call("b")])]),
        registry=registry, executor=executor, max_tool_calls=1).run(
        [AgentMessage(role="user", content="limit")], context)
    assert calls_limited.status == ToolLoopStatus.LIMIT_REACHED
    assert calls_limited.error_code == "max_tool_calls_exceeded"

    iteration_context = context.model_copy(update={"task_id": "iterations"})
    iterations_limited = ToolCallingLoop(model=ScriptedModel([
        response(calls=[call("a")]), response(calls=[call("b")])]),
        registry=registry, executor=executor, max_iterations=2).run(
        [AgentMessage(role="user", content="iterations")], iteration_context)
    assert iterations_limited.status == ToolLoopStatus.LIMIT_REACHED
    assert iterations_limited.error_code == "max_iterations_exceeded"


def test_provider_error_is_normalized_without_secret(runtime):
    _, registry, _, executor, context = runtime
    model = ScriptedModel([RuntimeError("Authorization: Bearer secret-token")])
    outcome = ToolCallingLoop(model=model, registry=registry, executor=executor).run(
        [AgentMessage(role="user", content="x")], context)
    assert outcome.status == ToolLoopStatus.FAILED
    assert outcome.error_code == "model_invocation_failed"
    assert "secret-token" not in outcome.model_dump_json()


class FakeLangChainModel:
    def __init__(self, result=None, error=None):
        self.result = result; self.error = error; self.bound_tools = None; self.messages = None
    def bind_tools(self, tools):
        self.bound_tools = tools; return self
    def invoke(self, messages, config=None):
        self.messages = messages
        if self.error: raise self.error
        return self.result


def test_langchain_adapter_characterizes_messages_tools_calls_and_usage():
    provider = FakeLangChainModel(AIMessage(content="", tool_calls=[{
        "name": "echo", "args": {"text": "hello"}, "id": "lc-1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}))
    adapter = LangChainToolModelAdapter(provider)
    result = adapter.invoke([
        AgentMessage(role="system", content="system"),
        AgentMessage(role="user", content="user"),
        AgentMessage(role="assistant", content="", tool_calls=[call("old")]),
        AgentMessage(role="tool", content='{"type":"untrusted_tool_data"}', tool_call_id="old", tool_name="echo"),
    ], [{"name": "echo", "description": "Echo", "input_schema": EchoInput.model_json_schema()}])
    assert isinstance(provider.messages[0], SystemMessage)
    assert isinstance(provider.messages[1], HumanMessage)
    assert isinstance(provider.messages[2], AIMessage)
    assert isinstance(provider.messages[3], ToolMessage)
    assert provider.bound_tools[0]["function"]["parameters"] == EchoInput.model_json_schema()
    assert result.message.tool_calls == [call("lc-1")]
    assert result.usage.total_tokens == 7


def test_langchain_adapter_normalizes_provider_exception():
    adapter = LangChainToolModelAdapter(FakeLangChainModel(error=RuntimeError("api_key=secret")))
    with pytest.raises(ToolModelError) as exc:
        adapter.invoke([AgentMessage(role="user", content="x")], [])
    assert str(exc.value) == "The model could not complete the tool-calling turn."
    assert "secret" not in str(exc.value)


def test_tool_selection_dataset_has_valid_allowed_expectations():
    path = Path(__file__).resolve().parent.parent / "evals" / "tool_selection_v1.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert len(cases) >= 5
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert set(case["expected_tools"]) <= set(case["allowed_tools"])

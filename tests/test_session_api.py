from __future__ import annotations

from collections import deque

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from agent_runtime.executor import ToolExecutor
from agent_runtime.policy import ToolPolicy
from agent_runtime.registry import ToolRegistry
from agent_runtime.repository import ToolCallRepository
from agent_runtime.sessions.claims import SessionClaimRepository
from agent_runtime.sessions.coordinator import SessionCoordinator
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionMessageDraft, SessionMessageVisibility, SessionStatus
from agent_runtime.tools.loop import ToolCallingLoop
from agent_runtime.tools.messages import AgentMessage, NormalizedToolCall, ToolModelResponse
from agent_runtime.types import (
    ToolContext,
    ToolDataClassification,
    ToolProvenance,
    ToolResult,
    ToolRiskLevel,
    ToolSideEffect,
)
from api.db import create_database, upgrade_database
from api.main import create_app
from api.repositories.run_repository import RunRepository
from api.session_dependencies import JOB_ASSISTANT_READONLY_TOOLS, SessionRuntime


class AnyInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class AnyOutput(BaseModel):
    model_config = ConfigDict(extra="allow")


class FakeTool:
    version = "test-1"
    description = "A test-only tool."
    data_classification = ToolDataClassification.SENSITIVE
    input_schema = AnyInput
    output_schema = AnyOutput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self, name: str, *, write: bool = False):
        self.name = name
        self.risk_level = ToolRiskLevel.LOCAL_WRITE if write else ToolRiskLevel.READ_ONLY
        self.side_effect = ToolSideEffect.LOCAL_WRITE if write else ToolSideEffect.NONE

    def execute(self, arguments, context: ToolContext, *, timeout_seconds=None):
        return ToolResult(
            output={"ok": True},
            provenance=[ToolProvenance(source_type="internal_database")],
        )


class ScriptedModel:
    def __init__(self, responses=()):
        self.responses = deque(responses)

    def invoke(self, messages, tools, *, timeout_seconds=None):
        if not self.responses:
            return ToolModelResponse(
                message=AgentMessage(role="assistant", content="Done.")
            )
        return self.responses.popleft()


def assistant(content="Done.", *, calls=()):
    return ToolModelResponse(
        message=AgentMessage(role="assistant", content=content, tool_calls=list(calls))
    )


def build_runtime(tmp_path, responses=(), *, write_render=False):
    url = f"sqlite:///{(tmp_path / 'session-api.sqlite').as_posix()}"
    upgrade_database(url)
    database = create_database(url)
    registry = ToolRegistry()
    for name in sorted(JOB_ASSISTANT_READONLY_TOOLS):
        registry.register(FakeTool(name, write=write_render and name == "render_tailored_resume"))
    tool_calls = ToolCallRepository(database.session_factory)
    executor = ToolExecutor(registry, policy=ToolPolicy(), repository=tool_calls)
    loop = ToolCallingLoop(model=ScriptedModel(responses), registry=registry, executor=executor)
    sessions = SessionRepository(database.session_factory)
    claims = SessionClaimRepository(database.session_factory)
    coordinator = SessionCoordinator(
        sessions=sessions,
        tool_calls=tool_calls,
        executor=executor,
        loop=loop,
        claims=claims,
    )
    from agent_runtime.tools.run_reader import SqlAlchemyRunReader

    return SessionRuntime(
        database,
        sessions,
        tool_calls,
        registry,
        coordinator,
        SqlAlchemyRunReader(database.session_factory),
        claims,
    )


@pytest.fixture
def client_and_runtime(tmp_path):
    runtime = build_runtime(tmp_path)
    app = create_app(run_service=object(), session_runtime=runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, runtime
    runtime.close()


def create_session(client):
    response = client.post(
        "/sessions", json={"capability_profile": "job_assistant_readonly", "title": "Jobs"}
    )
    assert response.status_code == 201
    return response.json()["session"]


def test_create_get_and_profile_is_server_owned(client_and_runtime):
    client, runtime = client_and_runtime
    session = create_session(client)
    stored = runtime.sessions.require(session["session_id"])
    assert stored.allowed_tools == JOB_ASSISTANT_READONLY_TOOLS
    assert client.get(f"/sessions/{stored.session_id}").json()["session"]["status"] == "active"
    assert client.post("/sessions", json={"capability_profile": "anything"}).status_code == 422
    assert client.post("/sessions", json={"allowed_tools": ["danger"]}).status_code == 422


def test_list_sessions_returns_safe_recent_summaries(client_and_runtime):
    client, _ = client_and_runtime
    first = create_session(client)
    second = create_session(client)
    response = client.get("/sessions?limit=1")
    assert response.status_code == 200
    summaries = response.json()["sessions"]
    assert len(summaries) == 1
    assert summaries[0]["session_id"] == second["session_id"]
    assert set(summaries[0]) == {
        "session_id", "title", "status", "active_run_id", "message_count",
        "created_at", "updated_at",
    }
    assert first["session_id"] != second["session_id"]
    assert client.get("/sessions?limit=0").status_code == 422


def test_create_session_can_be_associated_with_an_existing_run(client_and_runtime):
    client, runtime = client_and_runtime
    RunRepository(runtime.database.session_factory).create(
        run_id="run-for-assistant",
        thread_id="run-for-assistant",
        resume_text="Resume",
        job_description="Job",
        backend="custom",
        backend_source="test",
    )
    response = client.post(
        "/sessions",
        json={
            "capability_profile": "job_assistant_readonly",
            "active_run_id": "run-for-assistant",
        },
    )
    assert response.status_code == 201
    assert response.json()["session"]["active_run_id"] == "run-for-assistant"
    assert response.json()["session"]["recovery_available"] is False
    missing = client.post(
        "/sessions",
        json={
            "capability_profile": "job_assistant_readonly",
            "active_run_id": "missing",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "run_not_found"


def test_submit_message_is_persisted_idempotent_and_public_messages_are_filtered(tmp_path):
    runtime = build_runtime(tmp_path, [assistant("Safe answer")])
    app = create_app(run_service=object(), session_runtime=runtime)
    try:
        with TestClient(app) as client:
            session = create_session(client)
            body = {"message_id": "client-1", "content": "Show recent runs", "expected_version": session["version"]}
            first = client.post(f"/sessions/{session['session_id']}/messages", json=body)
            assert first.status_code == 200
            assert first.json()["response"] == "Safe answer"
            # A transport retry may carry the original expected_version.
            repeated = client.post(f"/sessions/{session['session_id']}/messages", json=body)
            assert repeated.status_code == 200
            result = client.get(f"/sessions/{session['session_id']}/messages").json()
            assert [item["role"] for item in result["messages"]] == ["user", "assistant"]
            assert all("tool_calls" not in item for item in result["messages"])
    finally:
        runtime.close()


def test_approval_uses_persisted_redacted_arguments_and_resumes(tmp_path):
    call = NormalizedToolCall(
        tool_call_id="provider-call",
        tool_name="render_tailored_resume",
        arguments={"run_id": "run-1", "password": "do-not-store"},
    )
    runtime = build_runtime(
        tmp_path,
        [assistant("", calls=[call]), assistant("Approved result")],
        write_render=True,
    )
    app = create_app(run_service=object(), session_runtime=runtime)
    try:
        with TestClient(app) as client:
            session = create_session(client)
            paused = client.post(
                f"/sessions/{session['session_id']}/messages",
                json={"message_id": "m-approve", "content": "Render", "expected_version": session["version"]},
            )
            assert paused.status_code == 200
            payload = paused.json()
            assert payload["pending_tool_approvals"], payload
            approval = payload["pending_tool_approvals"][0]
            assert approval["arguments"]["password"] == "[REDACTED]"
            assert approval["side_effect"] == "local_write"
            assert approval["data_classification"] == "sensitive"
            approved = client.post(
                f"/sessions/{session['session_id']}/tool-calls/{approval['call_id']}/approve",
                json={"expected_version": payload["session"]["version"]},
            )
            assert approved.status_code == 200
            assert approved.json()["response"] == "Approved result"
            visible = client.get(f"/sessions/{session['session_id']}/messages").json()
            assert all(item["role"] in {"user", "assistant"} for item in visible["messages"])
            assert all("do-not-store" not in item["content"] for item in visible["messages"])
    finally:
        runtime.close()


def test_reject_cancel_recover_and_stable_errors(tmp_path):
    call = NormalizedToolCall(
        tool_call_id="reject-call", tool_name="render_tailored_resume", arguments={"run_id": "r"}
    )
    runtime = build_runtime(tmp_path, [assistant("", calls=[call]), assistant("Rejected safely")], write_render=True)
    app = create_app(run_service=object(), session_runtime=runtime)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            session = create_session(client)
            paused = client.post(
                f"/sessions/{session['session_id']}/messages",
                json={"message_id": "m-reject", "content": "Render", "expected_version": session["version"]},
            ).json()
            assert paused["pending_tool_approvals"], paused
            call_id = paused["pending_tool_approvals"][0]["call_id"]
            rejected = client.post(
                f"/sessions/{session['session_id']}/tool-calls/{call_id}/reject",
                json={"expected_version": paused["session"]["version"]},
            )
            assert rejected.status_code == 200
            assert rejected.json()["response"] == "Rejected safely"
            tool_call = rejected.json()["tool_calls"][0]
            assert tool_call["provider"] == "Built-in"
            assert tool_call["mcp_server_id"] is None
            assert tool_call["approval_status"] == "rejected"
            assert tool_call["status"] == "denied"
            current = rejected.json()["session"]
            cancelled = client.post(
                f"/sessions/{session['session_id']}/cancel",
                json={"expected_version": current["version"], "reason": "No longer needed"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["session"]["status"] == "cancelled"
            assert client.get("/sessions/missing").status_code == 404
            stale = client.post(
                f"/sessions/{session['session_id']}/cancel",
                json={"expected_version": 0, "reason": "again"},
            )
            # Already-cancelled cancellation remains idempotent.
            assert stale.status_code == 200
    finally:
        runtime.close()


def test_recover_requires_current_version(tmp_path):
    runtime = build_runtime(tmp_path, [assistant("Recovered")])
    app = create_app(run_service=object(), session_runtime=runtime)
    try:
        with TestClient(app) as client:
            session = create_session(client)
            state = runtime.sessions.require(session["session_id"])
            running = state.model_validate({**state.model_dump(mode="python"), "status": SessionStatus.RUNNING})
            running = runtime.sessions.save_transition(
                running,
                SessionEvent(session_id=state.session_id, event_type=SessionEventType.SESSION_RESUMED),
                expected_version=state.version,
            )
            assert client.post(
                f"/sessions/{state.session_id}/recover", json={"expected_version": 0}
            ).status_code == 409
            recovered = client.post(
                f"/sessions/{state.session_id}/recover", json={"expected_version": running.version}
            )
            assert recovered.status_code == 200
            assert recovered.json()["response"] == "Recovered"
    finally:
        runtime.close()


def test_unknown_failures_return_generic_safe_500(client_and_runtime):
    client, runtime = client_and_runtime

    def explode(_session_id):
        raise RuntimeError("password=secret C:\\private\\database.sqlite")

    runtime.sessions.require = explode
    response = client.get("/sessions/boom")
    assert response.status_code == 500
    body = response.text
    assert "secret" not in body
    assert "database.sqlite" not in body
    assert response.json() == {
        "detail": {
            "code": "internal_error",
            "message": "The request could not be completed.",
        }
    }

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from agent_runtime.models import ToolCallRow
from agent_runtime import (
    ToolCallRepository,
    ToolCallRequest,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
)
from agent_runtime.sessions import (
    MessageConflictError,
    SessionEvent,
    SessionEventType,
    SessionMessageDraft,
    SessionMessageVisibility,
    SessionRepository,
    SessionState,
    SessionStatus,
    SessionTerminalError,
    StaleSessionError,
)
from agent_runtime.sessions.models import AgentSessionRow
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.tools import SqlAlchemyRunReader, register_builtin_job_agent_tools
from api.db import create_database
from api.models import Run


def database_url(path):
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def session_runtime(tmp_path):
    database = create_database(
        database_url(tmp_path / "sessions.sqlite"), create_schema_for_tests=True
    )
    yield database, SessionRepository(database.session_factory)
    database.close()


def create_session(repository, session_id="session-1", messages=()):
    return repository.create(
        SessionState(
            session_id=session_id,
            user_id="user-1",
            title="Job search",
            allowed_tools=frozenset({"list_recent_runs"}),
        ),
        SessionEvent(
            session_id=session_id,
            sequence=999,
            event_type=SessionEventType.SESSION_CREATED,
        ),
        messages=messages,
    )


def transition(repository, state, *, status=SessionStatus.RUNNING, event_id=None, messages=()):
    updates = {"status": status}
    if status in {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.TIMED_OUT,
    }:
        updates["terminal_reason"] = status.value
    else:
        updates["terminal_reason"] = None
    if status == SessionStatus.CANCELLED:
        updates.update(
            cancel_requested=True,
            cancel_requested_at=datetime.now(UTC),
            cancel_reason="test cancellation",
        )
    desired = SessionState.model_validate(
        {**state.model_dump(mode="python"), **updates}
    )
    event = SessionEvent(
        **({"event_id": event_id} if event_id else {}),
        session_id=state.session_id,
        sequence=777,
        event_type=SessionEventType.STATUS_CHANGED,
    )
    return repository.save_transition(
        desired, event, expected_version=state.version, messages=messages
    )


@pytest.mark.parametrize(
    "values",
    [
        {"status": SessionStatus.AWAITING_TOOL_APPROVAL},
        {"status": SessionStatus.RUNNING, "pending_tool_call_ids": ["call-1"]},
        {"status": SessionStatus.FAILED},
        {"status": SessionStatus.ACTIVE, "error_message": "safe"},
        {
            "status": SessionStatus.AWAITING_TOOL_APPROVAL,
            "pending_tool_call_ids": ["call-1", "call-1"],
            "pending_assistant_message_id": "assistant-1",
        },
        {"pending_assistant_message_id": "assistant-without-calls"},
    ],
)
def test_session_state_rejects_invalid_invariants(values):
    with pytest.raises(ValidationError):
        SessionState(session_id="invalid", **values)


def test_session_state_accepts_all_valid_statuses():
    for status in SessionStatus:
        values = {"session_id": status.value, "status": status}
        if status == SessionStatus.AWAITING_TOOL_APPROVAL:
            values["pending_tool_call_ids"] = ["call-1"]
            values["pending_assistant_message_id"] = "assistant-1"
        if status == SessionStatus.FAILED:
            values["error_code"] = "safe_failure"
        if status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.TIMED_OUT,
        }:
            values["terminal_reason"] = status.value
        if status == SessionStatus.CANCELLED:
            values.update(
                cancel_requested=True,
                cancel_requested_at=datetime.now(UTC),
                cancel_reason="test cancellation",
            )
        assert SessionState(**values).status == status


def test_legacy_terminal_state_is_upgraded_compatibly():
    legacy = SessionState.model_validate(
        {
            "schema_version": 1,
            "session_id": "legacy-cancelled",
            "status": "cancelled",
        }
    )
    assert legacy.status == SessionStatus.CANCELLED
    assert legacy.terminal_reason == "legacy_cancelled"
    assert legacy.cancel_requested is True


def test_repository_assigns_message_and_event_order(session_runtime):
    _, repository = session_runtime
    drafts = [
        SessionMessageDraft(message=AgentMessage(
            message_id="m-1", role="system", content="System")),
        SessionMessageDraft(message=AgentMessage(
            message_id="m-2", role="user", content="User")),
    ]
    state = create_session(repository, messages=drafts)
    state = transition(repository, state, messages=[SessionMessageDraft(
        message=AgentMessage(message_id="m-3", role="assistant", content="Answer"))])
    stored = repository.messages(state.session_id)
    assert [item.message_id for item in stored] == ["m-1", "m-2", "m-3"]
    assert [item.sequence for item in stored] == [1, 2, 3]
    assert state.message_sequence == 3
    events = repository.events(state.session_id)
    assert [item.sequence for item in events] == [1, 2]
    assert events[0].event_type == SessionEventType.SESSION_CREATED
    assert all(item.occurred_at.tzinfo is not None for item in events)


def test_duplicate_message_is_idempotent_without_sequence_increment(session_runtime):
    _, repository = session_runtime
    draft = SessionMessageDraft(message=AgentMessage(
        message_id="same", role="user", content="same content"))
    state = create_session(repository, messages=[draft])
    updated = transition(repository, state, messages=[draft, draft])
    assert updated.message_sequence == 1
    assert len(repository.messages(state.session_id)) == 1
    assert repository.messages(state.session_id)[0].content_hash


def test_message_id_with_different_content_conflicts_and_rolls_back(session_runtime):
    _, repository = session_runtime
    state = create_session(repository, messages=[SessionMessageDraft(
        message=AgentMessage(message_id="same", role="user", content="first"))])
    conflicting = SessionMessageDraft(message=AgentMessage(
        message_id="same", role="user", content="different"))
    with pytest.raises(MessageConflictError):
        transition(repository, state, messages=[conflicting])
    stored = repository.require(state.session_id)
    assert stored.version == 0 and stored.message_sequence == 1
    assert len(repository.events(state.session_id)) == 1


def test_optimistic_concurrency_rejects_stale_version(session_runtime):
    _, repository = session_runtime
    state = create_session(repository)
    transition(repository, state)
    with pytest.raises(StaleSessionError):
        transition(repository, state)


def test_event_failure_rolls_back_state_and_messages(session_runtime):
    _, repository = session_runtime
    event_id = "duplicate-event"
    state = repository.create(
        SessionState(session_id="rollback"),
        SessionEvent(event_id=event_id, session_id="rollback",
            event_type=SessionEventType.SESSION_CREATED),
    )
    with pytest.raises(IntegrityError):
        transition(repository, state, event_id=event_id, messages=[
            SessionMessageDraft(message=AgentMessage(
                message_id="rolled-back", role="user", content="rollback"))])
    stored = repository.require("rollback")
    assert stored.version == 0 and stored.message_sequence == 0
    assert repository.messages("rollback") == []
    assert len(repository.events("rollback")) == 1


def test_terminal_session_cannot_continue(session_runtime):
    _, repository = session_runtime
    state = create_session(repository)
    completed = transition(repository, state, status=SessionStatus.COMPLETED)
    with pytest.raises(SessionTerminalError):
        transition(repository, completed)


def test_visibility_and_task_private_filters(session_runtime):
    _, repository = session_runtime
    state = create_session(repository, messages=[
        SessionMessageDraft(message=AgentMessage(
            message_id="session", role="user", content="session")),
        SessionMessageDraft(message=AgentMessage(
            message_id="shared", role="user", content="shared"),
            visibility=SessionMessageVisibility.SHARED),
        SessionMessageDraft(message=AgentMessage(
            message_id="task-a", role="user", content="private a"),
            task_id="task-a", visibility=SessionMessageVisibility.TASK_PRIVATE),
        SessionMessageDraft(message=AgentMessage(
            message_id="task-b", role="user", content="private b"),
            task_id="task-b", visibility=SessionMessageVisibility.TASK_PRIVATE),
    ])
    assert [item.message_id for item in repository.messages(state.session_id)] == [
        "session", "shared"]
    assert [item.message_id for item in repository.messages(
        state.session_id, visibility=SessionMessageVisibility.SHARED)] == ["shared"]
    assert [item.message_id for item in repository.messages(
        state.session_id, task_id="task-a")] == ["session", "shared", "task-a"]
    assert [item.message_id for item in repository.messages(
        state.session_id, visibility=SessionMessageVisibility.TASK_PRIVATE,
        task_id="task-b")] == ["task-b"]


def test_session_delete_cascades_messages_and_events_but_not_runs_or_tools(session_runtime):
    database, repository = session_runtime
    state = create_session(repository, session_id="cascade", messages=[
        SessionMessageDraft(message=AgentMessage(
            message_id="cascade-message", role="user", content="hello"))])
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        session.add(Run(run_id="run-kept", thread_id="run-kept", status="running",
            backend="custom", backend_source="explicit_new_run", resume_text="R",
            job_description="J"))
        session.add(ToolCallRow(call_id="tool-kept", scope_type="session",
            scope_id=state.session_id, tool_name="list_recent_runs", tool_version="1",
            idempotency_key="key", arguments_hash="0" * 64, arguments_json="{}",
            risk_level="read_only", status="requested", retryable=False,
            attempt_count=0, max_attempts=1, version=0, event_sequence=0,
            created_at=now, updated_at=now))
    with database.session_factory.begin() as session:
        session.delete(session.get(AgentSessionRow, state.session_id))
    with database.session_factory() as session:
        assert session.get(Run, "run-kept") is not None
        assert session.get(ToolCallRow, "tool-kept") is not None
    assert repository.messages(state.session_id) == []
    assert repository.events(state.session_id) == []


def test_existing_tool_runtime_persists_session_scope_without_foreign_key(session_runtime):
    database, repository = session_runtime
    state = create_session(repository, session_id="tool-scope")
    registry = register_builtin_job_agent_tools(
        ToolRegistry(), SqlAlchemyRunReader(database.session_factory)
    )
    executor = ToolExecutor(
        registry, repository=ToolCallRepository(database.session_factory)
    )
    record = executor.execute(
        ToolCallRequest(
            tool_name="list_recent_runs",
            arguments={"limit": 1},
            idempotency_key="session-scope-test",
        ),
        ToolContext(
            session_id=state.session_id,
            allowed_tools=frozenset({"list_recent_runs"}),
        ),
    )
    assert record.scope_type == "session"
    assert record.scope_id == state.session_id


def test_migration_upgrades_old_database_without_changing_existing_runs(tmp_path):
    url = database_url(tmp_path / "old.sqlite")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0004_backend_cutover")
    database = create_database(url)
    with database.session_factory.begin() as session:
        session.add(Run(run_id="old-run", thread_id="old-run", status="running",
            backend="custom", backend_source="explicit_new_run", resume_text="R",
            job_description="J"))
    database.close()
    command.upgrade(config, "head")
    database = create_database(url)
    try:
        tables = set(inspect(database.engine).get_table_names())
        assert {"agent_sessions", "agent_session_messages",
            "agent_session_events"} <= tables
        with database.session_factory() as session:
            assert session.get(Run, "old-run") is not None
    finally:
        database.close()


def test_resumability_migration_preserves_existing_session_snapshot(tmp_path):
    url = database_url(tmp_path / "session-v1.sqlite")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0005_session_runtime")
    state = SessionState(session_id="old-session", title="Existing")
    database = create_database(url)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            """INSERT INTO agent_sessions (
                session_id,schema_version,user_id,title,status,version,
                event_sequence,message_sequence,active_run_id,
                pending_tool_call_ids_json,allowed_tools_json,loop_iteration,
                executed_tool_calls,total_input_tokens,total_output_tokens,
                error_code,error_message,state_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                state.session_id, state.schema_version, None, state.title,
                state.status.value, 0, 0, 0, None, "[]", "[]", 0, 0, 0, 0,
                None, None, state.model_dump_json(), state.created_at.isoformat(),
                state.updated_at.isoformat(),
            ),
        )
    database.close()
    command.upgrade(config, "head")
    database = create_database(url)
    try:
        columns = {item["name"] for item in inspect(database.engine).get_columns(
            "agent_sessions")}
        assert {"pending_assistant_message_id", "max_loop_iterations",
            "max_tool_calls"} <= columns
        with database.engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT title,max_loop_iterations,max_tool_calls "
                "FROM agent_sessions WHERE session_id='old-session'"
            ).one()
        assert tuple(row) == ("Existing", 6, 10)
    finally:
        database.close()

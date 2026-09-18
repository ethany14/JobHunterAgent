from typing import ClassVar
import json

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from api.db import create_database, upgrade_database

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime import (
    RESUME_EVIDENCE_SOURCE_TYPES,
    ToolCallRequest,
    ToolContext,
    ToolExecutionStatus,
    ToolExecutor,
    ToolCallRepository,
    ToolProvenance,
    ToolRegistry,
    ToolResult,
    ToolRiskLevel,
    provenance_may_support_resume_claim,
)
from agent_runtime.errors import DuplicateToolError, UnknownToolError
from agent_runtime.errors import IdempotencyConflictError, ApprovalBindingError, RetryableToolError, StaleToolCallError, ToolTimeoutError
from agent_runtime.models import ToolCallEventRow
from agent_runtime.security import REDACTED, arguments_hash


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)


class WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    api_key: str | None = None


class FakeReadTool:
    name = "search_resume"
    version = "1.0"
    description = "Search grounded resume text."
    risk_level = ToolRiskLevel.READ_ONLY
    input_schema = ReadArguments

    def __init__(self) -> None:
        self.calls: list[tuple[ReadArguments, ToolContext]] = []

    def execute(
        self, arguments: ReadArguments, context: ToolContext
    ) -> ToolResult:
        self.calls.append((arguments, context))
        return ToolResult(
            output={"text": arguments.query},
            provenance=[
                ToolProvenance(
                    source_type="resume_source",
                    source_id="resume-1",
                )
            ],
        )


class FakeWriteTool:
    version = "1.0"
    description = "Write test content."
    input_schema = WriteArguments

    def __init__(self, name: str, risk_level: ToolRiskLevel) -> None:
        self.name = name
        self.risk_level = risk_level
        self.calls: list[WriteArguments] = []

    def execute(
        self, arguments: WriteArguments, context: ToolContext
    ) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult(
            output={"written": arguments.content},
            provenance=[ToolProvenance(source_type="tool_generated")],
        )


class ExplodingReadTool:
    name = "explode"
    version = "1.0"
    description = "Raise a test exception."
    risk_level = ToolRiskLevel.READ_ONLY
    input_schema = ReadArguments
    secret_message: ClassVar[str] = "internal database password was exposed"

    def execute(
        self, arguments: ReadArguments, context: ToolContext
    ) -> ToolResult:
        raise RuntimeError(self.secret_message)


class OutputContract(BaseModel):
    value: int


class InvalidOutputTool(FakeReadTool):
    name = "invalid_output"
    output_schema = OutputContract

    def execute(self, arguments, context):
        return ToolResult(output={"value": "not-an-integer"})


def context(*tools: str) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        session_id="session-1",
        task_id="task-1",
        user_id="user-1",
        agent_name="test-agent",
        attempt_id="attempt-1",
        allowed_tools=frozenset(tools),
    )


def test_registry_registers_and_gets_tool():
    registry = ToolRegistry()
    tool = FakeReadTool()

    registry.register(tool)

    assert registry.get(tool.name) is tool
    assert registry.allowed_for(context(tool.name)) == [tool]
    assert registry.allowed_for(context()) == []


def test_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(FakeReadTool())

    with pytest.raises(DuplicateToolError, match="already registered"):
        registry.register(FakeReadTool())


def test_registry_get_rejects_unknown_tool():
    with pytest.raises(UnknownToolError, match="not registered"):
        ToolRegistry().get("missing")


def test_executor_returns_safe_unknown_tool_record():
    record = ToolExecutor(ToolRegistry()).execute(
        ToolCallRequest(tool_name="missing", arguments={}),
        context("missing"),
    )

    assert record.status == ToolExecutionStatus.DENIED
    assert record.error_code == "unknown_tool"
    assert "missing" not in record.error_message


def test_context_allowlist_denies_tool_before_execution():
    tool = FakeReadTool()
    registry = ToolRegistry()
    registry.register(tool)

    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"query": "Python"}),
        context(),
    )

    assert record.status == ToolExecutionStatus.DENIED
    assert record.error_code == "tool_not_allowed"
    assert tool.calls == []


def test_allowlisted_read_only_tool_executes_without_approval():
    tool = FakeReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_context = context(tool.name)

    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"query": "Python"}),
        tool_context,
    )

    assert record.status == ToolExecutionStatus.COMPLETED
    assert record.result.output == {"text": "Python"}
    assert tool.calls == [(ReadArguments(query="Python"), tool_context)]


@pytest.mark.parametrize(
    "risk_level",
    [
        ToolRiskLevel.LOCAL_WRITE,
        ToolRiskLevel.EXTERNAL_WRITE,
        ToolRiskLevel.SENSITIVE,
    ],
)
def test_non_read_tools_reject_unpersisted_request_approval(risk_level):
    tool = FakeWriteTool(f"write_{risk_level.value}", risk_level)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    tool_context = context(tool.name)

    pending = executor.execute(
        ToolCallRequest(tool_name=tool.name, arguments={"content": "draft"}),
        tool_context,
    )
    approved = executor.execute(
        ToolCallRequest(
            tool_name=tool.name,
            arguments={"content": "draft"},
            approval_granted=True,
        ),
        tool_context,
    )

    assert pending.status == ToolExecutionStatus.APPROVAL_REQUIRED
    assert approved.status == ToolExecutionStatus.APPROVAL_REQUIRED
    assert tool.calls == []


@pytest.mark.parametrize("approved", [False, True])
def test_invalid_arguments_never_execute_tool(approved):
    tool = FakeWriteTool("write_file", ToolRiskLevel.LOCAL_WRITE)
    registry = ToolRegistry()
    registry.register(tool)

    record = ToolExecutor(registry).execute(
        ToolCallRequest(
            tool_name=tool.name,
            arguments={"unexpected": "value"},
            approval_granted=approved,
        ),
        context(tool.name),
    )

    assert record.status == ToolExecutionStatus.FAILED
    assert record.error_message == "The tool arguments are invalid."
    assert tool.calls == []


def test_tool_exception_is_converted_to_safe_failure_record():
    tool = ExplodingReadTool()
    registry = ToolRegistry()
    registry.register(tool)

    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"query": "anything"}),
        context(tool.name),
    )

    assert record.status == ToolExecutionStatus.FAILED
    assert record.error_code == "tool_execution_failed"
    assert record.error_message == "The tool could not complete the request."
    assert tool.secret_message not in record.model_dump_json()


def test_invalid_tool_output_is_a_safe_failure():
    tool = InvalidOutputTool()
    registry = ToolRegistry(); registry.register(tool)
    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"query": "x"}),
        context(tool.name),
    )
    assert record.status == ToolExecutionStatus.FAILED
    assert record.error_code == "invalid_tool_output"
    assert record.error_message == "The tool returned an invalid result."


def test_registry_generates_model_schemas_for_allowed_tools():
    registry = ToolRegistry()
    read_tool = FakeReadTool()
    write_tool = FakeWriteTool("write_file", ToolRiskLevel.LOCAL_WRITE)
    registry.register(read_tool)
    registry.register(write_tool)

    schemas = registry.model_schemas(context(read_tool.name))

    assert [item["name"] for item in schemas] == [read_tool.name]
    assert schemas[0]["description"] == read_tool.description
    assert schemas[0]["input_schema"]["type"] == "object"
    assert schemas[0]["input_schema"]["properties"]["query"]["type"] == "string"
    assert [item["name"] for item in registry.model_schemas()] == [
        read_tool.name,
        write_tool.name,
    ]


def test_executor_preserves_tool_result_provenance():
    tool = FakeReadTool()
    registry = ToolRegistry()
    registry.register(tool)

    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"query": "FastAPI"}),
        context(tool.name),
    )

    assert record.result.provenance == [
        ToolProvenance(source_type="resume_source", source_id="resume-1")
    ]


def test_only_resume_and_user_confirmed_provenance_support_resume_claims():
    assert RESUME_EVIDENCE_SOURCE_TYPES == frozenset(
        {"resume_source", "user_confirmed"}
    )
    assert provenance_may_support_resume_claim(
        ToolProvenance(source_type="resume_source")
    )
    assert provenance_may_support_resume_claim(
        ToolProvenance(source_type="user_confirmed")
    )
    for source_type in ("job_description", "web", "tool_generated", "model_inferred"):
        assert not provenance_may_support_resume_claim(
            ToolProvenance(source_type=source_type)
        )


@pytest.fixture
def durable_runtime(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'tools.sqlite').as_posix()}", create_schema_for_tests=True)
    repository = ToolCallRepository(database.session_factory)
    registry = ToolRegistry()
    yield database, repository, registry
    database.close()


def persistent_request(tool_name, arguments=None, **updates):
    return ToolCallRequest(tool_name=tool_name, arguments=arguments or {"query": "Python"},
        idempotency_key="key-1", **updates)


def test_tool_runtime_migration_creates_both_tables(tmp_path):
    path = tmp_path / "migrated.sqlite"
    upgrade_database(f"sqlite:///{path.as_posix()}")
    database = create_database(f"sqlite:///{path.as_posix()}")
    try:
        tables = set(inspect(database.engine).get_table_names())
        assert {"tool_calls", "tool_call_events"} <= tables
    finally:
        database.close()


def test_persisted_execution_has_ordered_events_and_idempotent_reuse(durable_runtime):
    _, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    executor = ToolExecutor(registry, repository=repository)
    request = persistent_request(tool.name)
    first = executor.execute(request, context(tool.name))
    reused = executor.execute(request, context(tool.name))
    assert first.status == ToolExecutionStatus.COMPLETED
    assert reused.call_id == first.call_id
    assert len(tool.calls) == 1
    events = repository.list_events(first.call_id)
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert [e.to_status for e in events] == [
        ToolExecutionStatus.REQUESTED,
        ToolExecutionStatus.RUNNING,
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.COMPLETED,
    ]
    assert events[-1].event_type == "idempotently_reused"


def test_idempotency_conflict_uses_original_argument_hash(durable_runtime):
    _, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    executor = ToolExecutor(registry, repository=repository)
    executor.execute(persistent_request(tool.name), context(tool.name))
    with pytest.raises(IdempotencyConflictError):
        executor.execute(persistent_request(tool.name, {"query": "SQL"}), context(tool.name))


def test_persisted_approval_is_bound_to_version_and_hash(durable_runtime):
    _, repository, registry = durable_runtime
    tool = FakeWriteTool("write_file", ToolRiskLevel.LOCAL_WRITE); registry.register(tool)
    executor = ToolExecutor(registry, repository=repository)
    request = ToolCallRequest(tool_name=tool.name, arguments={"content": "draft"}, idempotency_key="write-1")
    pending = executor.execute(request, context(tool.name))
    assert pending.status == ToolExecutionStatus.APPROVAL_REQUIRED
    with pytest.raises(ApprovalBindingError):
        repository.approve(pending.call_id, expected_version=pending.version, tool_name=tool.name,
            tool_version="2.0", arguments_hash=pending.arguments_hash)
    approved = executor.approve(pending.call_id, expected_version=pending.version)
    assert approved.status == ToolExecutionStatus.APPROVED
    completed = executor.execute(request, context(tool.name))
    assert completed.status == ToolExecutionStatus.COMPLETED
    assert completed.approval_tool_version == "1.0"


def test_arguments_are_hashed_before_redaction_and_stored_redacted(durable_runtime):
    database, repository, registry = durable_runtime
    tool = FakeWriteTool("secret_write", ToolRiskLevel.SENSITIVE); registry.register(tool)
    raw = {"content": "draft", "api_key": "top-secret"}
    record = ToolExecutor(registry, repository=repository).execute(
        ToolCallRequest(tool_name=tool.name, arguments=raw, idempotency_key="secret-1"), context(tool.name))
    assert record.arguments_hash == arguments_hash(raw)
    assert record.request.arguments["api_key"] == REDACTED
    with database.session_factory() as session:
        stored = session.execute(text("select arguments_json from tool_calls where call_id=:id"), {"id": record.call_id}).scalar_one()
    assert "top-secret" not in stored and REDACTED in stored


def test_optimistic_concurrency_rejects_stale_transition(durable_runtime):
    _, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    record, _ = repository.get_or_create(request=persistent_request(tool.name), tool_version=tool.version,
        risk_level=tool.risk_level, scope_type="task", scope_id="task-1",
        arguments_hash=arguments_hash({"query": "Python"}), redacted_arguments={"query": "Python"})
    repository.transition(record.call_id, expected_version=0, status=ToolExecutionStatus.RUNNING,
        event_type="execution_started")
    with pytest.raises(StaleToolCallError):
        repository.transition(record.call_id, expected_version=0, status=ToolExecutionStatus.FAILED, event_type="failed")


def test_event_insert_failure_rolls_back_call_update(durable_runtime):
    database, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    record, _ = repository.get_or_create(request=persistent_request(tool.name), tool_version=tool.version,
        risk_level=tool.risk_level, scope_type="task", scope_id="task-1",
        arguments_hash=arguments_hash({"query": "Python"}), redacted_arguments={"query": "Python"})
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_second_event BEFORE INSERT ON tool_call_events WHEN NEW.sequence = 2 BEGIN SELECT RAISE(ABORT, 'event rejected'); END")
    with pytest.raises(IntegrityError):
        repository.transition(record.call_id, expected_version=0, status=ToolExecutionStatus.RUNNING, event_type="execution_started")
    unchanged = repository.require(record.call_id)
    assert unchanged.status == ToolExecutionStatus.REQUESTED and unchanged.version == 0 and unchanged.event_sequence == 1


class TimeoutReadTool(FakeReadTool):
    name = "timeout_read"
    def execute(self, arguments, context, *, timeout_seconds=None):
        assert timeout_seconds == 0.1
        raise ToolTimeoutError("provider included token=secret")

class TimeoutWriteTool(FakeWriteTool):
    def execute(self, arguments, context, *, timeout_seconds=None):
        raise ToolTimeoutError("unknown outcome secret")

class RetryTool(FakeReadTool):
    name = "retry_read"
    def __init__(self): self.attempts = 0
    def execute(self, arguments, context):
        self.attempts += 1
        if self.attempts == 1: raise RetryableToolError("password=secret")
        return ToolResult(output={"ok": True})


def test_read_timeout_is_timed_out_and_safe(durable_runtime):
    _, repository, registry = durable_runtime
    tool = TimeoutReadTool(); registry.register(tool)
    record = ToolExecutor(registry, repository=repository).execute(
        persistent_request(tool.name, timeout_seconds=.1), context(tool.name))
    assert record.status == ToolExecutionStatus.TIMED_OUT
    assert "secret" not in record.model_dump_json()


def test_write_timeout_is_outcome_unknown_and_never_retried(durable_runtime):
    _, repository, registry = durable_runtime
    tool = TimeoutWriteTool("timeout_write", ToolRiskLevel.EXTERNAL_WRITE); registry.register(tool)
    executor = ToolExecutor(registry, repository=repository)
    request = ToolCallRequest(tool_name=tool.name, arguments={"content": "x"}, idempotency_key="timeout", timeout_seconds=.1, retry_failed=True, max_attempts=3)
    pending = executor.execute(request, context(tool.name)); executor.approve(pending.call_id, expected_version=pending.version)
    unknown = executor.execute(request, context(tool.name))
    again = executor.execute(request, context(tool.name))
    assert unknown.status == ToolExecutionStatus.OUTCOME_UNKNOWN
    assert again.call_id == unknown.call_id and again.attempt_count == 1


def test_failed_call_retries_only_explicitly_and_within_limit(durable_runtime):
    _, repository, registry = durable_runtime
    tool = RetryTool(); registry.register(tool); executor = ToolExecutor(registry, repository=repository)
    base = ToolCallRequest(tool_name=tool.name, arguments={"query": "x"}, idempotency_key="retry", max_attempts=2)
    failed = executor.execute(base, context(tool.name))
    assert failed.status == ToolExecutionStatus.FAILED and failed.retryable
    assert executor.execute(base, context(tool.name)).status == ToolExecutionStatus.FAILED
    retry = base.model_copy(update={"retry_failed": True})
    assert executor.execute(retry, context(tool.name)).status == ToolExecutionStatus.COMPLETED
    assert tool.attempts == 2


def test_sqlite_foreign_keys_reject_orphan_event(durable_runtime):
    database, _, _ = durable_runtime
    with pytest.raises(IntegrityError):
        with database.session_factory.begin() as session:
            session.add(ToolCallEventRow(event_id="orphan", call_id="missing", sequence=1,
                event_type="requested", from_status=None, to_status="requested",
                payload_json="{}", occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC)))


@pytest.mark.parametrize(
    ("kwargs", "expected_type", "expected_id"),
    [
        ({"task_id": "t"}, "task", "t"),
        ({"session_id": "s"}, "session", "s"),
        ({"run_id": "r"}, "run", "r"),
        ({"user_id": "u"}, "user", "u"),
    ],
)
def test_idempotency_supports_each_context_scope(durable_runtime, kwargs, expected_type, expected_id):
    _, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    tool_context = ToolContext(**kwargs, allowed_tools=frozenset({tool.name}))
    record = ToolExecutor(registry, repository=repository).execute(
        persistent_request(tool.name), tool_context)
    assert (record.scope_type, record.scope_id) == (expected_type, expected_id)


def test_event_payload_redaction_never_persists_secrets(durable_runtime):
    _, repository, registry = durable_runtime
    tool = FakeReadTool(); registry.register(tool)
    record, _ = repository.get_or_create(request=persistent_request(tool.name), tool_version=tool.version,
        risk_level=tool.risk_level, scope_type="task", scope_id="task-1",
        arguments_hash=arguments_hash({"query": "Python"}), redacted_arguments={"query": "Python"})
    repository.transition(record.call_id, expected_version=record.version,
        status=ToolExecutionStatus.DENIED, event_type="denied",
        payload={"authorization": "Bearer private", "nested": {"password": "private"}})
    payload = repository.list_events(record.call_id)[-1].payload
    assert payload == {"authorization": REDACTED, "nested": {"password": REDACTED}}


class AlwaysRetryableTool(FakeReadTool):
    name = "always_retryable"
    def execute(self, arguments, context):
        raise RetryableToolError("transient secret")


def test_retry_attempt_limit_cannot_be_increased_by_a_later_request(durable_runtime):
    _, repository, registry = durable_runtime
    tool = AlwaysRetryableTool(); registry.register(tool)
    executor = ToolExecutor(registry, repository=repository)
    first = executor.execute(ToolCallRequest(tool_name=tool.name, arguments={"query": "x"},
        idempotency_key="limited", max_attempts=1), context(tool.name))
    second = executor.execute(ToolCallRequest(tool_name=tool.name, arguments={"query": "x"},
        idempotency_key="limited", max_attempts=99, retry_failed=True), context(tool.name))
    assert first.status == ToolExecutionStatus.FAILED
    assert second.attempt_count == 1

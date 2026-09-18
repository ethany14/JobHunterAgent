from __future__ import annotations

import asyncio
import sys
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel

from agent_runtime.errors import UnknownToolError
from agent_runtime.mcp import (
    McpRuntimeConfig,
    McpStdioServerConfig,
    McpToolManager,
    load_mcp_runtime_config,
    normalize_mcp_result,
)
from agent_runtime.mcp.client import StdioMcpClient
from agent_runtime.mcp.errors import (
    McpConfigurationError,
    McpCallTimeoutError,
    McpDisconnectedError,
    McpStartupError,
    McpToolCollisionError,
)
from agent_runtime.policy import ToolPolicy
from agent_runtime.registry import ToolRegistry
from agent_runtime.repository import ToolCallRepository
from agent_runtime.types import (
    ToolCallRequest,
    ToolContext,
    ToolExecutionStatus,
    ToolResult,
    ToolRiskLevel,
)
from api.db import create_database


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tests" / "fixtures" / "mcp_test_server.py"


def remote_tool(
    name: str = "echo_text",
    *,
    annotations: ToolAnnotations | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"Remote {name}",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        annotations=annotations,
    )


def result(text: str = "hello", *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"text": text},
        isError=is_error,
    )


class FakeClient:
    def __init__(
        self,
        tools: list[Tool] | None = None,
        *,
        response: Any | None = None,
        connect_error: Exception | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self.tools = tools or [remote_tool()]
        self.response = response or result()
        self.connect_error = connect_error
        self.call_error = call_error
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, dict, float | None]] = []

    def connect(self) -> None:
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    def list_tools(self) -> list[Any]:
        return self.tools

    def call_tool(self, name, arguments, *, timeout_seconds=None):
        self.calls.append((name, arguments, timeout_seconds))
        if self.call_error:
            raise self.call_error
        return self.response

    def close(self) -> None:
        self.closed = True
        self.connected = False


def config(**updates) -> McpStdioServerConfig:
    values = {
        "server_id": "local-test",
        "command": sys.executable,
        "read_only_tools": {"echo_text"},
        **updates,
    }
    return McpStdioServerConfig(**values)


def context(*names: str) -> ToolContext:
    return ToolContext(
        session_id="mcp-session",
        attempt_id="mcp-attempt",
        allowed_tools=frozenset(names),
    )


def persistent_executor(tmp_path, registry):
    database = create_database(
        f"sqlite:///{(tmp_path / 'mcp.sqlite').as_posix()}",
        create_schema_for_tests=True,
    )
    repository = ToolCallRepository(database.session_factory)
    from agent_runtime.executor import ToolExecutor

    return ToolExecutor(registry, policy=ToolPolicy(), repository=repository), repository


def test_config_validates_ids_cwd_filters_and_duplicate_servers(tmp_path):
    with pytest.raises(ValueError, match="lowercase"):
        config(server_id="Not Valid")
    with pytest.raises(ValueError, match="existing directory"):
        config(cwd=tmp_path / "missing")
    with pytest.raises(ValueError, match="unique"):
        McpRuntimeConfig(servers=[config(), config()])


def test_trusted_config_is_optional_and_resolves_named_environment(tmp_path):
    assert load_mcp_runtime_config(environ={}).servers == []
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [{
        "server_id": "configured",
        "command": sys.executable,
        "cwd": str(tmp_path),
        "env_var_names": {"CHILD_TOKEN": "BACKEND_TOKEN"},
        "tool_allowlist": ["echo_text"],
        "read_only_tools": ["echo_text"],
    }]}), encoding="utf-8")
    loaded = load_mcp_runtime_config(path, environ={"BACKEND_TOKEN": "resolved-secret"})
    assert loaded.servers[0].env == {"CHILD_TOKEN": "resolved-secret"}
    assert "resolved-secret" not in loaded.model_dump_json()


def test_trusted_config_accepts_windows_powershell_utf8_bom(tmp_path):
    path = tmp_path / "powershell-config.json"
    path.write_text(
        json.dumps({"servers": []}),
        encoding="utf-8-sig",
    )
    assert load_mcp_runtime_config(path, environ={}).servers == []


def test_trusted_config_rejects_malformed_missing_env_and_relative_path(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("not-json", encoding="utf-8")
    with pytest.raises(McpConfigurationError, match="invalid"):
        load_mcp_runtime_config(malformed, environ={})
    missing = tmp_path / "missing-env.json"
    missing.write_text(json.dumps({"servers": [{
        "server_id": "configured",
        "command": sys.executable,
        "env_var_names": {"TOKEN": "NOT_SET"},
    }]}), encoding="utf-8")
    with pytest.raises(McpConfigurationError, match="missing"):
        load_mcp_runtime_config(missing, environ={})
    with pytest.raises(McpConfigurationError, match="absolute"):
        load_mcp_runtime_config("relative.json", environ={})


def test_required_failure_stops_startup_but_optional_failure_is_degraded():
    optional = FakeClient(connect_error=McpStartupError("raw secret"))
    ready = FakeClient()
    clients = iter([optional, ready])
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[
            config(server_id="optional", read_only_tools=set()),
            config(server_id="ready"),
        ]),
        registry=ToolRegistry(),
        client_factory=lambda _: next(clients),
    )
    tools = manager.start()
    assert [tool.name for tool in tools] == ["mcp__ready__echo_text"]
    assert manager.health().model_dump() == {
        "configured_servers": 2,
        "ready_servers": 1,
        "failed_optional_servers": 1,
        "registered_tools": 1,
    }
    assert optional.closed is True
    manager.stop()

    required = FakeClient(connect_error=McpStartupError("raw secret"))
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config(required=True)]),
        registry=ToolRegistry(),
        client_factory=lambda _: required,
    )
    with pytest.raises(McpStartupError, match="Required MCP server"):
        manager.start()
    assert required.closed is True


def test_disabled_server_is_not_started_and_filters_are_applied():
    disabled = FakeClient()
    enabled = FakeClient([remote_tool("echo_text"), remote_tool("hidden")])
    clients = iter([enabled])
    manager = McpToolManager(
        config=McpRuntimeConfig(
            servers=[
                config(enabled=False),
                config(
                    server_id="second",
                    tool_allowlist={"echo_text", "hidden"},
                    tool_denylist={"hidden"},
                    read_only_tools={"echo_text"},
                ),
            ]
        ),
        registry=ToolRegistry(),
        client_factory=lambda _: next(clients),
    )

    tools = manager.start()

    assert disabled.connected is False
    assert [tool.name for tool in tools] == ["mcp__second__echo_text"]
    manager.stop()
    assert disabled.closed is False
    assert enabled.closed is True


def test_discovery_preserves_schema_metadata_and_calls_original_name():
    client = FakeClient()
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config(required=True)]),
        registry=registry,
        client_factory=lambda _: client,
    )
    tool = manager.start()[0]
    schema = registry.model_schemas(context(tool.name))[0]

    assert tool.metadata.remote_tool_name == "echo_text"
    assert schema["name"] == "mcp__local-test__echo_text"
    assert schema["input_schema"] == client.tools[0].input_schema

    from agent_runtime.executor import ToolExecutor

    record = ToolExecutor(registry).execute(
        ToolCallRequest(tool_name=tool.name, arguments={"text": "hello"}),
        context(tool.name),
    )
    assert record.status == ToolExecutionStatus.COMPLETED
    assert client.calls == [("echo_text", {"text": "hello"}, 30.0)]
    assert record.result.provenance[0].source_type == "mcp_server"
    assert record.result.provenance[0].source_id == "local-test"
    manager.stop()


def test_annotations_are_hints_and_do_not_bypass_persisted_approval(tmp_path):
    client = FakeClient(
        [remote_tool(annotations=ToolAnnotations(readOnlyHint=True))]
    )
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(
            servers=[
                McpStdioServerConfig(
                    server_id="local-test", command=sys.executable
                )
            ]
        ),
        registry=registry,
        client_factory=lambda _: client,
    )
    tool = manager.start()[0]
    executor, repository = persistent_executor(tmp_path, registry)
    request = ToolCallRequest(
        tool_name=tool.name,
        arguments={"text": "write?"},
        idempotency_key="approval-1",
    )

    pending = executor.execute(request, context(tool.name))
    assert tool.risk_level == ToolRiskLevel.EXTERNAL_WRITE
    assert pending.status == ToolExecutionStatus.APPROVAL_REQUIRED
    assert client.calls == []

    executor.approve(pending.call_id, expected_version=pending.version)
    completed = executor.execute(request, context(tool.name))
    assert completed.status == ToolExecutionStatus.COMPLETED
    assert repository.require(pending.call_id).status == ToolExecutionStatus.COMPLETED
    manager.stop()


def test_remote_is_error_becomes_persisted_failed_record(tmp_path):
    client = FakeClient(response=result("safe failure", is_error=True))
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config()]),
        registry=registry,
        client_factory=lambda _: client,
    )
    tool = manager.start()[0]
    executor, repository = persistent_executor(tmp_path, registry)
    failed = executor.execute(
        ToolCallRequest(
            tool_name=tool.name,
            arguments={"text": "hello"},
            idempotency_key="remote-error",
        ),
        context(tool.name),
    )

    assert failed.status == ToolExecutionStatus.FAILED
    assert failed.error_code == "mcp_tool_reported_error"
    assert failed.result.is_error is True
    assert [event.event_type for event in repository.list_events(failed.call_id)] == [
        "requested",
        "execution_started",
        "tool_reported_error",
    ]
    manager.stop()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (McpCallTimeoutError("secret timeout details"), "timed_out"),
        (McpDisconnectedError("secret disconnect details"), "mcp_disconnected"),
    ],
)
def test_runtime_failures_are_safe_and_classified(tmp_path, error, expected_code):
    client = FakeClient(call_error=error)
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config()]),
        registry=registry,
        client_factory=lambda _: client,
    )
    tool = manager.start()[0]
    executor, _ = persistent_executor(tmp_path, registry)
    record = executor.execute(
        ToolCallRequest(
            tool_name=tool.name,
            arguments={"text": "hello"},
            idempotency_key=expected_code,
        ),
        context(tool.name),
    )
    assert record.error_code == expected_code
    assert "secret" not in (record.error_message or "")
    manager.stop()


def test_configured_secrets_are_redacted_from_persisted_results_and_events(tmp_path):
    secret = "mcp-super-secret-value"
    client = FakeClient(response=result(f"server said {secret}"))
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(
            servers=[config(env={"API_TOKEN": secret})]
        ),
        registry=registry,
        client_factory=lambda _: client,
    )
    tool = manager.start()[0]
    executor, repository = persistent_executor(tmp_path, registry)
    record = executor.execute(
        ToolCallRequest(
            tool_name=tool.name,
            arguments={"text": "hello"},
            idempotency_key="secret-result",
        ),
        context(tool.name),
    )
    persisted = repository.require(record.call_id).model_dump_json()
    events = "".join(
        event.model_dump_json() for event in repository.list_events(record.call_id)
    )
    assert secret not in persisted
    assert secret not in events
    assert "[REDACTED]" in persisted
    manager.stop()


def test_oversized_result_is_deterministically_truncated():
    first = normalize_mcp_result(
        result("x" * 10_000),
        server_id="local-test",
        remote_tool_name="echo_text",
        max_bytes=512,
    )
    second = normalize_mcp_result(
        result("x" * 10_000),
        server_id="local-test",
        remote_tool_name="echo_text",
        max_bytes=512,
    )
    assert first == second
    assert first.output["truncated"] is True
    assert "[truncated by MCP adapter]" in first.output["content"][0]["text"]


def test_collisions_and_partial_startup_close_all_started_clients():
    class ExistingInput(BaseModel):
        text: str

    class ExistingTool:
        name = "mcp__local-test__echo_text"
        version = "1"
        description = "existing"
        risk_level = ToolRiskLevel.READ_ONLY
        input_schema = ExistingInput

        def execute(self, arguments, context):
            return ToolResult(output={})

    registry = ToolRegistry()
    registry.register(ExistingTool())
    first = FakeClient()
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config(required=True)]),
        registry=registry,
        client_factory=lambda _: first,
    )
    with pytest.raises(McpToolCollisionError):
        manager.start()
    assert first.closed is True

    good = FakeClient()
    bad = FakeClient(connect_error=McpStartupError("unavailable"))
    clients = iter([good, bad])
    manager = McpToolManager(
        config=McpRuntimeConfig(
            servers=[config(), config(server_id="second", required=True)]
        ),
        registry=ToolRegistry(),
        client_factory=lambda _: next(clients),
    )
    with pytest.raises(McpStartupError):
        manager.start()
    assert good.closed is True
    assert bad.closed is True


def test_local_stdio_server_end_to_end_persists_both_tools(tmp_path):
    server_config = McpStdioServerConfig(
        server_id="stdio-test",
        command=sys.executable,
        args=[str(SERVER)],
        cwd=ROOT,
        tool_allowlist={"echo_text", "add_numbers"},
        read_only_tools={"echo_text", "add_numbers"},
        startup_timeout_seconds=10,
        call_timeout_seconds=5,
    )
    registry = ToolRegistry()
    clients: list[StdioMcpClient] = []

    def make_client(server):
        client = StdioMcpClient(server)
        clients.append(client)
        return client

    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[server_config]),
        registry=registry,
        client_factory=make_client,
    )
    manager.start()
    executor, repository = persistent_executor(tmp_path, registry)
    ctx = context("mcp__stdio-test__echo_text", "mcp__stdio-test__add_numbers")
    echo = executor.execute(
        ToolCallRequest(
            tool_name="mcp__stdio-test__echo_text",
            arguments={"text": "stdio works"},
            idempotency_key="stdio-echo",
        ),
        ctx,
    )
    added = executor.execute(
        ToolCallRequest(
            tool_name="mcp__stdio-test__add_numbers",
            arguments={"left": 2, "right": 5},
            idempotency_key="stdio-add",
        ),
        ctx,
    )
    assert echo.status == ToolExecutionStatus.COMPLETED
    assert added.status == ToolExecutionStatus.COMPLETED
    assert echo.result.output["structured_content"] == {"text": "stdio works"}
    assert added.result.output["structured_content"] == {"sum": 7}
    assert repository.require(echo.call_id).result == echo.result
    assert repository.require(added.call_id).result == added.result
    reused = executor.execute(
        ToolCallRequest(
            tool_name="mcp__stdio-test__echo_text",
            arguments={"text": "stdio works"},
            idempotency_key="stdio-echo",
        ),
        ctx,
    )
    assert reused.call_id == echo.call_id
    manager.stop()
    assert clients[0].connected is False
    with pytest.raises(UnknownToolError):
        registry.get("mcp__stdio-test__echo_text")


def test_timed_out_stdio_connection_is_not_reused():
    from agent_runtime.mcp.client import StdioMcpClient

    client = StdioMcpClient(McpStdioServerConfig(
        server_id="timeout-test",
        command=sys.executable,
        args=[str(SERVER)],
        cwd=ROOT,
        startup_timeout_seconds=10,
        call_timeout_seconds=2,
    ))
    client.connect()
    try:
        with pytest.raises(McpCallTimeoutError):
            client.call_tool(
                "slow_echo",
                {"text": "late", "delay_seconds": 0.5},
                timeout_seconds=0.02,
            )
        with pytest.raises(McpDisconnectedError):
            client.call_tool("echo_text", {"text": "must not reuse"})
    finally:
        client.close()


def test_stdio_call_uses_owned_thread_boundary_from_running_event_loop():
    from agent_runtime.mcp.client import StdioMcpClient

    client = StdioMcpClient(McpStdioServerConfig(
        server_id="async-boundary",
        command=sys.executable,
        args=[str(SERVER)],
        cwd=ROOT,
        startup_timeout_seconds=10,
        call_timeout_seconds=2,
    ))
    client.connect()

    async def scenario():
        call = asyncio.create_task(asyncio.to_thread(
            client.call_tool,
            "slow_echo",
            {"text": "concurrent", "delay_seconds": 0.05},
            timeout_seconds=1,
        ))
        await asyncio.sleep(0.01)
        assert call.done() is False
        return await call

    try:
        response = asyncio.run(scenario())
        assert response.structured_content == {"text": "concurrent"}
    finally:
        client.close()


def test_one_stdio_connection_supports_two_concurrent_session_calls():
    from agent_runtime.mcp.client import StdioMcpClient

    client = StdioMcpClient(McpStdioServerConfig(
        server_id="concurrent-stdio",
        command=sys.executable,
        args=[str(SERVER)],
        cwd=ROOT,
        startup_timeout_seconds=10,
        call_timeout_seconds=2,
    ))
    client.connect()
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    client.call_tool,
                    "slow_echo",
                    {"text": f"session-{index}", "delay_seconds": 0.15},
                    timeout_seconds=1,
                )
                for index in range(2)
            ]
            results = [future.result(timeout=2) for future in futures]
        elapsed = time.monotonic() - started
        assert elapsed < 0.28
        assert {item.structured_content["text"] for item in results} == {
            "session-0",
            "session-1",
        }
    finally:
        client.close()


def test_manager_allows_concurrent_session_scopes_and_bounds_shutdown():
    class ConcurrentClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.entered = 0
            self.both_active = threading.Event()
            self.release = threading.Event()

        def call_tool(self, name, arguments, *, timeout_seconds=None):
            with self.lock:
                self.entered += 1
                if self.entered == 2:
                    self.both_active.set()
            self.both_active.wait(timeout=1)
            self.release.wait(timeout=1)
            return result(arguments["text"])

        def close(self):
            self.release.set()
            super().close()

    client = ConcurrentClient()
    registry = ToolRegistry()
    manager = McpToolManager(
        config=McpRuntimeConfig(servers=[config()]),
        registry=registry,
        client_factory=lambda _: client,
        shutdown_timeout_seconds=0.05,
    )
    manager.start()
    outputs = []

    def invoke(session_id):
        outputs.append(manager.call_tool(
            "local-test", "echo_text", {"text": session_id}, timeout_seconds=1
        ))

    threads = [threading.Thread(target=invoke, args=(f"session-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    assert client.both_active.wait(timeout=1)
    started = time.monotonic()
    manager.stop()
    elapsed = time.monotonic() - started
    for thread in threads:
        thread.join(timeout=1)
    assert elapsed < 0.5
    assert len(outputs) == 2
    assert client.closed is True

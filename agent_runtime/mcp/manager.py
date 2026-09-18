"""Application-owned MCP lifecycle and ToolRegistry integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Condition, RLock
from time import monotonic
from typing import Any

from pydantic import Field

from agent_runtime.mcp.adapter import McpAgentTool
from agent_runtime.mcp.client import McpClient, StdioMcpClient
from agent_runtime.mcp.config import McpRuntimeConfig, McpStdioServerConfig
from agent_runtime.mcp.errors import (
    McpCallTimeoutError,
    McpDisconnectedError,
    McpProtocolError,
    McpStartupError,
    McpToolCollisionError,
)
from agent_runtime.mcp.types import McpServerRuntimeStatus, McpServerStatus
from agent_runtime.registry import ToolRegistry
from agent_runtime.types import RuntimeModel


class McpServerDiagnostic(RuntimeModel):
    server_id: str
    status: McpServerStatus
    error_code: str | None = None
    failure_stage: str | None = None
    updated_at: datetime


class McpHealth(RuntimeModel):
    configured_servers: int
    ready_servers: int
    failed_optional_servers: int
    registered_tools: int
    servers: list[McpServerRuntimeStatus] = Field(default_factory=list)


class McpToolManager:
    def __init__(
        self,
        *,
        config: McpRuntimeConfig,
        registry: ToolRegistry,
        client_factory: Callable[[McpStdioServerConfig], McpClient] = StdioMcpClient,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("MCP shutdown timeout must be positive.")
        self._config = config
        self._registry = registry
        self._client_factory = client_factory
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, McpAgentTool] = {}
        self._diagnostics: dict[str, McpServerDiagnostic] = {}
        self._started = False
        self._accepting = False
        self._active_calls = 0
        self._server_metrics: dict[str, dict[str, Any]] = {
            server.server_id: {
                "active_call_count": 0,
                "completed_call_count": 0,
                "failed_call_count": 0,
                "timeout_call_count": 0,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error_code": None,
            }
            for server in config.servers
        }
        self._lock = RLock()
        self._condition = Condition(self._lock)

    @property
    def tools(self) -> tuple[McpAgentTool, ...]:
        with self._lock:
            return tuple(self._tools.values())

    @property
    def public_tool_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._tools)

    @property
    def diagnostics(self) -> tuple[McpServerDiagnostic, ...]:
        with self._lock:
            return tuple(
                self._diagnostics[server.server_id]
                for server in self._config.servers
                if server.server_id in self._diagnostics
            )

    def health(self) -> McpHealth:
        diagnostics = self.diagnostics
        with self._lock:
            statuses = [self._runtime_status(server) for server in self._config.servers]
        return McpHealth(
            configured_servers=len(self._config.servers),
            ready_servers=sum(item.status == McpServerStatus.READY for item in diagnostics),
            failed_optional_servers=sum(
                item.status == McpServerStatus.DEGRADED for item in diagnostics
            ),
            registered_tools=len(self.public_tool_names),
            servers=statuses,
        )

    def _runtime_status(self, server: McpStdioServerConfig) -> McpServerRuntimeStatus:
        diagnostic = self._diagnostics.get(server.server_id)
        metrics = self._server_metrics[server.server_id]
        return McpServerRuntimeStatus(
            server_id=server.server_id,
            status=(diagnostic.status.value if diagnostic else McpServerStatus.STOPPED.value),
            required=server.required,
            registered_tool_count=sum(
                tool.metadata.server_id == server.server_id for tool in self._tools.values()
            ),
            **metrics,
        )

    def start(self) -> tuple[McpAgentTool, ...]:
        with self._lock:
            if self._started:
                return tuple(self._tools.values())
            self._started = True
            self._accepting = False
        for server in self._config.servers:
            if not server.enabled:
                self._set_status(server.server_id, McpServerStatus.DISABLED)
                continue
            self._set_status(server.server_id, McpServerStatus.STARTING)
            try:
                self._start_server(server)
            except Exception as exc:
                self._cleanup_server(server.server_id)
                self._set_status(
                    server.server_id,
                    McpServerStatus.DEGRADED,
                    error_code=self._safe_error_code(exc),
                    failure_stage="startup",
                )
                if server.required:
                    self.stop()
                    if isinstance(exc, McpToolCollisionError):
                        raise
                    raise McpStartupError(
                        f"Required MCP server '{server.server_id}' failed to start."
                    ) from exc
        with self._lock:
            self._accepting = True
        return self.tools

    def _start_server(self, server: McpStdioServerConfig) -> None:
        client = self._client_factory(server)
        with self._lock:
            self._clients[server.server_id] = client
        client.connect()
        server_tools: dict[str, McpAgentTool] = {}
        for remote in client.list_tools():
            remote_name = getattr(remote, "name", None)
            if not isinstance(remote_name, str):
                raise McpProtocolError("The MCP server returned invalid tool metadata.")
            if server.tool_allowlist is not None and remote_name not in server.tool_allowlist:
                continue
            if remote_name in server.tool_denylist:
                continue
            tool = McpAgentTool(
                config=server,
                remote_tool=remote,
                caller=lambda name, arguments, timeout, sid=server.server_id: self.call_tool(
                    sid, name, arguments, timeout_seconds=timeout
                ),
            )
            if (
                tool.name in server_tools
                or tool.name in self._registry.names()
                or tool.name in self.public_tool_names
            ):
                raise McpToolCollisionError(
                    f"MCP public tool name '{tool.name}' is already registered."
                )
            server_tools[tool.name] = tool
        for tool in server_tools.values():
            self._registry.register(tool)
        with self._lock:
            self._tools.update(server_tools)
        self._set_status(server.server_id, McpServerStatus.READY)

    def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        with self._condition:
            client = self._clients.get(server_id)
            if not self._accepting or client is None:
                raise McpDisconnectedError("The MCP server connection is unavailable.")
            self._active_calls += 1
            self._server_metrics[server_id]["active_call_count"] += 1
        try:
            result = client.call_tool(
                remote_tool_name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            with self._condition:
                metrics = self._server_metrics[server_id]
                if bool(getattr(result, "is_error", False)):
                    metrics["failed_call_count"] += 1
                    metrics["last_failure_at"] = datetime.now(UTC)
                    metrics["last_error_code"] = "mcp_tool_reported_error"
                else:
                    metrics["completed_call_count"] += 1
                    metrics["last_success_at"] = datetime.now(UTC)
            return result
        except Exception as exc:
            with self._condition:
                metrics = self._server_metrics[server_id]
                metrics["failed_call_count"] += 1
                metrics["last_failure_at"] = datetime.now(UTC)
                code = self._safe_error_code(exc)
                if isinstance(exc, McpCallTimeoutError):
                    metrics["timeout_call_count"] += 1
                    code = "mcp_call_timeout"
                metrics["last_error_code"] = code
            raise
        finally:
            with self._condition:
                self._active_calls -= 1
                self._server_metrics[server_id]["active_call_count"] -= 1
                self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._accepting = False
            for server in self._config.servers:
                current = self._diagnostics.get(server.server_id)
                if current is not None and current.status not in {
                    McpServerStatus.DISABLED,
                    McpServerStatus.STOPPED,
                }:
                    self._set_status(server.server_id, McpServerStatus.STOPPING)
            deadline = monotonic() + self._shutdown_timeout_seconds
            while self._active_calls and monotonic() < deadline:
                self._condition.wait(timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            tools = list(self._tools.items())
            clients = list(self._clients.items())
            self._tools = {}
            self._clients = {}
        for name, tool in tools:
            self._registry.unregister(name, expected=tool)
        for _, client in reversed(clients):
            try:
                client.close()
            except Exception:
                pass
        for server in self._config.servers:
            current = self._diagnostics.get(server.server_id)
            if current is not None and current.status != McpServerStatus.DISABLED:
                self._set_status(server.server_id, McpServerStatus.STOPPED)
        with self._lock:
            self._started = False

    def _cleanup_server(self, server_id: str) -> None:
        with self._lock:
            client = self._clients.pop(server_id, None)
            owned = [
                (name, tool)
                for name, tool in self._tools.items()
                if tool.metadata.server_id == server_id
            ]
            for name, _ in owned:
                self._tools.pop(name, None)
        for name, tool in owned:
            self._registry.unregister(name, expected=tool)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _set_status(
        self,
        server_id: str,
        status: McpServerStatus,
        *,
        error_code: str | None = None,
        failure_stage: str | None = None,
    ) -> None:
        diagnostic = McpServerDiagnostic(
            server_id=server_id,
            status=status,
            error_code=error_code,
            failure_stage=failure_stage,
            updated_at=datetime.now(UTC),
        )
        with self._lock:
            self._diagnostics[server_id] = diagnostic
            if status == McpServerStatus.DEGRADED:
                metrics = self._server_metrics[server_id]
                metrics["last_failure_at"] = diagnostic.updated_at
                metrics["last_error_code"] = error_code or "mcp_startup_failed"

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        if isinstance(exc, McpCallTimeoutError):
            return "mcp_call_timeout"
        if isinstance(exc, McpDisconnectedError):
            return "mcp_disconnected"
        if isinstance(exc, McpToolCollisionError):
            return "mcp_tool_collision"
        if isinstance(exc, McpProtocolError):
            return exc.error_code
        return "mcp_startup_failed"

    def __enter__(self) -> "McpToolManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()

"""Lifecycle and ToolRegistry integration for configured MCP servers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runtime.mcp.adapter import McpAgentTool
from agent_runtime.mcp.client import McpClient, StdioMcpClient
from agent_runtime.mcp.config import McpRuntimeConfig, McpStdioServerConfig
from agent_runtime.mcp.errors import McpToolCollisionError
from agent_runtime.registry import ToolRegistry


class McpToolManager:
    def __init__(
        self,
        *,
        config: McpRuntimeConfig,
        registry: ToolRegistry,
        client_factory: Callable[[McpStdioServerConfig], McpClient] = StdioMcpClient,
    ) -> None:
        self._config = config
        self._registry = registry
        self._client_factory = client_factory
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, McpAgentTool] = {}
        self._started = False

    @property
    def tools(self) -> tuple[McpAgentTool, ...]:
        return tuple(self._tools.values())

    def start(self) -> tuple[McpAgentTool, ...]:
        if self._started:
            return self.tools
        staged_clients: dict[str, McpClient] = {}
        staged_tools: dict[str, McpAgentTool] = {}
        try:
            for server in self._config.servers:
                if not server.enabled:
                    continue
                client = self._client_factory(server)
                staged_clients[server.server_id] = client
                client.connect()
                for remote in client.list_tools():
                    remote_name = getattr(remote, "name", None)
                    if not isinstance(remote_name, str):
                        from agent_runtime.mcp.errors import McpProtocolError
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
                    if tool.name in staged_tools or tool.name in self._registry.names():
                        raise McpToolCollisionError(
                            f"MCP public tool name '{tool.name}' is already registered."
                        )
                    staged_tools[tool.name] = tool
            self._clients = staged_clients
            for tool in staged_tools.values():
                self._registry.register(tool)
            self._tools = staged_tools
            self._started = True
            return self.tools
        except Exception:
            for client in reversed(list(staged_clients.values())):
                try:
                    client.close()
                except Exception:
                    pass
            self._clients = {}
            self._tools = {}
            raise

    def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        client = self._clients.get(server_id)
        if client is None:
            from agent_runtime.mcp.errors import McpDisconnectedError
            raise McpDisconnectedError("The MCP server connection is unavailable.")
        return client.call_tool(
            remote_tool_name,
            arguments,
            timeout_seconds=timeout_seconds,
        )

    def stop(self) -> None:
        for name, tool in self._tools.items():
            self._registry.unregister(name, expected=tool)
        for client in reversed(list(self._clients.values())):
            try:
                client.close()
            except Exception:
                pass
        self._clients = {}
        self._tools = {}
        self._started = False

    def __enter__(self) -> "McpToolManager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()

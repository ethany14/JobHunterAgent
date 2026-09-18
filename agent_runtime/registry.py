"""In-process registry for explicitly installed agent tools."""

from __future__ import annotations

from typing import Any

from agent_runtime.errors import DuplicateToolError, UnknownToolError
from agent_runtime.types import AgentTool, ToolContext


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise DuplicateToolError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"Tool '{name}' is not registered.") from exc

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def unregister(self, name: str, *, expected: AgentTool | None = None) -> None:
        """Remove a lifecycle-owned tool without disturbing a replacement."""
        current = self._tools.get(name)
        if current is None:
            return
        if expected is not None and current is not expected:
            return
        del self._tools[name]

    def allowed_for(self, context: ToolContext) -> list[AgentTool]:
        return [
            tool
            for name, tool in self._tools.items()
            if name in context.allowed_tools
        ]

    def model_schemas(self, context: ToolContext | None = None) -> list[dict[str, Any]]:
        tools = list(self._tools.values()) if context is None else self.allowed_for(context)
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema.model_json_schema(),
                **(
                    {"output_schema": tool.output_schema.model_json_schema()}
                    if getattr(tool, "output_schema", None) is not None
                    else {}
                ),
            }
            for tool in tools
        ]

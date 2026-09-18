"""Public, sanitized MCP provenance and runtime diagnostics."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from agent_runtime.types import RuntimeModel, ToolProvenance


class McpServerStatus(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


class McpToolProvenance(RuntimeModel):
    server_id: str
    remote_tool_name: str
    transport: Literal["stdio"] = "stdio"
    public_tool_name: str

    def as_tool_provenance(self) -> ToolProvenance:
        return ToolProvenance(
            source_type="mcp_server",
            source_id=self.server_id,
            metadata=self.model_dump(mode="json"),
        )

    @classmethod
    def from_tool_provenance(
        cls, provenance: ToolProvenance
    ) -> "McpToolProvenance | None":
        if provenance.source_type != "mcp_server":
            return None
        metadata = dict(provenance.metadata)
        # Compatibility with MCP v0.1/v0.2 persisted results.
        metadata.setdefault("server_id", provenance.source_id)
        metadata.setdefault("transport", "stdio")
        remote = metadata.get("remote_tool_name")
        metadata.setdefault(
            "public_tool_name",
            f"mcp__{metadata.get('server_id')}__{remote}" if remote else "",
        )
        try:
            return cls.model_validate(metadata)
        except Exception:
            return None


class McpServerRuntimeStatus(RuntimeModel):
    server_id: str
    status: McpServerStatus
    required: bool
    registered_tool_count: int
    active_call_count: int
    completed_call_count: int
    failed_call_count: int
    timeout_call_count: int
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str | None = None

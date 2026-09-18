"""Trusted application configuration for stdio MCP servers."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from agent_runtime.types import RuntimeModel

_SERVER_ID = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


class McpStdioServerConfig(RuntimeModel):
    server_id: str
    enabled: bool = True
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None
    env: dict[str, str] = Field(default_factory=dict)
    tool_allowlist: set[str] | None = None
    tool_denylist: set[str] = Field(default_factory=set)
    read_only_tools: set[str] = Field(default_factory=set)
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    max_result_bytes: int = Field(default=20_000, ge=256, le=10_000_000)

    @field_validator("server_id")
    @classmethod
    def valid_server_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SERVER_ID.fullmatch(cleaned):
            raise ValueError("server_id must be a stable lowercase identifier.")
        return cleaned

    @field_validator("command")
    @classmethod
    def nonempty_command(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("command must not be empty.")
        return cleaned

    @field_validator("args")
    @classmethod
    def valid_args(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or "\x00" in value for value in values):
            raise ValueError("MCP arguments must be safe strings.")
        return values

    @field_validator("env")
    @classmethod
    def valid_environment(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or "=" in key or "\x00" in key or "\x00" in value
               for key, value in values.items()):
            raise ValueError("MCP environment entries are invalid.")
        return values

    @field_validator("cwd")
    @classmethod
    def valid_cwd(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        resolved = value.expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError("MCP cwd must be an existing directory.")
        return resolved

    @model_validator(mode="after")
    def coherent_filters(self) -> "McpStdioServerConfig":
        if self.tool_allowlist is not None and not self.read_only_tools <= self.tool_allowlist:
            raise ValueError("read_only_tools must be included in tool_allowlist.")
        return self


class McpRuntimeConfig(RuntimeModel):
    servers: list[McpStdioServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_server_ids(self) -> "McpRuntimeConfig":
        ids = [server.server_id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server_id values must be unique.")
        return self

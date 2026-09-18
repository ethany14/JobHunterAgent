"""Trusted application configuration for stdio MCP servers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from agent_runtime.mcp.errors import McpConfigurationError
from agent_runtime.types import RuntimeModel

_SERVER_ID = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


class McpStdioServerConfig(RuntimeModel):
    server_id: str
    enabled: bool = True
    required: bool = False
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None
    env: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)
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


class _McpFileServerConfig(RuntimeModel):
    server_id: str
    enabled: bool = True
    required: bool = False
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None
    env_var_names: dict[str, str] = Field(default_factory=dict)
    tool_allowlist: set[str] | None = None
    tool_denylist: set[str] = Field(default_factory=set)
    read_only_tools: set[str] = Field(default_factory=set)
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("MCP cwd must be absolute.")
        return value

    @field_validator("env_var_names")
    @classmethod
    def valid_env_names(cls, values: dict[str, str]) -> dict[str, str]:
        for child_name, backend_name in values.items():
            if (
                not child_name
                or not backend_name
                or "=" in child_name
                or "\x00" in child_name
                or "\x00" in backend_name
            ):
                raise ValueError("MCP environment variable names are invalid.")
        return values


class _McpFileConfig(RuntimeModel):
    servers: list[_McpFileServerConfig] = Field(default_factory=list)


def load_mcp_runtime_config(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> McpRuntimeConfig:
    """Load trusted local MCP configuration and resolve named environment values."""
    environment = os.environ if environ is None else environ
    selected = config_path
    if selected is None:
        selected = environment.get("MCP_CONFIG_PATH")
    if selected is None or not str(selected).strip():
        return McpRuntimeConfig()
    path = Path(selected)
    if not path.is_absolute():
        raise McpConfigurationError("MCP_CONFIG_PATH must be an absolute path.")
    try:
        if path.stat().st_size > 1_000_000:
            raise McpConfigurationError("The MCP configuration file is too large.")
        # Windows PowerShell 5 writes a UTF-8 BOM for `Set-Content -Encoding
        # UTF8`. utf-8-sig accepts that file while remaining compatible with
        # ordinary BOM-free UTF-8 JSON.
        raw: Any = json.loads(path.read_text(encoding="utf-8-sig"))
        parsed = _McpFileConfig.model_validate(raw)
    except McpConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise McpConfigurationError("The MCP configuration is invalid.") from exc
    servers: list[McpStdioServerConfig] = []
    for item in parsed.servers:
        resolved: dict[str, str] = {}
        for child_name, backend_name in item.env_var_names.items():
            value = environment.get(backend_name)
            if value is None:
                raise McpConfigurationError(
                    f"A required environment variable for MCP server '{item.server_id}' is missing."
                )
            resolved[child_name] = value
        values = item.model_dump(exclude={"env_var_names"})
        try:
            servers.append(McpStdioServerConfig(**values, env=resolved))
        except ValidationError as exc:
            raise McpConfigurationError("The MCP configuration is invalid.") from exc
    try:
        return McpRuntimeConfig(servers=servers)
    except ValidationError as exc:
        raise McpConfigurationError("The MCP configuration is invalid.") from exc

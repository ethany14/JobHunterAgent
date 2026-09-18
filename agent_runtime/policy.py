"""Deterministic authorization policy for registered tools."""

from __future__ import annotations

from pydantic import ConfigDict

from agent_runtime.types import AgentTool, RuntimeModel, ToolContext, ToolRiskLevel


class ToolPolicyDecision(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    approval_required: bool = False
    reason: str


class ToolPolicy:
    _APPROVAL_RISKS = frozenset(
        {
            ToolRiskLevel.LOCAL_WRITE,
            ToolRiskLevel.EXTERNAL_WRITE,
            ToolRiskLevel.SENSITIVE,
        }
    )

    def evaluate(
        self,
        tool: AgentTool,
        context: ToolContext,
        *,
        approval_granted: bool = False,
    ) -> ToolPolicyDecision:
        if tool.name not in context.allowed_tools:
            return ToolPolicyDecision(
                allowed=False,
                reason="The tool is not allowed in this execution context.",
            )
        if tool.risk_level == ToolRiskLevel.READ_ONLY:
            return ToolPolicyDecision(
                allowed=True,
                reason="Read-only tools are automatically allowed when allowlisted.",
            )
        if tool.risk_level in self._APPROVAL_RISKS and not approval_granted:
            return ToolPolicyDecision(
                allowed=False,
                approval_required=True,
                reason="This tool requires explicit approval before execution.",
            )
        return ToolPolicyDecision(
            allowed=True,
            reason="The tool is allowlisted and the required approval was granted.",
        )

"""Framework-independent tool execution primitives."""

from agent_runtime.executor import ToolExecutor
from agent_runtime.repository import ToolCallRepository
from agent_runtime.security import REDACTED, arguments_hash, redact_sensitive
from agent_runtime.policy import ToolPolicy, ToolPolicyDecision
from agent_runtime.registry import ToolRegistry
from agent_runtime.tools.messages import (
    AgentMessage, NormalizedToolCall, TokenUsage, ToolCallStep, ToolCapableModel,
    ToolLoopOutcome, ToolLoopStatus, ToolMessageEnvelope, ToolModelResponse,
)
from agent_runtime.tools.model_adapter import LangChainToolModelAdapter
from agent_runtime.tools.loop import ToolCallingLoop
from agent_runtime.types import (
    RESUME_EVIDENCE_SOURCE_TYPES,
    AgentTool,
    ToolCallRecord,
    ToolCallEventRecord,
    ToolCallRequest,
    ToolContext,
    ToolExecutionStatus,
    ToolProvenance,
    ToolResult,
    ToolRiskLevel,
    ToolSideEffect,
    ToolDataClassification,
    provenance_may_support_resume_claim,
)

__all__ = [
    "RESUME_EVIDENCE_SOURCE_TYPES",
    "AgentTool",
    "AgentMessage",
    "NormalizedToolCall",
    "ToolCallRecord",
    "ToolCallStep",
    "ToolCallEventRecord",
    "ToolCallRequest",
    "ToolContext",
    "ToolExecutionStatus",
    "ToolExecutor",
    "ToolCallingLoop",
    "ToolLoopOutcome",
    "ToolLoopStatus",
    "ToolCapableModel",
    "ToolMessageEnvelope",
    "ToolModelResponse",
    "TokenUsage",
    "LangChainToolModelAdapter",
    "ToolCallRepository",
    "ToolPolicy",
    "ToolPolicyDecision",
    "ToolProvenance",
    "ToolRegistry",
    "ToolResult",
    "ToolRiskLevel",
    "ToolSideEffect",
    "ToolDataClassification",
    "provenance_may_support_resume_claim",
    "arguments_hash",
    "redact_sensitive",
    "REDACTED",
]

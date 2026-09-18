"""Tool implementations belong here in later runtime phases."""
"""Built-in tools and their dependency-injected registration."""

from agent_runtime.tools.job_agent_tools import register_builtin_job_agent_tools
from agent_runtime.tools.loop import TOOL_DATA_SYSTEM_RULE, ToolCallingLoop
from agent_runtime.tools.messages import (
    AgentMessage,
    NormalizedToolCall,
    TokenUsage,
    ToolCallStep,
    ToolCapableModel,
    ToolLoopOutcome,
    ToolLoopStatus,
    ToolMessageEnvelope,
    ToolModelResponse,
)
from agent_runtime.tools.model_adapter import LangChainToolModelAdapter
from agent_runtime.tools.run_reader import RunReader, SqlAlchemyRunReader
from agent_runtime.tools.schemas import (
    CompareRunRequirementsInput,
    CompareRunRequirementsOutput,
    GetRunResultInput,
    ListRecentRunsInput,
    ListRecentRunsOutput,
    PublicRunResult,
    RenderTailoredResumeInput,
    RenderTailoredResumeOutput,
)

__all__ = [
    "AgentMessage", "LangChainToolModelAdapter", "NormalizedToolCall",
    "CompareRunRequirementsInput", "CompareRunRequirementsOutput",
    "GetRunResultInput", "ListRecentRunsInput", "ListRecentRunsOutput",
    "PublicRunResult", "RenderTailoredResumeInput",
    "RenderTailoredResumeOutput", "RunReader", "SqlAlchemyRunReader",
    "TOOL_DATA_SYSTEM_RULE", "TokenUsage", "ToolCallStep", "ToolCallingLoop",
    "ToolCapableModel", "ToolLoopOutcome", "ToolLoopStatus",
    "ToolMessageEnvelope", "ToolModelResponse",
    "register_builtin_job_agent_tools",
]

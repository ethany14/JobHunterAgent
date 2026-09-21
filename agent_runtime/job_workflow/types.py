"""Persisted workflow ownership; never infer historical mode from a default."""
from enum import StrEnum


class WorkflowMode(StrEnum):
    SINGLE_CUSTOM = "single_custom"
    MULTI_AGENT_V1 = "multi_agent_v1"

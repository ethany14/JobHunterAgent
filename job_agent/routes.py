"""Transitions for the sequential LangGraph workflow."""
from typing import Literal

from langgraph.graph import END, START, StateGraph

from job_agent.schemas import VerificationResult
from job_agent.state import JobAgentState


def route_after_verification(
    state: JobAgentState,
) -> Literal["revise", "human_review"]:
    verification = state.get("verification")
    if verification is None:
        raise ValueError("Verification result is missing.")
    verification = VerificationResult.model_validate(verification)
    if verification.passed:
        return "human_review"
    if state["revision_count"] >= state["max_revisions"]:
        return "human_review"
    return "revise"


def route_after_human_review(
    state: JobAgentState,
) -> Literal["end", "revise"]:
    if state.get("approved"):
        return "end"
    return "revise"


def add_routes(builder: StateGraph) -> None:
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "analyze_resume")
    builder.add_edge("analyze_resume", "validate_extracted_evidence")
    builder.add_edge("validate_extracted_evidence", "analyze_job")
    builder.add_edge("analyze_job", "match_skills")
    builder.add_edge("match_skills", "write_resume")
    builder.add_edge("write_resume", "verify_resume")
    builder.add_conditional_edges(
        "verify_resume",
        route_after_verification,
        {"revise": "revise_resume", "human_review": "human_review"},
    )
    builder.add_edge("revise_resume", "verify_resume")
    builder.add_conditional_edges(
        "human_review",
        route_after_human_review,
        {"revise": "revise_resume", "end": END},
    )

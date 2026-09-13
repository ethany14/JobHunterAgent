"""Transitions for the sequential LangGraph workflow."""
from langgraph.graph import END, START, StateGraph


def add_routes(builder: StateGraph) -> None:
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "analyze_resume")
    builder.add_edge("analyze_resume", "analyze_job")
    builder.add_edge("analyze_job", "match_skills")
    builder.add_edge("match_skills", END)

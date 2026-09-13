"""Build and compile the job matching graph."""
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from job_agent.nodes import (
    analyze_job,
    analyze_resume,
    match_skills,
    human_review,
    revise_resume,
    validate_input,
    validate_extracted_evidence,
    verify_resume,
    write_resume,
)
from job_agent.routes import add_routes
from job_agent.state import JobAgentState


def build_graph() -> StateGraph:
    builder = StateGraph(JobAgentState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("analyze_resume", analyze_resume)
    builder.add_node("validate_extracted_evidence", validate_extracted_evidence)
    builder.add_node("analyze_job", analyze_job)
    builder.add_node("match_skills", match_skills)
    builder.add_node("write_resume", write_resume)
    builder.add_node("verify_resume", verify_resume)
    builder.add_node("revise_resume", revise_resume)
    builder.add_node("human_review", human_review)
    add_routes(builder)
    return builder


builder = build_graph()
checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)

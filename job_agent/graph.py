"""Build and compile the job matching graph."""
from langgraph.graph import StateGraph
from job_agent.nodes import analyze_job, analyze_resume, match_skills, validate_input
from job_agent.routes import add_routes
from job_agent.state import JobAgentState


def build_graph() -> StateGraph:
    builder = StateGraph(JobAgentState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("analyze_resume", analyze_resume)
    builder.add_node("analyze_job", analyze_job)
    builder.add_node("match_skills", match_skills)
    add_routes(builder)
    return builder


builder = build_graph()
graph = builder.compile()

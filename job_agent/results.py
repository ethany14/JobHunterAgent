"""Public serialization helpers shared by CLI and API entry points."""

from typing import Any

from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    SkillMatch,
    TailoredResume,
    VerificationResult,
)


def public_result(state: dict[str, Any]) -> dict[str, Any]:
    """Validate generated state and omit raw resume and job-description text."""
    required_outputs = {
        "resume_analysis": ResumeAnalysis,
        "job_analysis": JobAnalysis,
        "skill_match": SkillMatch,
    }
    output: dict[str, Any] = {}
    for field, schema in required_outputs.items():
        if field not in state:
            raise RuntimeError(f"Graph completed without producing '{field}'.")
        output[field] = schema.model_validate(state[field]).model_dump(mode="json")
    if "tailored_resume" not in state or state.get("verification") is None:
        raise RuntimeError("Graph completed without generating and verifying a resume.")
    output.update(
        {
            "tailored_resume": TailoredResume.model_validate(
                state["tailored_resume"]
            ).model_dump(mode="json"),
            "verification": VerificationResult.model_validate(
                state["verification"]
            ).model_dump(mode="json"),
            "revision_feedback": state.get("revision_feedback", []),
            "revision_count": state.get("revision_count", 0),
            "max_revisions": state.get("max_revisions", 3),
            "approved": state.get("approved"),
            "human_feedback": state.get("human_feedback"),
            "workflow_status": state.get("workflow_status", "running"),
        }
    )
    return output


def interrupt_payload(state: dict[str, Any]) -> dict[str, Any] | None:
    interruptions = state.get("__interrupt__", ())
    if not interruptions:
        return None
    payload = interruptions[0].value
    if not isinstance(payload, dict):
        raise RuntimeError("Human review interrupt returned an invalid payload.")
    return payload

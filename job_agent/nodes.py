"""Analysis nodes; the model is initialized only when a node runs."""
import os
from pathlib import Path
from typing import TypeVar

from dotenv import dotenv_values
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from job_agent.prompts import JOB_PROMPT, MATCH_PROMPT, RESUME_PROMPT
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillMatch
from job_agent.state import JobAgentState

Result = TypeVar("Result", bound=BaseModel)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _create_model() -> ChatOpenAI:
    # Read the project's .env on each call so edits take effect without restarting.
    settings = {**dotenv_values(ENV_PATH), **os.environ}
    model_id = (settings.get("LLM_MODEL_ID") or "").strip()
    if not model_id:
        raise ValueError("Set LLM_MODEL_ID in the environment or project's .env file.")
    options = {"model": model_id}
    api_key = settings.get("LLM_API_KEY")
    base_url = settings.get("LLM_BASE_URL")
    timeout = settings.get("LLM_TIMEOUT")
    if api_key:
        options["api_key"] = api_key
    if base_url:
        options["base_url"] = base_url
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError as exc:
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.") from exc
        if not 0 < seconds < float("inf"):
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.")
        options["timeout"] = seconds
    return ChatOpenAI(**options)


def _analyze(schema: type[Result], prompt: str, content: str,
             config: RunnableConfig | None = None) -> Result:
    model = _create_model()
    structured_model = model.with_structured_output(schema)
    result = structured_model.invoke(
        [("system", prompt), ("human", content)], config=config
    )
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)


def _require_text(state: JobAgentState, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value.strip()


def validate_input(state: JobAgentState) -> dict:
    """Reject invalid workflow inputs before the first paid model call."""
    _require_text(state, "resume_text")
    _require_text(state, "job_description")
    return {}


def analyze_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    resume = state["resume_text"].strip()
    return {"resume_analysis": _analyze(ResumeAnalysis, RESUME_PROMPT, resume, config)}


def analyze_job(state: JobAgentState, config: RunnableConfig) -> dict:
    job = state["job_description"].strip()
    return {"job_analysis": _analyze(JobAnalysis, JOB_PROMPT, job, config)}


def match_skills(state: JobAgentState, config: RunnableConfig) -> dict:
    if "resume_analysis" not in state or "job_analysis" not in state:
        raise ValueError("Resume and job analyses are required before matching skills.")
    resume = ResumeAnalysis.model_validate(state["resume_analysis"])
    job = JobAnalysis.model_validate(state["job_analysis"])
    content = (
        f"Resume analysis:\n{resume.model_dump_json()}\n\n"
        f"Job analysis:\n{job.model_dump_json()}"
    )
    return {"skill_match": _analyze(SkillMatch, MATCH_PROMPT, content, config)}

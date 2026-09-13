from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from job_agent.agent import graph
from job_agent.nodes import _create_model, match_skills, validate_input
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillEvidence, SkillMatch


def test_full_workflow(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL_ID=test-model\n")
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    resume = ResumeAnalysis(summary="Developer", skills=["Python"],
                            experience=["Built services"], education=[])
    job = JobAnalysis(title="Developer", summary="Build services",
                      required_skills=["Python", "SQL"], preferred_skills=[],
                      responsibilities=["Build services"])
    match = SkillMatch(matches=[
                           SkillEvidence(job_skill="Python", requirement_level="required",
                                         matched=True, resume_evidence="Python", confidence=1),
                           SkillEvidence(job_skill="SQL", requirement_level="required",
                                         matched=False, resume_evidence=None, confidence=1),
                       ], missing_skills=["SQL"], overall_score=50,
                       explanation="Python is evidenced; SQL is not.",
                       recommendations=["Add SQL experience if applicable."])
    structured = MagicMock()
    structured.invoke.side_effect = [resume, job, match]
    with patch("job_agent.nodes.ChatOpenAI") as model:
        model.return_value.with_structured_output.return_value = structured
        result = graph.invoke({"resume_text": "Built services in Python",
                               "job_description": "Requires Python and SQL"})
    assert result["resume_analysis"] == resume
    assert result["job_analysis"] == job
    assert result["skill_match"] == match
    schemas = [call.args[0] for call in model.return_value.with_structured_output.call_args_list]
    assert schemas == [ResumeAnalysis, JobAnalysis, SkillMatch]
    assert all(not call.kwargs for call in model.return_value.with_structured_output.call_args_list)
    match_content = structured.invoke.call_args_list[2].args[0][1][1]
    assert resume.model_dump_json() in match_content
    assert job.model_dump_json() in match_content


@pytest.mark.parametrize("inputs", [
    {},
    {"resume_text": " ", "job_description": "Python"},
    {"resume_text": "Python", "job_description": " "},
    {"resume_text": 123, "job_description": "Python"},
])
def test_invalid_inputs_do_not_call_model(inputs):
    with patch("job_agent.nodes.ChatOpenAI") as model:
        with pytest.raises(ValueError, match="non-empty string"):
            graph.invoke(inputs)
        model.assert_not_called()


def test_matching_requires_analyses():
    with pytest.raises(ValueError, match="analyses are required"):
        match_skills({}, {})


def test_score_is_bounded():
    with pytest.raises(ValidationError):
        SkillMatch(matches=[], missing_skills=[], overall_score=101,
                   explanation="Invalid", recommendations=[])


def test_model_settings_reload_from_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.setenv("LLM_MODEL_ID", "deployed-model")
    env_file.write_text(
        "LLM_MODEL_ID=first-model\nLLM_API_KEY=test-key\n"
        "LLM_BASE_URL=https://example.com/v1\nLLM_TIMEOUT=45\n"
    )
    with patch("job_agent.nodes.ChatOpenAI") as model:
        _create_model()
        model.assert_called_once_with(model="deployed-model", api_key="test-key",
                                      base_url="https://example.com/v1", timeout=45.0)
        env_file.write_text("LLM_MODEL_ID=second-model\n")
        _create_model()
        assert model.call_args.kwargs["model"] == "deployed-model"


def test_env_file_changes_are_reloaded_without_system_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    env_file.write_text("LLM_MODEL_ID=first-model\n")
    with patch("job_agent.nodes.ChatOpenAI") as model:
        _create_model()
        env_file.write_text("LLM_MODEL_ID=second-model\n")
        _create_model()
    assert model.call_args.kwargs["model"] == "second-model"


def test_missing_model_has_clear_error(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL_ID=\n")
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    with patch("job_agent.nodes.ChatOpenAI") as model:
        with pytest.raises(ValueError, match="LLM_MODEL_ID"):
            _create_model()
        model.assert_not_called()


def test_validate_input_has_one_responsibility():
    assert validate_input({"resume_text": " Resume ", "job_description": " Job "}) == {}

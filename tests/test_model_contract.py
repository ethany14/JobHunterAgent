from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_agent import nodes
from job_agent.model import ProviderCompatibleChatOpenAI, create_model, invoke_structured
from langchain_core.messages import AIMessage, HumanMessage
from job_agent.prompts import (
    JOB_PROMPT,
    MATCH_PROMPT,
    PROMPT_VERSION,
    REVISE_RESUME_PROMPT,
    VERIFY_RESUME_PROMPT,
    WRITE_RESUME_PROMPT,
)
from job_agent.schemas import (
    SCHEMA_VERSION,
    JobRequirement,
    MissingRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    SupportedClaim,
    UnsupportedClaim,
)


def analysis() -> ResumeAnalysis:
    return ResumeAnalysis(summary="Developer", skills=[], evidence=[], education=[])


def test_shared_model_configuration_preserves_baseline_options(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_MODEL_ID=file-model\nLLM_API_KEY=file-key\nLLM_BASE_URL=https://file.test\n",
        encoding="utf-8",
    )
    factory = MagicMock()
    create_model(
        env_path=env_path,
        environ={
            "LLM_MODEL_ID": "  deployed-model  ",
            "LLM_API_KEY": "  deployed-key  ",
            "LLM_BASE_URL": "https://provider.test/v1",
            "LLM_TIMEOUT": "12.5",
            "LLM_MAX_RETRIES": "2",
        },
        model_factory=factory,
    )
    assert factory.call_args.kwargs == {
        "model": "deployed-model",
        "temperature": 0,
        "api_key": "deployed-key",
        "base_url": "https://provider.test/v1",
        "timeout": 12.5,
        "max_retries": 2,
    }


def test_optional_model_settings_are_stripped_and_blank_values_are_omitted(tmp_path):
    factory = MagicMock()
    create_model(
        env_path=tmp_path / "missing.env",
        environ={
            "LLM_MODEL_ID": "  model-name  ",
            "LLM_API_KEY": "   ",
            "LLM_BASE_URL": "  https://provider.test/v1  ",
            "LLM_TIMEOUT": "   ",
        },
        model_factory=factory,
    )
    assert factory.call_args.kwargs == {
        "model": "model-name",
        "temperature": 0,
        "max_retries": 0,
        "base_url": "https://provider.test/v1",
    }


def test_gemini_tool_history_gets_documented_signature_fallback():
    model = ProviderCompatibleChatOpenAI(
        model="gemini-test",
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    payload = model._get_request_payload(
        [
            HumanMessage(content="Use a tool."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_recent_runs",
                        "args": {"limit": 1},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    call = payload["messages"][1]["tool_calls"][0]
    assert call["extra_content"]["google"]["thought_signature"] == (
        "skip_thought_signature_validator"
    )


def test_non_gemini_tool_history_is_not_modified_with_provider_metadata():
    model = ProviderCompatibleChatOpenAI(
        model="provider-test",
        api_key="test-key",
        base_url="https://provider.test/v1/",
    )
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "list_recent_runs",
                        "args": {"limit": 1},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    assert "extra_content" not in payload["messages"][0]["tool_calls"][0]


@pytest.mark.parametrize("value", ["-1", "1.5", "many"])
def test_retry_configuration_requires_non_negative_integer(tmp_path, value):
    with pytest.raises(ValueError, match="LLM_MAX_RETRIES"):
        create_model(
            env_path=tmp_path / "missing.env",
            environ={"LLM_MODEL_ID": "model", "LLM_MAX_RETRIES": value},
            model_factory=MagicMock(),
        )


def test_shared_structured_invocation_preserves_schema_and_messages():
    model = MagicMock()
    structured = model.with_structured_output.return_value
    structured.invoke.return_value = analysis()
    result = invoke_structured(
        model,
        ResumeAnalysis,
        "system instructions",
        "human content",
        {"tags": ["contract"]},
    )
    model.with_structured_output.assert_called_once_with(ResumeAnalysis)
    structured.invoke.assert_called_once_with(
        [("system", "system instructions"), ("human", "human content")],
        config={"tags": ["contract"]},
    )
    assert result == analysis()


def test_langgraph_node_wrapper_uses_identical_shared_message_contract(tmp_path):
    fake_model = MagicMock()
    fake_model.with_structured_output.return_value.invoke.return_value = analysis()
    with patch.object(nodes, "_create_model", return_value=fake_model):
        result = nodes._analyze(
            ResumeAnalysis,
            "system instructions",
            "human content",
            {"tags": ["baseline"]},
        )
    fake_model.with_structured_output.assert_called_once_with(ResumeAnalysis)
    fake_model.with_structured_output.return_value.invoke.assert_called_once_with(
        [("system", "system instructions"), ("human", "human content")],
        config={"tags": ["baseline"]},
    )
    assert result == analysis()


def test_prompt_v4_contains_grounding_structure_and_constraint_rules():
    normalized_verify_prompt = " ".join(VERIFY_RESUME_PROMPT.split())
    assert PROMPT_VERSION == "v4"
    assert "source_text must copy" in JOB_PROMPT
    assert "requirement_group_id" in JOB_PROMPT
    assert "user_confirmation" in JOB_PROMPT
    assert "Never infer years from seniority words" in JOB_PROMPT
    assert "minimum number of years" in MATCH_PROMPT
    assert "Do not infer duration" in MATCH_PROMPT
    assert "schema_version=2" in WRITE_RESUME_PROMPT
    assert "no more than two concise sentences" in WRITE_RESUME_PROMPT
    assert "stronger claim" in WRITE_RESUME_PROMPT
    assert "A claim with no evidence IDs is unsupported" in normalized_verify_prompt
    assert "unknown evidence ID is unsupported" in normalized_verify_prompt
    assert "untrusted editing" in REVISE_RESUME_PROMPT
    assert "summary claim" in REVISE_RESUME_PROMPT


def test_schema_v3_rejects_unknown_and_blank_contract_fields():
    assert SCHEMA_VERSION == "v3"
    with pytest.raises(ValueError):
        ResumeEvidence(
            evidence_id="EXP-1",
            source_section="Experience",
            exact_text="Fact",
            confidence=0.9,
        )
    with pytest.raises(ValueError):
        ResumeEvidence(evidence_id=" ", source_section="Experience", exact_text="Fact")
    with pytest.raises(ValueError):
        UnsupportedClaim(claim="AWS", reason="   ")


def test_resume_evidence_preserves_verbatim_text_while_cleaning_identifiers():
    evidence = ResumeEvidence(
        evidence_id="  EXP-1  ",
        source_section="  Experience  ",
        exact_text="  Built Python APIs.  ",
    )
    assert evidence.evidence_id == "EXP-1"
    assert evidence.source_section == "Experience"
    assert evidence.exact_text == "  Built Python APIs.  "
    with pytest.raises(ValueError, match="exact_text"):
        ResumeEvidence(
            evidence_id="EXP-1",
            source_section="Experience",
            exact_text="   ",
        )


def test_supported_claim_cleans_and_deduplicates_evidence_ids():
    claim = SupportedClaim(
        text="  Built Python APIs.  ",
        evidence_ids=[" EXP-1 ", "EXP-1", "EXP-2"],
        source_entry_id="legacy:unattributed",
    )
    assert claim.text == "Built Python APIs."
    assert claim.evidence_ids == ["EXP-1", "EXP-2"]
    with pytest.raises(ValueError, match="must not be blank"):
        SupportedClaim(
            text="Python",
            evidence_ids=["EXP-1", " "],
            source_entry_id="legacy:unattributed",
        )


@pytest.mark.parametrize("schema", [JobRequirement, MissingRequirement])
def test_minimum_years_must_be_positive_when_present(schema):
    values = {
        "canonical_name": "leadership_experience",
        "original_text": "Leadership experience",
        "minimum_years": 0,
    }
    if schema is JobRequirement:
        values.update(requirement_id="REQ-1", level="required")
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        schema.model_validate(values)

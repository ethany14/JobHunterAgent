from pathlib import Path
from unittest.mock import MagicMock, patch

from job_agent import nodes
from job_agent.model import create_model, invoke_structured
from job_agent.schemas import ResumeAnalysis


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
            "LLM_MODEL_ID": "deployed-model",
            "LLM_API_KEY": "deployed-key",
            "LLM_BASE_URL": "https://provider.test/v1",
            "LLM_TIMEOUT": "12.5",
        },
        model_factory=factory,
    )
    assert factory.call_args.kwargs == {
        "model": "deployed-model",
        "temperature": 0,
        "api_key": "deployed-key",
        "base_url": "https://provider.test/v1",
        "timeout": 12.5,
    }


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

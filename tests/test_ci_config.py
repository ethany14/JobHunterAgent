from pathlib import Path


def test_ci_runs_python_and_documented_browser_tests_without_live_evals():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m pytest -q" in workflow
    for test_file in (
        "tests/test_copilot_ui.mjs",
        "tests/test_learning_ui.mjs",
        "tests/test_mock_interview_ui.mjs",
    ):
        assert test_file in workflow
    assert "evals.run_" not in workflow

from evals.metrics import EvaluationResult, MissingRequirement
from evals.run_custom_parity import (
    baseline_transition_sequence,
    compare_case_records,
    summarize_backend,
)


def record(backend: str, *, revision_count=0, canonical="python"):
    result = EvaluationResult(
        case_id="case",
        case_type="valid_workflow",
        expected_behavior_achieved=True,
        reached_human_review=True,
        input_validation_passed=None,
        strict_missing_requirement_recall=None,
        canonical_missing_requirement_recall=1.0,
        expected_missing_requirement_count=1,
        strict_matched_requirement_count=0,
        canonical_matched_requirement_count=1,
        actual_missing_requirements=[MissingRequirement(canonical_name=canonical)],
        forbidden_claim_count=0,
        verification_passed=True,
        revision_count=revision_count,
        latency_seconds=1,
        error=None,
    )
    return {
        "case_id": "case",
        "backend": backend,
        "evaluation": result.model_dump(mode="json"),
        "transition_sequence": ["validate_input", "verify_resume"],
    }


def test_parity_comparison_uses_behavior_not_generated_text():
    comparison = compare_case_records(record("langgraph"), record("custom"))
    assert comparison["all_behavior_checks_match"] is True


def test_parity_comparison_detects_business_difference():
    comparison = compare_case_records(
        record("langgraph"), record("custom", revision_count=1, canonical="aws")
    )
    assert comparison["all_behavior_checks_match"] is False
    assert comparison["missing_requirements_match"] is False
    assert comparison["revision_count_match"] is False


def test_parity_summary_aggregates_canonical_metrics():
    summary = summarize_backend([record("langgraph")])
    assert summary["reached_human_review"] == 1
    assert summary["micro_canonical_recall"] == 1.0
    assert summary["forbidden_claims"] == 0


def test_baseline_transition_sequence_reflects_revision_count():
    assert baseline_transition_sequence(0)[-1] == "verify_resume"
    assert baseline_transition_sequence(1)[-3:] == [
        "verify_resume",
        "revise_resume",
        "verify_resume",
    ]

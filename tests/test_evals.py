from evals import DATASET_VERSION
from evals.metrics import (
    EvaluationResult,
    MissingRequirement,
    ReflectionEvaluationResult,
    calculate_recall,
    calculate_requirement_recall,
    count_requirement_matches,
    count_strict_matches,
    count_forbidden_claims,
)
from evals.run_evals import summarize, summarize_reflection, tailored_resume_text
from evals.run_ablation import UsageCollector, build_comparison, build_writer_only_graph
from evals.run_stability_evals import (
    consistency_summary,
    percentile,
    tag_summary,
    validate_dataset,
)
from evals.run_evals import EVALS_DIR, load_cases
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from job_agent.schemas import SupportedClaim, TailoredResume
from job_agent.nodes import canonicalize_requirement, extract_minimum_years


def workflow_result(case_id: str, *, case_type="valid_workflow", strict=None,
                    canonical=None, latency=2, reached=True) -> EvaluationResult:
    return EvaluationResult(
        case_id=case_id, case_type=case_type, expected_behavior_achieved=True,
        reached_human_review=reached,
        input_validation_passed=True if case_type == "input_validation" else None,
        strict_missing_requirement_recall=strict,
        canonical_missing_requirement_recall=canonical,
        expected_missing_requirement_count=1 if strict is not None else 0,
        strict_matched_requirement_count=int(strict or 0),
        canonical_matched_requirement_count=int(canonical or 0),
        actual_missing_requirements=[], forbidden_claim_count=0,
        verification_passed=True if reached else None, revision_count=0,
        latency_seconds=latency, error=None,
    )


def test_dataset_version_is_v3():
    assert DATASET_VERSION == "v3"


def test_empty_expected_recall_is_not_applicable():
    assert calculate_recall([], ["anything"]) is None
    assert calculate_recall(["AWS", "Kubernetes"], ["aws"]) == 0.5
    assert count_strict_matches(["AWS", "Kubernetes"], ["aws"]) == 1


def test_canonical_recall_includes_numeric_constraint():
    expected = [MissingRequirement(canonical_name="leadership_experience", minimum_years=5)]
    assert calculate_requirement_recall(
        expected, [MissingRequirement(canonical_name="LEADERSHIP_EXPERIENCE", minimum_years=5)]
    ) == 1.0
    assert calculate_requirement_recall(
        expected, [MissingRequirement(canonical_name="leadership_experience", minimum_years=2)]
    ) == 0.0
    assert count_requirement_matches(expected, expected) == 1


def test_requirement_normalization_and_year_extraction_are_deterministic():
    text = "Requires at least five years of engineering leadership experience."
    assert canonicalize_requirement("technical leadership", text) == "leadership_experience"
    assert extract_minimum_years(text) == 5


def test_count_forbidden_claims_is_case_insensitive():
    assert count_forbidden_claims(
        "Deployed to AWS but did not use kubernetes.",
        ["deployed to aws", "used Kubernetes"],
    ) == 1


def test_tailored_resume_text_includes_every_claim_group():
    resume = TailoredResume(
        professional_summary=[SupportedClaim(text="Summary", evidence_ids=["EXP-1"])],
        experience_bullets=[SupportedClaim(text="Bullet", evidence_ids=["EXP-2"])],
        highlighted_skills=[SupportedClaim(text="Python", evidence_ids=["EXP-3"])],
    )
    assert tailored_resume_text(resume) == (
        "PROFESSIONAL SUMMARY\nSummary\n\n"
        "EXPERIENCE\n• Bullet\n\nSKILLS\nPython"
    )


def test_summary_excludes_na_and_invalid_input_from_recall_and_latency():
    results = [
        workflow_result("positive-a", strict=1, canonical=1, latency=8),
        workflow_result("positive-b", strict=0, canonical=1, latency=12),
        workflow_result("no-missing", strict=None, canonical=None, latency=10),
        workflow_result("invalid", case_type="input_validation", latency=.01, reached=False),
    ]
    summary = summarize(results)
    assert summary["macro_strict_missing_requirement_recall"] == 0.5
    assert summary["micro_strict_missing_requirement_recall"] == 0.5
    assert summary["macro_canonical_missing_requirement_recall"] == 1.0
    assert summary["micro_canonical_missing_requirement_recall"] == 1.0
    assert summary["positive_recall_cases"] == 2
    assert summary["average_valid_workflow_latency_seconds"] == 10
    assert summary["average_invalid_input_rejection_latency_seconds"] == .01


def test_reflection_summary_uses_claim_level_denominators():
    results = [
        ReflectionEvaluationResult(
            case_id="a", injected_unsupported_claims=2, detected_unsupported_claims=1,
            supported_claims=2, supported_claims_incorrectly_rejected=1,
            initial_verification_passed=False, revision_successful=True,
            remaining_unsupported_claims=0, revision_count=1, latency_seconds=2, error=None,
        )
    ]
    summary = summarize_reflection(results)
    assert summary["unsupported_claim_detection_recall"] == .5
    assert summary["supported_claim_false_positive_rate"] == .5
    assert summary["revision_success_rate"] == 1


def test_usage_collector_counts_provider_tokens():
    collector = UsageCollector()
    response = LLMResult(
        generations=[[
            ChatGeneration(
                message=AIMessage(
                    content="{}",
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                )
            )
        ]]
    )
    collector.on_llm_end(response)
    assert collector.model_calls == 1
    assert collector.input_tokens == 10
    assert collector.output_tokens == 4
    assert collector.total_tokens == 14


def test_writer_only_graph_omits_verifier_nodes():
    nodes = build_writer_only_graph().nodes
    assert "write_resume" in nodes
    assert "human_review" in nodes
    assert "verify_resume" not in nodes
    assert "revise_resume" not in nodes


def test_ablation_comparison_reports_quality_and_cost_deltas():
    writer = {
        "adversarial_case_count": 3,
        "injected_unsupported_claims": 4,
        "detected_unsupported_claims": 0,
        "remaining_unsupported_claims": 4,
        "unsupported_claim_detection_recall": None,
        "unsupported_claim_removal_rate": 0.0,
        "supported_claim_false_positive_rate": None,
        "average_valid_workflow_latency_seconds": 8.0,
        "workflow_usage": {
            "valid_workflow_count": 4,
            "totals": {"model_calls": 16, "total_tokens": 8000,
                       "estimated_cost_usd": .014},
            "per_workflow_average": {"model_calls": 4, "total_tokens": 2000,
                                     "estimated_cost_usd": .0035},
        },
        "total_evaluation_usage": {"model_calls": 16, "total_tokens": 8000,
                                   "estimated_cost_usd": .014},
    }
    full = {
        "adversarial_case_count": 3,
        "injected_unsupported_claims": 4,
        "detected_unsupported_claims": 4,
        "remaining_unsupported_claims": 0,
        "unsupported_claim_detection_recall": 1.0,
        "unsupported_claim_removal_rate": 1.0,
        "supported_claim_false_positive_rate": 0.0,
        "average_valid_workflow_latency_seconds": 10.0,
        "workflow_usage": {
            "valid_workflow_count": 4,
            "totals": {"model_calls": 20, "total_tokens": 10000,
                       "estimated_cost_usd": .016},
            "per_workflow_average": {"model_calls": 5, "total_tokens": 2500,
                                     "estimated_cost_usd": .004},
        },
        "adversarial_usage": {"model_calls": 9, "total_tokens": 4000,
                              "estimated_cost_usd": .005},
        "total_evaluation_usage": {"model_calls": 29, "total_tokens": 14000,
                                   "estimated_cost_usd": .021},
    }
    comparison = build_comparison(writer, full)
    per_case = comparison["normal_workflow_per_case"]
    assert per_case["average_increase"]["latency_seconds"] == 2.0
    assert per_case["average_increase"]["model_calls"] == 1
    assert comparison["four_workflow_evaluation_totals"]["increase"]["model_calls"] == 4
    assert comparison["adversarial_suite"]["full_agent_removal_rate"] == 1.0
    assert comparison["all_evaluation_overhead"]["model_calls"] == 13


def test_stability_dataset_has_required_coverage():
    summary = validate_dataset(
        load_cases(EVALS_DIR / "stability_cases.json"),
        load_cases(EVALS_DIR / "stability_adversarial_cases.json"),
    )
    assert summary["workflow_cases"] == 20
    assert set(summary["cases_by_role"].values()) == {5}
    assert summary["tag_counts"]["prompt_injection"] >= 3
    assert summary["tag_counts"]["synonym"] >= 3
    assert summary["tag_counts"]["numeric_constraint"] >= 3
    assert 15 <= summary["unique_injected_unsupported_claims"] <= 20


def test_percentile_uses_linear_interpolation():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([1, 2, 3, 4, 5], 0.95) == 4.8


def test_consistency_reports_exact_and_modal_agreement():
    records = [
        {"case_id": "a", "signature": "x"},
        {"case_id": "a", "signature": "x"},
        {"case_id": "a", "signature": "y"},
        {"case_id": "b", "signature": "z"},
        {"case_id": "b", "signature": "z"},
        {"case_id": "b", "signature": "z"},
    ]
    summary = consistency_summary(records, "signature", 3)
    assert summary["cases_with_all_runs_identical"] == 1
    assert summary["all_runs_identical_rate"] == .5
    assert summary["mean_modal_agreement_rate"] == (2 / 3 + 1) / 2


def test_tag_summary_reports_recall_and_unexpected_missing_requirements():
    records = [
        {
            "tags": ["prompt_injection"], "reached_human_review": True,
            "forbidden_claim_count": 0, "expected_missing_requirement_count": 1,
            "canonical_matched_requirement_count": 1,
            "actual_missing_requirements": [{"canonical_name": "aws"}],
        },
        {
            "tags": ["synonym"], "reached_human_review": True,
            "forbidden_claim_count": 0, "expected_missing_requirement_count": 0,
            "canonical_matched_requirement_count": 0,
            "actual_missing_requirements": [],
        },
    ]
    summary = tag_summary(records)
    assert summary["prompt_injection"]["canonical_recall"] == 1.0
    assert summary["synonym"]["canonical_recall"] is None
    assert summary["synonym"]["unexpected_missing_requirements"] == 0

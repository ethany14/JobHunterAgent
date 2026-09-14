"""Run workflow and adversarial reflection evaluations and save JSON results."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import warnings
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from evals.metrics import (
    EvaluationResult,
    MissingRequirement,
    ReflectionEvaluationResult,
    calculate_recall,
    calculate_requirement_recall,
    count_requirement_matches,
    count_strict_matches,
    count_forbidden_claims,
    phrase_detected,
)
from job_agent.graph import builder
from job_agent.nodes import _create_model, make_evidence_id, revise_resume, verify_resume
from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    ResumeEvidence,
    SkillMatch,
    SupportedClaim,
    TailoredResume,
    VerificationResult,
)

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_CASES_PATH = EVALS_DIR / "cases.json"
DEFAULT_ADVERSARIAL_CASES_PATH = EVALS_DIR / "adversarial_cases.json"
DEFAULT_OUTPUT_PATH = EVALS_DIR / "results" / "latest.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError(f"Evaluation cases in {path} must be a JSON list.")
    return cases


def tailored_resume_text(resume: TailoredResume) -> str:
    claims = resume.professional_summary + resume.experience_bullets + resume.highlighted_skills
    return "\n".join(claim.text for claim in claims)


def _expected_requirements(case: dict[str, Any]) -> list[MissingRequirement]:
    return [MissingRequirement.model_validate(item) for item in case.get("expected_missing_requirements", [])]


def run_case(
    case: dict[str, Any],
    graph: Any,
    *,
    callbacks: list[Any] | None = None,
    thread_id: str | None = None,
) -> EvaluationResult:
    case_id = case["id"]
    is_input_validation = "expected_error" in case
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id or f"eval-{case_id}"}
    }
    if callbacks:
        config["callbacks"] = callbacks
    started = perf_counter()
    try:
        result = graph.invoke(
            {"resume_text": case["resume_text"], "job_description": case["job_description"]},
            config=config,
        )
        snapshot = graph.get_state(config)
        reached_review = bool(result.get("__interrupt__")) and snapshot.next == ("human_review",)
        if not reached_review:
            raise RuntimeError("Graph did not pause at human_review.")
        skill_match = SkillMatch.model_validate(snapshot.values["skill_match"])
        tailored_resume = TailoredResume.model_validate(snapshot.values["tailored_resume"])
        verification = VerificationResult.model_validate(snapshot.values["verification"])
        actual = [
            MissingRequirement.model_validate(item.model_dump(mode="json"))
            for item in skill_match.missing_required_requirements
        ]
        expected = _expected_requirements(case)
        expected_strict = case.get("expected_missing_required", [])
        actual_strict = [item.original_text or "" for item in actual]
        strict_recall = calculate_recall(
            expected_strict,
            actual_strict,
        )
        canonical_recall = calculate_requirement_recall(expected, actual)
        return EvaluationResult(
            case_id=case_id,
            case_type="valid_workflow",
            expected_behavior_achieved=reached_review,
            reached_human_review=reached_review,
            input_validation_passed=None,
            strict_missing_requirement_recall=strict_recall,
            canonical_missing_requirement_recall=canonical_recall,
            expected_missing_requirement_count=len(expected),
            strict_matched_requirement_count=count_strict_matches(
                expected_strict, actual_strict
            ),
            canonical_matched_requirement_count=count_requirement_matches(
                expected, actual
            ),
            actual_missing_requirements=actual,
            forbidden_claim_count=count_forbidden_claims(
                tailored_resume_text(tailored_resume), case.get("forbidden_claims", [])
            ),
            verification_passed=verification.passed,
            revision_count=snapshot.values["revision_count"],
            latency_seconds=round(perf_counter() - started, 3),
            error=None,
        )
    except Exception as exc:
        elapsed = round(perf_counter() - started, 3)
        expected_error = case.get("expected_error")
        error = str(exc)
        expected_failure = bool(expected_error and expected_error in error)
        return EvaluationResult(
            case_id=case_id,
            case_type="input_validation" if is_input_validation else "valid_workflow",
            expected_behavior_achieved=expected_failure,
            reached_human_review=False,
            input_validation_passed=expected_failure if is_input_validation else None,
            strict_missing_requirement_recall=None,
            canonical_missing_requirement_recall=None,
            expected_missing_requirement_count=0,
            strict_matched_requirement_count=0,
            canonical_matched_requirement_count=0,
            actual_missing_requirements=[],
            forbidden_claim_count=0,
            verification_passed=None,
            revision_count=0,
            latency_seconds=elapsed,
            error=None if expected_failure else error,
        )


def summarize(results: list[EvaluationResult]) -> dict[str, float | int]:
    valid = [item for item in results if item.case_type == "valid_workflow"]
    invalid = [item for item in results if item.case_type == "input_validation"]
    strict = [item.strict_missing_requirement_recall for item in valid if item.strict_missing_requirement_recall is not None]
    canonical = [item.canonical_missing_requirement_recall for item in valid if item.canonical_missing_requirement_recall is not None]
    generated = [item for item in valid if item.reached_human_review]
    expected_total = sum(item.expected_missing_requirement_count for item in valid)
    strict_matched = sum(item.strict_matched_requirement_count for item in valid)
    canonical_matched = sum(item.canonical_matched_requirement_count for item in valid)
    return {
        "evaluation_cases": len(results),
        "expected_behavior_achieved": sum(item.expected_behavior_achieved for item in results),
        "valid_workflow_cases": len(valid),
        "valid_workflows_reaching_human_review": sum(item.reached_human_review for item in valid),
        "input_validation_cases": len(invalid),
        "input_validation_cases_passed": sum(bool(item.input_validation_passed) for item in invalid),
        "macro_strict_missing_requirement_recall": sum(strict) / len(strict) if strict else 0.0,
        "micro_strict_missing_requirement_recall": strict_matched / expected_total if expected_total else 0.0,
        "macro_canonical_missing_requirement_recall": sum(canonical) / len(canonical) if canonical else 0.0,
        "micro_canonical_missing_requirement_recall": canonical_matched / expected_total if expected_total else 0.0,
        "positive_recall_cases": len(strict),
        "total_forbidden_claims": sum(item.forbidden_claim_count for item in generated),
        "average_revisions": sum(item.revision_count for item in generated) / len(generated) if generated else 0.0,
        "average_valid_workflow_latency_seconds": sum(item.latency_seconds for item in generated) / len(generated) if generated else 0.0,
        "average_invalid_input_rejection_latency_seconds": sum(item.latency_seconds for item in invalid) / len(invalid) if invalid else 0.0,
    }


def _adversarial_state(case: dict[str, Any]) -> dict[str, Any]:
    evidence_id = make_evidence_id(case["resume_text"])
    supported = [SupportedClaim(text=text, evidence_ids=[evidence_id]) for text in case["supported_claims"]]
    injected = [SupportedClaim(text=text, evidence_ids=[evidence_id]) for text in case["injected_claims"]]
    resume_analysis = ResumeAnalysis(
        summary=case["resume_text"], skills=[],
        evidence=[ResumeEvidence(evidence_id=evidence_id, source_section="Resume", exact_text=case["resume_text"])],
        education=[],
    )
    return {
        "resume_text": case["resume_text"],
        "job_description": case["job_description"],
        "resume_analysis": resume_analysis.model_dump(mode="json"),
        "job_analysis": JobAnalysis(title=None, summary=case["job_description"], requirements=[], responsibilities=[]).model_dump(mode="json"),
        "skill_match": SkillMatch(
            matches=[], explanation="Adversarial verifier evaluation.", recommendations=[],
            missing_required_requirements=[], missing_preferred_requirements=[], overall_score=0,
        ).model_dump(mode="json"),
        "tailored_resume": TailoredResume(
            professional_summary=supported, experience_bullets=injected, highlighted_skills=[]
        ).model_dump(mode="json"),
        "verification": None,
        "revision_feedback": [], "revision_count": 0, "max_revisions": 3,
        "approved": None, "human_feedback": None, "workflow_status": "running",
    }


def run_adversarial_case(
    case: dict[str, Any], callbacks: list[Any] | None = None
) -> ReflectionEvaluationResult:
    state = _adversarial_state(case)
    config: dict[str, Any] = {
        "configurable": {"thread_id": f"adversarial-{case['id']}"}
    }
    if callbacks:
        config["callbacks"] = callbacks
    started = perf_counter()
    try:
        state.update(verify_resume(state, config))
        initial = VerificationResult.model_validate(state["verification"])
        detected_text = [item.claim for item in initial.unsupported_claims]
        detected = sum(phrase_detected(claim, detected_text) for claim in case["injected_claims"])
        false_positives = sum(phrase_detected(claim, detected_text) for claim in case["supported_claims"])
        while not VerificationResult.model_validate(state["verification"]).passed and state["revision_count"] < state["max_revisions"]:
            state.update(revise_resume(state, config))
            state.update(verify_resume(state, config))
        final = VerificationResult.model_validate(state["verification"])
        return ReflectionEvaluationResult(
            case_id=case["id"],
            injected_unsupported_claims=len(case["injected_claims"]),
            detected_unsupported_claims=detected,
            supported_claims=len(case["supported_claims"]),
            supported_claims_incorrectly_rejected=false_positives,
            initial_verification_passed=initial.passed,
            revision_successful=not initial.passed and final.passed,
            remaining_unsupported_claims=len(final.unsupported_claims),
            revision_count=state["revision_count"],
            latency_seconds=round(perf_counter() - started, 3),
            error=None,
        )
    except Exception as exc:
        return ReflectionEvaluationResult(
            case_id=case["id"], injected_unsupported_claims=len(case["injected_claims"]),
            detected_unsupported_claims=0, supported_claims=len(case["supported_claims"]),
            supported_claims_incorrectly_rejected=0, initial_verification_passed=False,
            revision_successful=False, remaining_unsupported_claims=len(case["injected_claims"]),
            revision_count=state["revision_count"], latency_seconds=round(perf_counter() - started, 3), error=str(exc),
        )


def summarize_reflection(results: list[ReflectionEvaluationResult]) -> dict[str, float | int]:
    injected = sum(item.injected_unsupported_claims for item in results)
    detected = sum(item.detected_unsupported_claims for item in results)
    supported = sum(item.supported_claims for item in results)
    false_positives = sum(item.supported_claims_incorrectly_rejected for item in results)
    return {
        "adversarial_cases": len(results),
        "injected_unsupported_claims": injected,
        "detected_unsupported_claims": detected,
        "unsupported_claim_detection_recall": detected / injected if injected else 0.0,
        "supported_claim_false_positive_rate": false_positives / supported if supported else 0.0,
        "revision_success_rate": sum(item.revision_successful for item in results) / len(results) if results else 0.0,
        "remaining_unsupported_claims": sum(item.remaining_unsupported_claims for item in results),
        "average_revisions": sum(item.revision_count for item in results) / len(results) if results else 0.0,
    }


def run_evaluations(cases_path: Path, adversarial_path: Path, output_path: Path) -> dict[str, Any]:
    checkpoint_messages: list[str] = []

    class CheckpointWarningHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            checkpoint_messages.append(record.getMessage())

    checkpoint_logger = logging.getLogger("langgraph.checkpoint.serde")
    handler = CheckpointWarningHandler(level=logging.WARNING)
    checkpoint_logger.addHandler(handler)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            graph = builder.compile(
                checkpointer=InMemorySaver(
                    serde=JsonPlusSerializer(allowed_msgpack_modules=None)
                )
            )
            workflow_results = [run_case(case, graph) for case in load_cases(cases_path)]
            reflection_results = [run_adversarial_case(case) for case in load_cases(adversarial_path)]
    finally:
        checkpoint_logger.removeHandler(handler)
    model = _create_model()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "git_commit": commit,
            "model": model.model_name,
            "temperature": model.temperature,
            "prompt_version": "v1",
            "dataset_version": "v1",
        },
        "environment": {
            "langgraph": version("langgraph"),
            "langgraph_checkpoint": version("langgraph-checkpoint"),
            "pydantic": version("pydantic"),
        },
        "warnings": list(dict.fromkeys(
            [str(item.message) for item in caught] + checkpoint_messages
        )),
        "workflow_summary": summarize(workflow_results),
        "reflection_summary": summarize_reflection(reflection_results),
        "workflow_results": [item.model_dump(mode="json") for item in workflow_results],
        "reflection_results": [item.model_dump(mode="json") for item in reflection_results],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run synthetic job-agent evaluations.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--adversarial-cases", type=Path, default=DEFAULT_ADVERSARIAL_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = run_evaluations(args.cases, args.adversarial_cases, args.output)
    workflow = report["workflow_summary"]
    reflection = report["reflection_summary"]
    print(f"Evaluation cases: {workflow['evaluation_cases']}")
    print(f"Expected behavior achieved: {workflow['expected_behavior_achieved']}/{workflow['evaluation_cases']}")
    print(f"Valid workflows reaching human review: {workflow['valid_workflows_reaching_human_review']}/{workflow['valid_workflow_cases']}")
    print(f"Input-validation cases passed: {workflow['input_validation_cases_passed']}/{workflow['input_validation_cases']}")
    print(f"Macro strict missing-requirement recall: {workflow['macro_strict_missing_requirement_recall']:.1%}")
    print(f"Micro strict missing-requirement recall: {workflow['micro_strict_missing_requirement_recall']:.1%}")
    print(f"Macro canonical missing-requirement recall: {workflow['macro_canonical_missing_requirement_recall']:.1%}")
    print(f"Micro canonical missing-requirement recall: {workflow['micro_canonical_missing_requirement_recall']:.1%}")
    print(f"Forbidden claims: {workflow['total_forbidden_claims']}")
    print(f"Average revisions: {workflow['average_revisions']:.2f}")
    print(f"Average valid-workflow latency: {workflow['average_valid_workflow_latency_seconds']:.2f}s")
    print(f"Invalid-input rejection latency: {workflow['average_invalid_input_rejection_latency_seconds']:.3f}s")
    print(f"Unsupported-claim detection recall: {reflection['unsupported_claim_detection_recall']:.1%}")
    print(f"Supported-claim false-positive rate: {reflection['supported_claim_false_positive_rate']:.1%}")
    print(f"Revision success rate: {reflection['revision_success_rate']:.1%}")
    print(f"Checkpoint warnings captured: {len(report['warnings'])}")
    print(f"Results saved to: {args.output}")
    all_workflows_pass = workflow["expected_behavior_achieved"] == workflow["evaluation_cases"]
    no_reflection_errors = all(item["error"] is None for item in report["reflection_results"])
    return 0 if all_workflows_pass and no_reflection_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

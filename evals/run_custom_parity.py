"""Compare LangGraph and custom runtimes on the same workflow cases."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from evals import DATASET_VERSION
from evals.metrics import (
    EvaluationResult,
    MissingRequirement,
    calculate_recall,
    calculate_requirement_recall,
    count_forbidden_claims,
    count_requirement_matches,
    count_strict_matches,
    requirement_key,
)
from evals.run_ablation import compile_graph
from evals.run_evals import EVALS_DIR, load_cases, run_case, tailored_resume_text
from custom_agent.handlers import JobAgentStepHandler
from custom_agent.loop import AgentLoop
from custom_agent.repository import StateRepository
from custom_agent.state import AgentStatus, Step
from api.db import create_database
from job_agent.graph import builder
from job_agent.nodes import _create_model
from job_agent.prompts import PROMPT_VERSION
from job_agent.schemas import (
    SCHEMA_VERSION,
    SkillMatch,
    TailoredResume,
    VerificationResult,
)

DEFAULT_CASES_PATH = EVALS_DIR / "stability_cases.json"
DEFAULT_OUTPUT_PATH = EVALS_DIR / "results" / "custom_v0.1.1_parity.json"
COMPUTATION_STEPS = {
    "validate_input",
    "analyze_resume",
    "validate_extracted_evidence",
    "analyze_job",
    "match_skills",
    "write_resume",
    "verify_resume",
    "revise_resume",
}


def dependency_versions() -> dict[str, str]:
    """Capture the runtime versions needed to interpret a parity artifact."""
    versions = {"python": platform.python_version()}
    for package in (
        "langgraph",
        "langgraph-checkpoint",
        "langgraph-checkpoint-sqlite",
        "langchain-core",
        "langchain-openai",
        "pydantic",
        "sqlalchemy",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _expected_requirements(case: dict[str, Any]) -> list[MissingRequirement]:
    return [
        MissingRequirement.model_validate(item)
        for item in case.get("expected_missing_requirements", [])
    ]


def evaluate_outputs(
    case: dict[str, Any],
    *,
    skill_match: SkillMatch,
    tailored_resume: TailoredResume,
    verification: VerificationResult,
    revision_count: int,
    reached_human_review: bool,
    latency_seconds: float,
) -> EvaluationResult:
    actual = [
        MissingRequirement.model_validate(item.model_dump(mode="json"))
        for item in skill_match.missing_required_requirements
    ]
    expected = _expected_requirements(case)
    expected_strict = case.get("expected_missing_required", [])
    actual_strict = [item.original_text or "" for item in actual]
    return EvaluationResult(
        case_id=case["id"],
        case_type="valid_workflow",
        expected_behavior_achieved=reached_human_review,
        reached_human_review=reached_human_review,
        input_validation_passed=None,
        strict_missing_requirement_recall=calculate_recall(
            expected_strict, actual_strict
        ),
        canonical_missing_requirement_recall=calculate_requirement_recall(
            expected, actual
        ),
        expected_missing_requirement_count=len(expected),
        strict_matched_requirement_count=count_strict_matches(
            expected_strict, actual_strict
        ),
        canonical_matched_requirement_count=count_requirement_matches(expected, actual),
        actual_missing_requirements=actual,
        forbidden_claim_count=count_forbidden_claims(
            tailored_resume_text(tailored_resume), case.get("forbidden_claims", [])
        ),
        verification_passed=verification.passed,
        revision_count=revision_count,
        latency_seconds=round(latency_seconds, 3),
        error=None,
    )


def baseline_transition_sequence(revision_count: int) -> list[str]:
    """Return the deterministic LangGraph route implied by its final revision count."""
    steps = [
        "validate_input",
        "analyze_resume",
        "validate_extracted_evidence",
        "analyze_job",
        "match_skills",
        "write_resume",
    ]
    for _ in range(revision_count):
        steps.extend(["verify_resume", "revise_resume"])
    steps.append("verify_resume")
    return steps


def custom_transition_sequence(repository: StateRepository, run_id: str) -> list[str]:
    return [
        event.step.value
        for event in repository.events(run_id)
        if event.event_type.value in {"step_completed", "paused_for_review"}
        and event.step.value in COMPUTATION_STEPS
    ]


def summarize_backend(records: list[dict[str, Any]]) -> dict[str, Any]:
    results = [EvaluationResult.model_validate(item["evaluation"]) for item in records]
    positive = [
        result.canonical_missing_requirement_recall
        for result in results
        if result.canonical_missing_requirement_recall is not None
    ]
    expected = sum(result.expected_missing_requirement_count for result in results)
    matched = sum(result.canonical_matched_requirement_count for result in results)
    return {
        "cases": len(results),
        "reached_human_review": sum(result.reached_human_review for result in results),
        "macro_canonical_recall": mean(positive) if positive else 0.0,
        "micro_canonical_recall": matched / expected if expected else 0.0,
        "forbidden_claims": sum(result.forbidden_claim_count for result in results),
        "verification_pass_rate": (
            sum(result.verification_passed is True for result in results) / len(results)
            if results
            else 0.0
        ),
        "mean_revisions": mean(result.revision_count for result in results),
        "mean_latency_seconds": mean(result.latency_seconds for result in results),
    }


def compare_case_records(
    langgraph_record: dict[str, Any], custom_record: dict[str, Any]
) -> dict[str, Any]:
    langgraph_result = EvaluationResult.model_validate(langgraph_record["evaluation"])
    custom_result = EvaluationResult.model_validate(custom_record["evaluation"])
    langgraph_missing = sorted(
        requirement_key(item) for item in langgraph_result.actual_missing_requirements
    )
    custom_missing = sorted(
        requirement_key(item) for item in custom_result.actual_missing_requirements
    )
    checks = {
        "pause_status_match": (
            langgraph_result.reached_human_review
            == custom_result.reached_human_review
        ),
        "transition_sequence_match": (
            langgraph_record["transition_sequence"]
            == custom_record["transition_sequence"]
        ),
        "missing_requirements_match": langgraph_missing == custom_missing,
        "verification_verdict_match": (
            langgraph_result.verification_passed
            == custom_result.verification_passed
        ),
        "revision_count_match": (
            langgraph_result.revision_count == custom_result.revision_count
        ),
        "forbidden_claim_count_match": (
            langgraph_result.forbidden_claim_count
            == custom_result.forbidden_claim_count
        ),
    }
    return {
        "case_id": langgraph_result.case_id,
        **checks,
        "all_behavior_checks_match": all(checks.values()),
    }


def run_parity_evaluation(cases_path: Path, output_path: Path) -> dict[str, Any]:
    evaluated_source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty_before_run = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    cases = load_cases(cases_path)
    if len(cases) != 20:
        raise ValueError(f"Parity evaluation requires 20 cases; received {len(cases)}.")
    graph = compile_graph(builder)
    model = _create_model()
    langgraph_records: list[dict[str, Any]] = []
    custom_records: list[dict[str, Any]] = []

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output_path.parent) as temporary_directory:
        database_path = Path(temporary_directory) / "custom-parity.sqlite"
        database = create_database(
            f"sqlite:///{database_path.as_posix()}", create_schema_for_tests=True
        )
        repository = StateRepository(database.session_factory)
        loop = AgentLoop(repository=repository, handler=JobAgentStepHandler())
        try:
            for index, case in enumerate(cases, start=1):
                langgraph_config = {
                    "configurable": {
                        "thread_id": f"parity-langgraph-{case['id']}"
                    }
                }
                langgraph_result = run_case(
                    case,
                    graph,
                    thread_id=langgraph_config["configurable"]["thread_id"],
                )
                langgraph_records.append(
                    {
                        "case_id": case["id"],
                        "evaluation": langgraph_result.model_dump(mode="json"),
                        "transition_sequence": baseline_transition_sequence(
                            langgraph_result.revision_count
                        ),
                    }
                )

                custom_run_id = f"parity-custom-{index:02d}"
                started = perf_counter()
                state = loop.start(
                    run_id=custom_run_id,
                    resume_text=case["resume_text"],
                    job_description=case["job_description"],
                )
                elapsed = perf_counter() - started
                custom_result = evaluate_outputs(
                    case,
                    skill_match=state.skill_match,
                    tailored_resume=state.tailored_resume,
                    verification=state.verification,
                    revision_count=state.revision_count,
                    reached_human_review=(
                        state.step == Step.HUMAN_REVIEW
                        and state.status == AgentStatus.AWAITING_REVIEW
                    ),
                    latency_seconds=elapsed,
                )
                custom_records.append(
                    {
                        "case_id": case["id"],
                        "evaluation": custom_result.model_dump(mode="json"),
                        "transition_sequence": custom_transition_sequence(
                            repository, custom_run_id
                        ),
                    }
                )
                print(f"Parity case {index}/20: {case['id']}", flush=True)
        finally:
            database.close()

    comparisons = [
        compare_case_records(langgraph_record, custom_record)
        for langgraph_record, custom_record in zip(
            langgraph_records, custom_records, strict=True
        )
    ]
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "evaluated_source_commit": evaluated_source_commit,
            "dirty_before_run": dirty_before_run,
            "model": model.model_name,
            "temperature": model.temperature,
            "max_retries": model.max_retries,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "runs_per_case": 1,
            "cases_per_backend": len(cases),
            "dependency_versions": dependency_versions(),
            "backend_policy": "langgraph_frozen_custom_default",
            "transition_comparison": (
                "LangGraph route derived from final revision_count; custom route read "
                "from persisted events."
            ),
        },
        "langgraph": {
            "summary": summarize_backend(langgraph_records),
            "records": langgraph_records,
        },
        "custom_agent": {
            "summary": summarize_backend(custom_records),
            "records": custom_records,
        },
        "comparison": {
            "cases_with_all_behavior_checks_matching": sum(
                item["all_behavior_checks_match"] for item in comparisons
            ),
            "total_cases": len(comparisons),
            "check_match_counts": {
                check: sum(item[check] for item in comparisons)
                for check in (
                    "pause_status_match",
                    "transition_sequence_match",
                    "missing_requirements_match",
                    "verification_verdict_match",
                    "revision_count_match",
                    "forbidden_claim_count_match",
                )
            },
            "per_case": comparisons,
        },
    }
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    report = run_parity_evaluation(args.cases, args.output)
    print(json.dumps({
        "langgraph": report["langgraph"]["summary"],
        "custom_agent": report["custom_agent"]["summary"],
        "comparison": report["comparison"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

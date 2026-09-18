"""Run the paired v3 requirement-intelligence regression evaluation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from api.db import create_database
from custom_agent.handlers import JobAgentStepHandler
from custom_agent.loop import AgentLoop
from custom_agent.repository import StateRepository
from custom_agent.state import AgentStatus, Step
from evals import DATASET_VERSION
from evals.run_ablation import compile_graph
from evals.run_evals import EVALS_DIR, load_cases, tailored_resume_text
from job_agent.domain import calculate_score_breakdown, normalize_text
from job_agent.graph import builder
from job_agent.nodes import _create_model
from job_agent.prompts import PROMPT_VERSION
from job_agent.schemas import (
    SCHEMA_VERSION,
    JobAnalysis,
    JobRequirement,
    SkillEvidence,
    SkillMatch,
    TailoredResume,
    VerificationResult,
    VerificationMode,
)

DEFAULT_CASES_PATH = EVALS_DIR / "requirement_intelligence_v3_cases.json"
DEFAULT_OUTPUT_PATH = EVALS_DIR / "results" / "requirement_intelligence_v3.json"
DEFAULT_BASELINE_PATH = EVALS_DIR / "results" / "pre_requirement_intelligence_v3.json"
RUNS_PER_CASE = 3


def semantic_key(requirement: JobRequirement) -> str | None:
    """Map Deloitte requirement wording to deterministic evaluation concepts."""
    text = normalize_text(
        " ".join(
            (
                requirement.canonical_name.replace("_", " "),
                requirement.display_name,
                requirement.atomic_text,
            )
        )
    )
    if "certified scrum master" in text:
        return "certified_scrum_master"
    if "itil foundation" in text:
        return "itil_foundation"
    if "claude" in text and "certification" in text:
        return "claude_certification"
    if "claude code" in text:
        return "claude_code"
    if re.search(r"\bclaude\b", text):
        return "claude"
    if re.search(r"\bcowork\b", text):
        return "cowork"
    if re.search(r"\bpower\s*bi\b", text):
        return "power_bi"
    if "bachelor" in text:
        return "bachelors_degree"
    if "master" in text and "degree" in text:
        return "masters_degree"
    if "consulting firm" in text and "3" in text:
        return "consulting_experience"
    if "project management" in text and "2" in text:
        return "it_project_management_experience"
    if "legally authorized" in text or "work authorization" in text:
        return "work_authorization"
    if "travel" in text and ("50" in text or "ability" in text):
        return "travel"
    return None


def _requirements_by_key(job: JobAnalysis) -> dict[str, JobRequirement]:
    mapped: dict[str, JobRequirement] = {}
    for requirement in job.requirements:
        key = semantic_key(requirement)
        if key is not None and key not in mapped:
            mapped[key] = requirement
    return mapped


def _matches_by_key(
    job: JobAnalysis, skill_match: SkillMatch
) -> dict[str, SkillEvidence]:
    requirements_by_id = {item.requirement_id: item for item in job.requirements}
    mapped: dict[str, SkillEvidence] = {}
    for match in skill_match.matches:
        requirement = requirements_by_id.get(match.requirement_id)
        if requirement is None:
            continue
        key = semantic_key(requirement)
        if key is not None and key not in mapped:
            mapped[key] = match
    return mapped


def _grouped_score_is_invariant(
    job: JobAnalysis, skill_match: SkillMatch
) -> bool:
    baseline = calculate_score_breakdown(skill_match.matches, job.requirements)
    group_members: dict[str, list[tuple[JobRequirement, SkillEvidence]]] = {}
    matches_by_id = {item.requirement_id: item for item in skill_match.matches}
    for requirement in job.requirements:
        match = matches_by_id.get(requirement.requirement_id)
        if match is not None:
            group_members.setdefault(requirement.requirement_group_id, []).append(
                (requirement, match)
            )
    compound = next((items for items in group_members.values() if len(items) > 1), None)
    if compound is None:
        return False

    duplicated_requirements = list(job.requirements)
    duplicated_matches = list(skill_match.matches)
    for requirement, match in compound:
        duplicate_id = f"{requirement.requirement_id}-EVAL-DUPLICATE"
        duplicated_requirements.append(
            JobRequirement.model_validate(
                {
                    **requirement.model_dump(mode="python"),
                    "requirement_id": duplicate_id,
                }
            )
        )
        duplicated_matches.append(
            SkillEvidence.model_validate(
                {
                    **match.model_dump(mode="python"),
                    "requirement_id": duplicate_id,
                }
            )
        )
    duplicated = calculate_score_breakdown(
        duplicated_matches, duplicated_requirements
    )
    return duplicated == baseline


def evaluate_outputs(
    case: dict[str, Any],
    *,
    job: JobAnalysis,
    skill_match: SkillMatch,
    tailored_resume: TailoredResume,
) -> dict[str, Any]:
    requirements = _requirements_by_key(job)
    matches = _matches_by_key(job, skill_match)

    expected_categories = case["expected_categories"]
    category_details = {
        key: {
            "expected": expected,
            "actual": (
                requirements[key].category.value if key in requirements else None
            ),
            "correct": (
                key in requirements and requirements[key].category.value == expected
            ),
        }
        for key, expected in expected_categories.items()
    }
    category_accuracy = sum(
        item["correct"] for item in category_details.values()
    ) / len(category_details)

    confirmation_ids = {
        item.requirement_id for item in skill_match.confirmation_requirements
    }
    eligibility_details: dict[str, Any] = {}
    for key in case["expected_eligibility"]:
        requirement = requirements.get(key)
        match = matches.get(key)
        checks = {
            "found": requirement is not None and match is not None,
            "user_confirmation_mode": (
                requirement is not None
                and requirement.verification_mode
                == VerificationMode.USER_CONFIRMATION
            ),
            "needs_confirmation_status": (
                match is not None and match.match_status == "needs_confirmation"
            ),
            "listed_for_confirmation": (
                requirement is not None
                and requirement.requirement_id in confirmation_ids
            ),
        }
        eligibility_details[key] = {
            **checks,
            "violation": not all(checks.values()),
        }
    eligibility_violations = sum(
        item["violation"] for item in eligibility_details.values()
    )

    duration_details = []
    for expected in case["expected_duration_decisions"]:
        key = expected["canonical_key"]
        requirement = requirements.get(key)
        match = matches.get(key)
        correct = bool(
            requirement is not None
            and match is not None
            and requirement.minimum_years == expected["minimum_years"]
            and match.match_status == expected["expected_status"]
        )
        duration_details.append(
            {
                **expected,
                "actual_minimum_years": (
                    requirement.minimum_years if requirement is not None else None
                ),
                "actual_status": match.match_status if match is not None else None,
                "correct": correct,
            }
        )
    duration_accuracy = sum(item["correct"] for item in duration_details) / len(
        duration_details
    )

    education_expected = case["expected_in_progress_education"]
    education_match = matches.get(education_expected["canonical_key"])
    in_progress_education_correct = bool(
        education_match is not None
        and education_match.match_status == education_expected["expected_status"]
    )

    group_details = []
    for expected_group in case["expected_groups"]:
        group_requirements = [requirements.get(key) for key in expected_group]
        found = all(item is not None for item in group_requirements)
        group_ids = {
            item.requirement_group_id
            for item in group_requirements
            if item is not None
        }
        correct = found and len(group_ids) == 1
        group_details.append(
            {
                "canonical_keys": expected_group,
                "actual_group_ids": sorted(group_ids),
                "correct": correct,
            }
        )
    group_accuracy = sum(item["correct"] for item in group_details) / len(
        group_details
    )

    expected_missing = set(case["expected_missing_canonical"])
    actual_missing = {
        key
        for key, match in matches.items()
        if match.match_status in {"missing", "partial"}
    }
    canonical_missing_recall = len(expected_missing & actual_missing) / len(
        expected_missing
    )

    generated_text = tailored_resume_text(tailored_resume)
    unsupported_claims = [
        claim
        for claim in case["forbidden_claims"]
        if claim.casefold() in generated_text.casefold()
    ]
    return {
        "requirement_category_accuracy": category_accuracy,
        "requirement_category_details": category_details,
        "eligibility_inference_violations": eligibility_violations,
        "eligibility_details": eligibility_details,
        "duration_decision_accuracy": duration_accuracy,
        "duration_details": duration_details,
        "in_progress_education_accuracy": float(in_progress_education_correct),
        "in_progress_education_correct": in_progress_education_correct,
        "group_accuracy": group_accuracy,
        "group_details": group_details,
        "grouped_score_invariance": _grouped_score_is_invariant(job, skill_match),
        "canonical_missing_recall": canonical_missing_recall,
        "expected_missing_canonical": sorted(expected_missing),
        "actual_missing_canonical": sorted(actual_missing),
        "unsupported_claim_count": len(unsupported_claims),
        "unsupported_claims": unsupported_claims,
    }


def _record(
    case: dict[str, Any],
    *,
    backend: str,
    repeat: int,
    job: JobAnalysis,
    skill_match: SkillMatch,
    tailored_resume: TailoredResume,
    verification: Any,
    revision_count: int,
    reached_human_review: bool,
    latency_seconds: float,
) -> dict[str, Any]:
    verification = VerificationResult.model_validate(verification)
    return {
        "case_id": case["id"],
        "backend": backend,
        "repeat": repeat,
        "reached_human_review": reached_human_review,
        "latency_seconds": round(latency_seconds, 3),
        "revision_count": revision_count,
        "verification_passed": verification.passed,
        "metrics": evaluate_outputs(
            case,
            job=job,
            skill_match=skill_match,
            tailored_resume=tailored_resume,
        ),
        "outputs": {
            "job_analysis": job.model_dump(mode="json"),
            "skill_match": skill_match.model_dump(mode="json"),
            "tailored_resume": tailored_resume.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        },
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [item["metrics"] for item in records]
    return {
        "runs": len(records),
        "runs_reaching_human_review": sum(
            item["reached_human_review"] for item in records
        ),
        "mean_requirement_category_accuracy": mean(
            item["requirement_category_accuracy"] for item in metrics
        ),
        "total_eligibility_inference_violations": sum(
            item["eligibility_inference_violations"] for item in metrics
        ),
        "mean_duration_decision_accuracy": mean(
            item["duration_decision_accuracy"] for item in metrics
        ),
        "mean_in_progress_education_accuracy": mean(
            item["in_progress_education_accuracy"] for item in metrics
        ),
        "mean_group_accuracy": mean(item["group_accuracy"] for item in metrics),
        "grouped_score_invariance_rate": mean(
            item["grouped_score_invariance"] for item in metrics
        ),
        "mean_canonical_missing_recall": mean(
            item["canonical_missing_recall"] for item in metrics
        ),
        "total_unsupported_claims": sum(
            item["unsupported_claim_count"] for item in metrics
        ),
        "mean_latency_seconds": mean(
            item["latency_seconds"] for item in records
        ),
        "distinct_metric_outcomes": len(
            {
                json.dumps(item, sort_keys=True)
                for item in metrics
            }
        ),
    }


def baseline_comparison(
    baseline: dict[str, Any],
    langgraph_summary: dict[str, Any],
    custom_summary: dict[str, Any],
) -> dict[str, Any]:
    shared = {}
    for backend, current in (
        ("langgraph", langgraph_summary),
        ("custom_agent", custom_summary),
    ):
        old = baseline[backend]["summary"]
        shared[backend] = {
            "canonical_missing_recall": {
                "pre_v3": old["macro_canonical_recall"],
                "v3_deloitte_regression": current[
                    "mean_canonical_missing_recall"
                ],
                "comparable_scope": False,
                "note": "Different datasets; values are shown side by side, not pooled.",
            },
            "unsupported_claims": {
                "pre_v3_total": old["forbidden_claims"],
                "v3_deloitte_total": current["total_unsupported_claims"],
                "comparable_scope": False,
                "note": "Pre-v3 used 20 cases once; v3 uses one regression case three times.",
            },
        }
    return {
        "frozen_baseline": str(DEFAULT_BASELINE_PATH.as_posix()),
        "frozen_baseline_metadata": baseline["run_metadata"],
        "shared_metrics": shared,
        "new_v3_metrics_in_pre_v3_baseline": "not_available",
    }


def run_evaluation(
    cases_path: Path,
    output_path: Path,
    baseline_path: Path,
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    graph = compile_graph(builder)
    model = _create_model()
    langgraph_records: list[dict[str, Any]] = []
    custom_records: list[dict[str, Any]] = []

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output_path.parent) as temporary_directory:
        database_path = Path(temporary_directory) / "requirement-intelligence.sqlite"
        database = create_database(
            f"sqlite:///{database_path.as_posix()}", create_schema_for_tests=True
        )
        repository = StateRepository(database.session_factory)
        loop = AgentLoop(repository=repository, handler=JobAgentStepHandler())
        try:
            for case in cases:
                for repeat in range(1, RUNS_PER_CASE + 1):
                    config = {
                        "configurable": {
                            "thread_id": (
                                f"requirement-v3-langgraph-{case['id']}-{repeat}"
                            )
                        }
                    }
                    started = perf_counter()
                    result = graph.invoke(
                        {
                            "resume_text": case["resume_text"],
                            "job_description": case["job_description"],
                        },
                        config=config,
                    )
                    elapsed = perf_counter() - started
                    snapshot = graph.get_state(config)
                    langgraph_records.append(
                        _record(
                            case,
                            backend="langgraph",
                            repeat=repeat,
                            job=JobAnalysis.model_validate(
                                snapshot.values["job_analysis"]
                            ),
                            skill_match=SkillMatch.model_validate(
                                snapshot.values["skill_match"]
                            ),
                            tailored_resume=TailoredResume.model_validate(
                                snapshot.values["tailored_resume"]
                            ),
                            verification=snapshot.values["verification"],
                            revision_count=snapshot.values["revision_count"],
                            reached_human_review=(
                                bool(result.get("__interrupt__"))
                                and snapshot.next == ("human_review",)
                            ),
                            latency_seconds=elapsed,
                        )
                    )
                    print(
                        f"LangGraph {case['id']} repeat {repeat}/{RUNS_PER_CASE}",
                        flush=True,
                    )

                    custom_run_id = f"requirement-v3-custom-{case['id']}-{repeat}"
                    started = perf_counter()
                    state = loop.start(
                        run_id=custom_run_id,
                        resume_text=case["resume_text"],
                        job_description=case["job_description"],
                    )
                    elapsed = perf_counter() - started
                    custom_records.append(
                        _record(
                            case,
                            backend="custom_agent",
                            repeat=repeat,
                            job=state.job_analysis,
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
                    )
                    print(
                        f"Custom {case['id']} repeat {repeat}/{RUNS_PER_CASE}",
                        flush=True,
                    )
        finally:
            database.close()

    langgraph_summary = summarize(langgraph_records)
    custom_summary = summarize(custom_records)
    paired = []
    for langgraph_record, custom_record in zip(
        langgraph_records, custom_records, strict=True
    ):
        langgraph_metrics = langgraph_record["metrics"]
        custom_metrics = custom_record["metrics"]
        paired.append(
            {
                "case_id": langgraph_record["case_id"],
                "repeat": langgraph_record["repeat"],
                "metrics_match": langgraph_metrics == custom_metrics,
                "langgraph_metrics": langgraph_metrics,
                "custom_agent_metrics": custom_metrics,
            }
        )

    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "evaluated_source_commit": source_commit,
            "dirty_before_run": bool(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            ),
            "model": model.model_name,
            "temperature": model.temperature,
            "max_retries": model.max_retries,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "runs_per_case": RUNS_PER_CASE,
            "cases": len(cases),
            "dataset_path": str(cases_path.as_posix()),
        },
        "langgraph": {
            "summary": langgraph_summary,
            "records": langgraph_records,
        },
        "custom_agent": {
            "summary": custom_summary,
            "records": custom_records,
        },
        "paired_backend_comparison": {
            "metric_matches": sum(item["metrics_match"] for item in paired),
            "pairs": len(paired),
            "records": paired,
        },
        "comparison_to_frozen_pre_v3": baseline_comparison(
            baseline, langgraph_summary, custom_summary
        ),
    }
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    args = parser.parse_args()
    report = run_evaluation(args.cases, args.output, args.baseline)
    print(
        json.dumps(
            {
                "run_metadata": report["run_metadata"],
                "langgraph": report["langgraph"]["summary"],
                "custom_agent": report["custom_agent"]["summary"],
                "paired_backend_comparison": report[
                    "paired_backend_comparison"
                ],
                "comparison_to_frozen_pre_v3": report[
                    "comparison_to_frozen_pre_v3"
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

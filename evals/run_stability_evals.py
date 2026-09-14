"""Run the larger repeated stability evaluation before API integration."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from evals.metrics import EvaluationResult, requirement_key
from evals.run_ablation import UsageCollector, compile_graph, usage_report
from evals.run_evals import EVALS_DIR, load_cases, run_adversarial_case, run_case
from job_agent.graph import builder
from job_agent.nodes import _create_model
from job_agent.schemas import TailoredResume

DEFAULT_CASES_PATH = EVALS_DIR / "stability_cases.json"
DEFAULT_ADVERSARIAL_CASES_PATH = EVALS_DIR / "stability_adversarial_cases.json"
DEFAULT_OUTPUT_PATH = EVALS_DIR / "results" / "stability_v0.2.json"
DEFAULT_RUNS_PER_CASE = 3


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def validate_dataset(
    cases: list[dict[str, Any]], adversarial_cases: list[dict[str, Any]]
) -> dict[str, Any]:
    roles = Counter(case.get("role") for case in cases)
    required_roles = {
        "backend": 5,
        "data_analyst": 5,
        "business_analyst": 5,
        "ai_engineer": 5,
    }
    if len(cases) != 20 or roles != required_roles:
        raise ValueError(f"Expected 20 cases split 5 per role; received {dict(roles)}")
    tag_counts = Counter(tag for case in cases for tag in case.get("tags", []))
    for tag in ("prompt_injection", "synonym", "numeric_constraint"):
        if tag_counts[tag] < 3:
            raise ValueError(f"Dataset requires at least three {tag} cases.")
    injected = sum(len(case.get("injected_claims", [])) for case in adversarial_cases)
    if not 15 <= injected <= 20:
        raise ValueError("Adversarial dataset must contain 15 to 20 injected claims.")
    return {
        "workflow_cases": len(cases),
        "cases_by_role": dict(roles),
        "tag_counts": dict(tag_counts),
        "adversarial_cases": len(adversarial_cases),
        "unique_injected_unsupported_claims": injected,
    }


def decision_signature(result: EvaluationResult) -> str:
    requirements = sorted(requirement_key(item) for item in result.actual_missing_requirements)
    value = {
        "missing_requirements": requirements,
        "verification_passed": result.verification_passed,
        "forbidden_claim_count": result.forbidden_claim_count,
        "revision_count": result.revision_count,
        "error": result.error,
    }
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def content_hash(graph: Any, thread_id: str) -> str | None:
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    value = snapshot.values.get("tailored_resume")
    if value is None:
        return None
    resume = TailoredResume.model_validate(value).model_dump(mode="json")
    rendered = json.dumps(resume, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def consistency_summary(
    records: list[dict[str, Any]], signature_field: str, runs_per_case: int
) -> dict[str, Any]:
    grouped: dict[str, list[str | None]] = defaultdict(list)
    for record in records:
        grouped[record["case_id"]].append(record[signature_field])
    per_case = {}
    modal_rates = []
    exact_count = 0
    for case_id, signatures in grouped.items():
        modal = max(Counter(signatures).values()) / len(signatures)
        exact = len(signatures) == runs_per_case and len(set(signatures)) == 1
        modal_rates.append(modal)
        exact_count += exact
        per_case[case_id] = {
            "all_runs_identical": exact,
            "modal_agreement_rate": modal,
        }
    return {
        "definition": signature_field,
        "cases_with_all_runs_identical": exact_count,
        "total_cases": len(grouped),
        "all_runs_identical_rate": exact_count / len(grouped) if grouped else 0.0,
        "mean_modal_agreement_rate": mean(modal_rates) if modal_rates else 0.0,
        "per_case": per_case,
    }


def tag_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    tags = sorted({tag for record in records for tag in record.get("tags", [])})
    summary = {}
    for tag in tags:
        selected = [record for record in records if tag in record.get("tags", [])]
        expected = sum(record["expected_missing_requirement_count"] for record in selected)
        matched = sum(record["canonical_matched_requirement_count"] for record in selected)
        summary[tag] = {
            "runs": len(selected),
            "runs_reaching_human_review": sum(record["reached_human_review"] for record in selected),
            "forbidden_claims": sum(record["forbidden_claim_count"] for record in selected),
            "expected_missing_requirements": expected,
            "canonical_missing_requirements_recalled": matched,
            "canonical_recall": matched / expected if expected else None,
            "unexpected_missing_requirements": sum(
                len(record["actual_missing_requirements"])
                for record in selected
                if record["expected_missing_requirement_count"] == 0
            ),
        }
    return summary


def run_stability_evaluation(
    cases_path: Path,
    adversarial_path: Path,
    output_path: Path,
    runs_per_case: int,
) -> dict[str, Any]:
    if runs_per_case < 2:
        raise ValueError("runs_per_case must be at least 2 for stability evaluation.")
    cases = load_cases(cases_path)
    adversarial_cases = load_cases(adversarial_path)
    dataset = validate_dataset(cases, adversarial_cases)
    graph = compile_graph(builder)
    model = _create_model()
    workflow_records: list[dict[str, Any]] = []
    total_workflow_usage = UsageCollector()

    for case_number, case in enumerate(cases, start=1):
        for run_index in range(1, runs_per_case + 1):
            usage = UsageCollector()
            thread_id = f"stability-{case['id']}-{run_index}"
            result = run_case(
                case,
                graph,
                callbacks=[usage],
                thread_id=thread_id,
            )
            total_workflow_usage.model_calls += usage.model_calls
            total_workflow_usage.input_tokens += usage.input_tokens
            total_workflow_usage.output_tokens += usage.output_tokens
            total_workflow_usage.reported_cost_usd += usage.reported_cost_usd
            total_workflow_usage.has_reported_cost |= usage.has_reported_cost
            workflow_records.append(
                {
                    "case_id": case["id"],
                    "role": case["role"],
                    "tags": case.get("tags", []),
                    "run_index": run_index,
                    **result.model_dump(mode="json"),
                    "decision_signature": decision_signature(result),
                    "tailored_resume_sha256": (
                        content_hash(graph, thread_id)
                        if result.reached_human_review else None
                    ),
                    "usage": usage_report(usage, model.model_name),
                }
            )
            print(
                f"Workflow {case_number}/20 run {run_index}/{runs_per_case}: "
                f"{case['id']}",
                flush=True,
            )

    reflection_records: list[dict[str, Any]] = []
    total_reflection_usage = UsageCollector()
    for case_number, case in enumerate(adversarial_cases, start=1):
        for run_index in range(1, runs_per_case + 1):
            usage = UsageCollector()
            result = run_adversarial_case(case, callbacks=[usage])
            total_reflection_usage.model_calls += usage.model_calls
            total_reflection_usage.input_tokens += usage.input_tokens
            total_reflection_usage.output_tokens += usage.output_tokens
            total_reflection_usage.reported_cost_usd += usage.reported_cost_usd
            total_reflection_usage.has_reported_cost |= usage.has_reported_cost
            reflection_records.append(
                {
                    "run_index": run_index,
                    **result.model_dump(mode="json"),
                    "outcome_signature": json.dumps(
                        {
                            "detected": result.detected_unsupported_claims,
                            "false_positives": result.supported_claims_incorrectly_rejected,
                            "revision_successful": result.revision_successful,
                            "remaining": result.remaining_unsupported_claims,
                            "revision_count": result.revision_count,
                            "error": result.error,
                        },
                        sort_keys=True,
                    ),
                    "usage": usage_report(usage, model.model_name),
                }
            )
            print(
                f"Adversarial {case_number}/{len(adversarial_cases)} "
                f"run {run_index}/{runs_per_case}: {case['id']}",
                flush=True,
            )

    successful_workflows = [
        item for item in workflow_records if item["reached_human_review"]
    ]
    latencies = [item["latency_seconds"] for item in successful_workflows]
    token_counts = [item["usage"]["total_tokens"] for item in successful_workflows]
    costs = [
        item["usage"]["estimated_cost_usd"] for item in successful_workflows
        if item["usage"]["estimated_cost_usd"] is not None
    ]
    expected = sum(item["expected_missing_requirement_count"] for item in workflow_records)
    canonical_matched = sum(
        item["canonical_matched_requirement_count"] for item in workflow_records
    )
    positive_recalls = [
        item["canonical_missing_requirement_recall"] for item in workflow_records
        if item["canonical_missing_requirement_recall"] is not None
    ]
    injected = sum(item["injected_unsupported_claims"] for item in reflection_records)
    detected = sum(item["detected_unsupported_claims"] for item in reflection_records)
    supported = sum(item["supported_claims"] for item in reflection_records)
    false_positives = sum(
        item["supported_claims_incorrectly_rejected"] for item in reflection_records
    )
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            "model": model.model_name,
            "temperature": model.temperature,
            "prompt_version": "v1",
            "dataset_version": "v2",
            "runs_per_case": runs_per_case,
            "working_tree_dirty_at_run": bool(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
            ),
        },
        "dataset": dataset,
        "workflow_summary": {
            "total_runs": len(workflow_records),
            "runs_reaching_human_review": len(successful_workflows),
            "mean_latency_seconds": mean(latencies) if latencies else 0.0,
            "p50_latency_seconds": median(latencies) if latencies else 0.0,
            "p95_latency_seconds": percentile(latencies, 0.95),
            "mean_tokens_per_workflow": mean(token_counts) if token_counts else 0.0,
            "mean_cost_per_workflow_usd": mean(costs) if costs else None,
            "macro_canonical_recall": mean(positive_recalls) if positive_recalls else 0.0,
            "micro_canonical_recall": canonical_matched / expected if expected else 0.0,
            "forbidden_claims": sum(item["forbidden_claim_count"] for item in successful_workflows),
            "tag_summary": tag_summary(workflow_records),
            "total_usage": usage_report(total_workflow_usage, model.model_name),
            "decision_consistency": consistency_summary(
                workflow_records, "decision_signature", runs_per_case
            ),
            "tailored_resume_exact_consistency": consistency_summary(
                workflow_records, "tailored_resume_sha256", runs_per_case
            ),
        },
        "reflection_summary": {
            "total_runs": len(reflection_records),
            "injected_unsupported_claims_across_runs": injected,
            "detected_unsupported_claims": detected,
            "unsupported_claim_detection_recall": detected / injected if injected else 0.0,
            "supported_control_claims_across_runs": supported,
            "supported_claim_false_positive_rate": false_positives / supported if supported else 0.0,
            "revision_success_rate": sum(item["revision_successful"] for item in reflection_records) / len(reflection_records),
            "remaining_unsupported_claims": sum(item["remaining_unsupported_claims"] for item in reflection_records),
            "mean_revision_count": mean(item["revision_count"] for item in reflection_records),
            "total_usage": usage_report(total_reflection_usage, model.model_name),
            "outcome_consistency": consistency_summary(
                reflection_records, "outcome_signature", runs_per_case
            ),
        },
        "workflow_results": workflow_records,
        "reflection_results": reflection_records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeated stability evaluations.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--adversarial-cases", type=Path, default=DEFAULT_ADVERSARIAL_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--runs-per-case", type=int, default=DEFAULT_RUNS_PER_CASE)
    args = parser.parse_args()
    report = run_stability_evaluation(
        args.cases, args.adversarial_cases, args.output, args.runs_per_case
    )
    workflow = report["workflow_summary"]
    reflection = report["reflection_summary"]
    print(f"Workflow runs reaching human review: {workflow['runs_reaching_human_review']}/{workflow['total_runs']}")
    print(f"Mean/P50/P95 latency: {workflow['mean_latency_seconds']:.2f}s / {workflow['p50_latency_seconds']:.2f}s / {workflow['p95_latency_seconds']:.2f}s")
    print(f"Mean tokens per workflow: {workflow['mean_tokens_per_workflow']:.1f}")
    print(f"Mean cost per workflow: ${workflow['mean_cost_per_workflow_usd']:.6f}")
    print(f"Micro canonical recall: {workflow['micro_canonical_recall']:.1%}")
    print(f"Unsupported-claim detection recall: {reflection['unsupported_claim_detection_recall']:.1%}")
    print(f"Supported-claim false-positive rate: {reflection['supported_claim_false_positive_rate']:.1%}")
    print(f"Revision success rate: {reflection['revision_success_rate']:.1%}")
    print(f"Decision consistency: {workflow['decision_consistency']['all_runs_identical_rate']:.1%}")
    print(f"Exact resume consistency: {workflow['tailored_resume_exact_consistency']['all_runs_identical_rate']:.1%}")
    print(f"Results saved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

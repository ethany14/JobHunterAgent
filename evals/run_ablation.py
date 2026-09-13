"""Compare the writer-only workflow with the full verification workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import dotenv_values
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from evals.metrics import count_forbidden_claims
from evals.run_evals import (
    EVALS_DIR,
    load_cases,
    run_adversarial_case,
    summarize_reflection,
    tailored_resume_text,
)
from job_agent.graph import builder as full_builder
from job_agent.nodes import (
    ENV_PATH,
    _create_model,
    analyze_job,
    analyze_resume,
    match_skills,
    validate_extracted_evidence,
    validate_input,
    write_resume,
)
from job_agent.schemas import TailoredResume
from job_agent.state import JobAgentState

DEFAULT_CASES_PATH = EVALS_DIR / "cases.json"
DEFAULT_ADVERSARIAL_CASES_PATH = EVALS_DIR / "adversarial_cases.json"
DEFAULT_OUTPUT_PATH = EVALS_DIR / "results" / "ablation_v0.1.json"
OFFICIAL_MODEL_PRICING = {
    "gemini-3.7-flash": {
        "input_per_million_usd": 0.75,
        "output_per_million_usd": 3.75,
        "effective_through": "2026-12-31",
        "source": "https://ai.google.dev/gemini-api/docs/pricing",
    }
}


class UsageCollector(BaseCallbackHandler):
    """Collect call and token counts exposed by OpenAI-compatible providers."""

    def __init__(self) -> None:
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.reported_cost_usd = 0.0
        self.has_reported_cost = False

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.model_calls += 1
        for generations in response.generations:
            for generation in generations[:1]:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                self.input_tokens += int(usage.get("input_tokens", 0) or 0)
                self.output_tokens += int(usage.get("output_tokens", 0) or 0)
                metadata = getattr(message, "response_metadata", None) or {}
                for key in ("cost", "cost_usd", "estimated_cost"):
                    if isinstance(metadata.get(key), (int, float)):
                        self.reported_cost_usd += float(metadata[key])
                        self.has_reported_cost = True
                        break

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def human_review_without_verifier(state: JobAgentState) -> dict:
    decision = interrupt(
        {
            "question": "Do you approve this tailored resume?",
            "tailored_resume": state["tailored_resume"],
            "verification": None,
            "revision_count": state["revision_count"],
        }
    )
    return {"approved": bool(decision.get("approved"))}


def build_writer_only_graph() -> StateGraph:
    graph_builder = StateGraph(JobAgentState)
    graph_builder.add_node("validate_input", validate_input)
    graph_builder.add_node("analyze_resume", analyze_resume)
    graph_builder.add_node("validate_extracted_evidence", validate_extracted_evidence)
    graph_builder.add_node("analyze_job", analyze_job)
    graph_builder.add_node("match_skills", match_skills)
    graph_builder.add_node("write_resume", write_resume)
    graph_builder.add_node("human_review", human_review_without_verifier)
    graph_builder.add_edge(START, "validate_input")
    graph_builder.add_edge("validate_input", "analyze_resume")
    graph_builder.add_edge("analyze_resume", "validate_extracted_evidence")
    graph_builder.add_edge("validate_extracted_evidence", "analyze_job")
    graph_builder.add_edge("analyze_job", "match_skills")
    graph_builder.add_edge("match_skills", "write_resume")
    graph_builder.add_edge("write_resume", "human_review")
    graph_builder.add_edge("human_review", END)
    return graph_builder


def compile_graph(graph_builder: StateGraph) -> Any:
    return graph_builder.compile(
        checkpointer=InMemorySaver(
            serde=JsonPlusSerializer(allowed_msgpack_modules=None)
        )
    )


def run_workflow_variant(
    name: str, graph: Any, cases: list[dict[str, Any]], usage: UsageCollector
) -> dict[str, Any]:
    results = []
    for case in cases:
        started = perf_counter()
        expected_error = case.get("expected_error")
        config = {
            "configurable": {"thread_id": f"ablation-{name}-{case['id']}"},
            "callbacks": [usage],
        }
        try:
            result = graph.invoke(
                {
                    "resume_text": case["resume_text"],
                    "job_description": case["job_description"],
                },
                config=config,
            )
            snapshot = graph.get_state(config)
            reached_review = bool(result.get("__interrupt__")) and snapshot.next == (
                "human_review",
            )
            resume = TailoredResume.model_validate(snapshot.values["tailored_resume"])
            results.append(
                {
                    "case_id": case["id"],
                    "case_type": "valid_workflow",
                    "expected_behavior_achieved": reached_review,
                    "reached_human_review": reached_review,
                    "forbidden_claim_count": count_forbidden_claims(
                        tailored_resume_text(resume), case.get("forbidden_claims", [])
                    ),
                    "latency_seconds": round(perf_counter() - started, 3),
                    "error": None,
                }
            )
        except Exception as exc:
            message = str(exc)
            passed = bool(expected_error and expected_error in message)
            results.append(
                {
                    "case_id": case["id"],
                    "case_type": "input_validation" if expected_error else "valid_workflow",
                    "expected_behavior_achieved": passed,
                    "reached_human_review": False,
                    "forbidden_claim_count": 0,
                    "latency_seconds": round(perf_counter() - started, 3),
                    "error": None if passed else message,
                }
            )
    valid = [item for item in results if item["case_type"] == "valid_workflow"]
    return {
        "results": results,
        "expected_behavior_achieved": sum(item["expected_behavior_achieved"] for item in results),
        "valid_workflows_reaching_human_review": sum(item["reached_human_review"] for item in valid),
        "workflow_forbidden_claims": sum(item["forbidden_claim_count"] for item in valid),
        "average_valid_workflow_latency_seconds": sum(item["latency_seconds"] for item in valid) / len(valid),
    }


def combine_usage(*collectors: UsageCollector) -> UsageCollector:
    combined = UsageCollector()
    combined.model_calls = sum(item.model_calls for item in collectors)
    combined.input_tokens = sum(item.input_tokens for item in collectors)
    combined.output_tokens = sum(item.output_tokens for item in collectors)
    combined.reported_cost_usd = sum(item.reported_cost_usd for item in collectors)
    combined.has_reported_cost = any(item.has_reported_cost for item in collectors)
    return combined


def usage_report(usage: UsageCollector, model_name: str) -> dict[str, Any]:
    settings = {**dotenv_values(ENV_PATH), **os.environ}
    input_rate = settings.get("LLM_INPUT_COST_PER_MILLION")
    output_rate = settings.get("LLM_OUTPUT_COST_PER_MILLION")
    if usage.has_reported_cost:
        cost = usage.reported_cost_usd
        basis = "provider_reported"
    elif input_rate and output_rate:
        cost = (
            usage.input_tokens * float(input_rate)
            + usage.output_tokens * float(output_rate)
        ) / 1_000_000
        basis = "configured_rates"
        pricing = None
    elif model_name in OFFICIAL_MODEL_PRICING:
        pricing = OFFICIAL_MODEL_PRICING[model_name]
        cost = (
            usage.input_tokens * pricing["input_per_million_usd"]
            + usage.output_tokens * pricing["output_per_million_usd"]
        ) / 1_000_000
        basis = "official_paid_tier_list_price"
    else:
        cost = None
        basis = "unavailable_without_provider_cost_or_configured_rates"
        pricing = None
    if usage.has_reported_cost:
        pricing = None
    return {
        "model_calls": usage.model_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "estimated_cost_usd": round(cost, 8) if cost is not None else None,
        "cost_basis": basis,
        "pricing": pricing,
    }


def build_comparison(writer: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    def change(new: float, old: float) -> float | None:
        return (new - old) / old if old else None

    writer_workflow = writer["workflow_usage"]
    full_workflow = full["workflow_usage"]
    writer_total = writer["total_usage"]
    full_total = full["total_usage"]
    unsupported_before = writer["total_evaluated_unsupported_claims"]
    unsupported_after = full["total_evaluated_unsupported_claims"]
    return {
        "unsupported_claim_reduction": unsupported_before - unsupported_after,
        "unsupported_claim_reduction_rate": (
            (unsupported_before - unsupported_after) / unsupported_before
            if unsupported_before else 0.0
        ),
        "workflow_latency_overhead_seconds": round(
            full["average_valid_workflow_latency_seconds"]
            - writer["average_valid_workflow_latency_seconds"], 4
        ),
        "workflow_latency_overhead_rate": round(
            change(
                full["average_valid_workflow_latency_seconds"],
                writer["average_valid_workflow_latency_seconds"],
            ) or 0.0,
            6,
        ),
        "workflow_model_call_overhead": (
            full_workflow["model_calls"] - writer_workflow["model_calls"]
        ),
        "workflow_token_overhead": (
            full_workflow["total_tokens"] - writer_workflow["total_tokens"]
        ),
        "workflow_estimated_cost_overhead_usd": round(
            full_workflow["estimated_cost_usd"]
            - writer_workflow["estimated_cost_usd"], 8
        ),
        "total_suite_model_call_overhead": (
            full_total["model_calls"] - writer_total["model_calls"]
        ),
        "total_suite_token_overhead": (
            full_total["total_tokens"] - writer_total["total_tokens"]
        ),
        "total_suite_estimated_cost_overhead_usd": round(
            full_total["estimated_cost_usd"] - writer_total["estimated_cost_usd"], 8
        ),
    }


def run_ablation(cases_path: Path, adversarial_path: Path, output_path: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    adversarial_cases = load_cases(adversarial_path)
    writer_usage = UsageCollector()
    full_workflow_usage = UsageCollector()
    full_reflection_usage = UsageCollector()
    model = _create_model()
    model_name = model.model_name
    writer = run_workflow_variant(
        "writer-only", compile_graph(build_writer_only_graph()), cases, writer_usage
    )
    full = run_workflow_variant(
        "full-agent", compile_graph(full_builder), cases, full_workflow_usage
    )
    reflection_results = [
        run_adversarial_case(case, callbacks=[full_reflection_usage])
        for case in adversarial_cases
    ]
    reflection = summarize_reflection(reflection_results)
    injected = sum(len(case["injected_claims"]) for case in adversarial_cases)
    writer.update(
        {
            "adversarial_remaining_unsupported_claims": injected,
            "total_evaluated_unsupported_claims": writer["workflow_forbidden_claims"] + injected,
            "unsupported_claim_detection_recall": None,
            "revision_success_rate": None,
            "workflow_usage": usage_report(writer_usage, model_name),
            "adversarial_usage": usage_report(UsageCollector(), model_name),
            "total_usage": usage_report(writer_usage, model_name),
        }
    )
    full.update(
        {
            "adversarial_remaining_unsupported_claims": reflection["remaining_unsupported_claims"],
            "total_evaluated_unsupported_claims": full["workflow_forbidden_claims"] + reflection["remaining_unsupported_claims"],
            "unsupported_claim_detection_recall": reflection["unsupported_claim_detection_recall"],
            "revision_success_rate": reflection["revision_success_rate"],
            "workflow_usage": usage_report(full_workflow_usage, model_name),
            "adversarial_usage": usage_report(full_reflection_usage, model_name),
            "total_usage": usage_report(
                combine_usage(full_workflow_usage, full_reflection_usage), model_name
            ),
            "reflection_results": [item.model_dump(mode="json") for item in reflection_results],
        }
    )
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip(),
            "model": model.model_name,
            "temperature": model.temperature,
            "prompt_version": "v1",
            "dataset_version": "v1",
            "runs_per_case": 1,
        },
        "version_a_writer_only": writer,
        "version_b_full_agent": full,
        "comparison": build_comparison(writer, full),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run verifier ablation evaluation.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--adversarial-cases", type=Path, default=DEFAULT_ADVERSARIAL_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    report = run_ablation(args.cases, args.adversarial_cases, args.output)
    for label, key in (
        ("Version A - writer only", "version_a_writer_only"),
        ("Version B - full agent", "version_b_full_agent"),
    ):
        result = report[key]
        print(label)
        print(f"  Unsupported claims remaining: {result['total_evaluated_unsupported_claims']}")
        print(f"  Average valid-workflow latency: {result['average_valid_workflow_latency_seconds']:.2f}s")
        print(f"  Workflow model calls: {result['workflow_usage']['model_calls']}")
        print(f"  Total model calls: {result['total_usage']['model_calls']}")
        print(f"  Total tokens: {result['total_usage']['total_tokens']}")
        print(f"  Estimated cost: ${result['total_usage']['estimated_cost_usd']:.6f}")
    print(f"Results saved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

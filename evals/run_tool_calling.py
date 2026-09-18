"""Optional live evaluation of provider tool selection and arguments."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from time import perf_counter

from agent_runtime import AgentMessage, LangChainToolModelAdapter, ToolContext, ToolRegistry
from agent_runtime.tools import register_builtin_job_agent_tools
from job_agent.model import create_model

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "tool_calling_cases.json"


class EmptyRunReader:
    def list_recent_runs(self, limit):
        return []

    def get_run(self, run_id):
        return None


def _arguments_match(expected: list[dict], actual: list[dict]) -> bool:
    return expected == actual


def run(*, live: bool) -> dict:
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dataset_version": "tool_calling_v1",
        "live_evaluation": live,
        "results": [],
    }
    if not live:
        report["metrics"] = None
        return report

    registry = register_builtin_job_agent_tools(ToolRegistry(), EmptyRunReader())
    model = create_model()
    adapter = LangChainToolModelAdapter(model)
    totals = {"input_tokens": 0, "output_tokens": 0}
    for case in cases:
        context = ToolContext(
            task_id=f"tool-eval-{case['id']}",
            allowed_tools=frozenset(case["allowed_tools"]),
        )
        started = perf_counter()
        error = None
        selected: list[str] = []
        arguments: list[dict] = []
        input_tokens = output_tokens = 0
        try:
            response = adapter.invoke(
                [
                    AgentMessage(
                        message_id=f"system-{case['id']}",
                        role="system",
                        content=(
                            "Use only the supplied tools. Call a tool only when the "
                            "request needs local run data."
                        ),
                    ),
                    AgentMessage(
                        message_id=f"user-{case['id']}",
                        role="user",
                        content=case["user_message"],
                    ),
                ],
                registry.model_schemas(context),
            )
            selected = [item.tool_name for item in response.message.tool_calls]
            arguments = [item.arguments for item in response.message.tool_calls]
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
        except Exception:
            error = "The configured endpoint did not complete the tool-calling case."
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        report["results"].append(
            {
                "case_id": case["id"],
                "selected_tools": selected,
                "arguments": arguments,
                "tool_selection_correct": selected == case["expected_tools"],
                "arguments_correct": _arguments_match(
                    case["expected_arguments"], arguments
                ),
                "unnecessary_tool_call": bool(selected and not case["expected_tools"]),
                "unauthorized_tool_calls": sum(
                    item not in case["allowed_tools"] for item in selected
                ),
                "completed": error is None,
                "iterations": 1,
                "tool_calls": len(selected),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_seconds": round(perf_counter() - started, 3),
                "error": error,
            }
        )

    results = report["results"]
    report["metrics"] = {
        "tool_selection_accuracy": mean(
            item["tool_selection_correct"] for item in results
        ),
        "argument_accuracy": mean(item["arguments_correct"] for item in results),
        "unnecessary_tool_call_rate": mean(
            item["unnecessary_tool_call"] for item in results
        ),
        "unauthorized_tool_call_count": sum(
            item["unauthorized_tool_calls"] for item in results
        ),
        "completion_rate": mean(item["completed"] for item in results),
        "average_iterations": mean(item["iterations"] for item in results),
        "average_tool_calls": mean(item["tool_calls"] for item in results),
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "average_latency_seconds": mean(
            item["latency_seconds"] for item in results
        ),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(live=args.live)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

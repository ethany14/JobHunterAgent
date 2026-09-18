"""Optional real-model smoke evaluation for provider tool selection.

No network/model call is made unless --smoke-test is supplied.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from agent_runtime import AgentMessage, LangChainToolModelAdapter, ToolContext, ToolRegistry
from agent_runtime.tools import register_builtin_job_agent_tools
from job_agent.model import create_model

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "tool_selection_v1.json"

class EmptyRunReader:
    def list_recent_runs(self, limit): return []
    def get_run(self, run_id): return None

def run(smoke_test: bool) -> dict:
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    registry = register_builtin_job_agent_tools(ToolRegistry(), EmptyRunReader())
    report = {"timestamp": datetime.now(UTC).isoformat(), "dataset_version": "tool_selection_v1",
        "smoke_test_requested": smoke_test, "adapter": "LangChainToolModelAdapter",
        "bind_tools_available": None, "results": []}
    if not smoke_test:
        return report
    model = create_model()
    report["bind_tools_available"] = callable(getattr(model, "bind_tools", None))
    adapter = LangChainToolModelAdapter(model)
    for case in cases:
        context = ToolContext(task_id=f"selection-{case['id']}",
            allowed_tools=frozenset(case["allowed_tools"]))
        try:
            response = adapter.invoke([AgentMessage(role="system",
                content="Select tools only when needed. Do not invent tool arguments."),
                AgentMessage(role="user", content=case["user_message"])],
                registry.model_schemas(context))
            selected = [item.tool_name for item in response.message.tool_calls]
            report["results"].append({"id": case["id"], "selected_tools": selected,
                "expected_tools": case["expected_tools"],
                "passed": selected == case["expected_tools"], "error": None})
        except Exception:
            report["results"].append({"id": case["id"], "selected_tools": [],
                "expected_tools": case["expected_tools"], "passed": False,
                "error": "The configured endpoint did not complete the tool-selection smoke test."})
    return report

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.smoke_test)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output: args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

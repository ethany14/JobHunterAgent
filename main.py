"""Command-line entry point for the job matching agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Sequence

from langgraph.types import Command
from job_agent.agent import graph
from job_agent.results import interrupt_payload, public_result


def _read_text(path: Path, label: str) -> str:
    """Read a UTF-8 input file and reject empty content early."""
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"{label} file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read {label} file '{path}': {exc}") from exc

    if not content.strip():
        raise ValueError(f"{label} file is empty: {path}")
    return content


def analyze_files(
    resume_path: Path,
    job_path: Path,
    thread_id: str,
    reviewer: Callable[[dict], dict] | None = None,
) -> dict:
    """Run or pause the agent, optionally handling reviews in the same process."""
    if not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string.")
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        {
            "resume_text": _read_text(resume_path, "Resume"),
            "job_description": _read_text(job_path, "Job description"),
        },
        config=config,
    )
    payload = interrupt_payload(result)
    while payload is not None and reviewer is not None:
        result = graph.invoke(Command(resume=reviewer(payload)), config=config)
        payload = interrupt_payload(result)
    return public_result(result)


def _console_review(payload: dict) -> dict:
    print("\nHuman review required:\n")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    while True:
        answer = input("\nApprove this tailored resume? [y/n]: ").strip().casefold()
        if answer in {"y", "yes"}:
            return {"approved": True, "feedback": None}
        if answer in {"n", "no"}:
            feedback = input("Revision feedback: ").strip()
            if feedback:
                return {"approved": False, "feedback": feedback}
            print("Feedback is required when rejecting the resume.")
            continue
        print("Enter y or n.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a resume with a job description using the job agent."
    )
    parser.add_argument("resume", type=Path, help="Path to a UTF-8 resume text file.")
    parser.add_argument(
        "job", type=Path, help="Path to a UTF-8 job-description text file."
    )
    parser.add_argument(
        "--thread-id",
        required=True,
        help="Unique checkpoint thread ID, for example application-001.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write the JSON result to this file instead of standard output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = analyze_files(
            args.resume,
            args.job,
            thread_id=args.thread_id,
            reviewer=_console_review,
        )
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
            print(f"Analysis written to {args.output}")
        else:
            print(rendered, end="")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

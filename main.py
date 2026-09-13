"""Command-line entry point for the job matching agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from job_agent.agent import graph
from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    SkillMatch,
    TailoredResume,
    VerificationResult,
)


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


def _public_result(state: dict) -> dict:
    """Validate the graph result and omit the raw resume and job text."""
    required_outputs = {
        "resume_analysis": ResumeAnalysis,
        "job_analysis": JobAnalysis,
        "skill_match": SkillMatch,
    }
    output = {}
    for field, schema in required_outputs.items():
        if field not in state:
            raise RuntimeError(f"Graph completed without producing '{field}'.")
        output[field] = schema.model_validate(state[field]).model_dump(mode="json")
    if "tailored_resume" not in state or state.get("verification") is None:
        raise RuntimeError("Graph completed without generating and verifying a resume.")
    output.update(
        {
            "tailored_resume": TailoredResume.model_validate(
                state["tailored_resume"]
            ).model_dump(mode="json"),
            "verification": VerificationResult.model_validate(
                state["verification"]
            ).model_dump(mode="json"),
            "revision_feedback": state.get("revision_feedback", []),
            "revision_count": state.get("revision_count", 0),
            "max_revisions": state.get("max_revisions", 3),
        }
    )
    return output


def analyze_files(resume_path: Path, job_path: Path) -> dict:
    """Run the agent using a resume file and a job-description file."""
    result = graph.invoke(
        {
            "resume_text": _read_text(resume_path, "Resume"),
            "job_description": _read_text(job_path, "Job description"),
        }
    )
    return _public_result(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a resume with a job description using the job agent."
    )
    parser.add_argument("resume", type=Path, help="Path to a UTF-8 resume text file.")
    parser.add_argument(
        "job", type=Path, help="Path to a UTF-8 job-description text file."
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
        result = analyze_files(args.resume, args.job)
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

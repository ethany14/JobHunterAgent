"""Validate local human-review packets without calling a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ALLOWED_FAILURE_REASONS = {
    "unsupported_claim",
    "invented_metadata",
    "within_entry_duplicate",
    "cross_entry_fact_removed",
    "poor_targeting",
    "not_application_ready",
    "other",
}


def _load(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list):
        raise ValueError(f"{path.name} must contain a JSON array.")
    return value


def validate_cases(cases: list[dict[str, Any]]) -> None:
    if not 10 <= len(cases) <= 20:
        raise ValueError("Human review requires between 10 and 20 cases.")
    identifiers: set[str] = set()
    required_text = ("resume_text", "job_description", "generated_artifact")
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("case_id") or "").strip()
        if not case_id or case_id in identifiers:
            raise ValueError(f"Case {index} has a blank or duplicate case_id.")
        identifiers.add(case_id)
        if case.get("source_kind") != "anonymized_real":
            raise ValueError(f"{case_id} is not declared as anonymized real material.")
        if case.get("consent_confirmed") is not True:
            raise ValueError(f"{case_id} is missing confirmed permission for evaluation.")
        for field in required_text:
            if not str(case.get(field) or "").strip():
                raise ValueError(f"{case_id} has no {field}.")


def validate_reviews(
    cases: list[dict[str, Any]], reviews: list[dict[str, Any]],
) -> None:
    case_ids = {case["case_id"] for case in cases}
    reviewed_ids: set[str] = set()
    for review in reviews:
        case_id = str(review.get("case_id") or "").strip()
        if case_id not in case_ids:
            raise ValueError(f"Review references unknown case {case_id!r}.")
        reviewed_ids.add(case_id)
        scores = [review.get(name) for name in (
            "factual_accuracy", "non_duplication", "application_readiness"
        )]
        if any(not isinstance(score, int) or not 1 <= score <= 5 for score in scores):
            raise ValueError(f"{case_id} review scores must be integers from 1 to 5.")
        failed = any(score < 4 for score in scores)
        reasons = review.get("failure_reasons") or []
        if not isinstance(reasons, list) or any(
            reason not in ALLOWED_FAILURE_REASONS for reason in reasons
        ):
            raise ValueError(f"{case_id} contains an invalid failure reason.")
        if failed and not reasons:
            raise ValueError(f"{case_id} failed review without a failure reason.")
        if failed and not str(review.get("regression_test") or "").strip():
            raise ValueError(f"{case_id} failed review without a regression test.")
    missing = case_ids - reviewed_ids
    if missing:
        raise ValueError(f"Cases without reviews: {', '.join(sorted(missing))}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    cases = _load(args.cases)
    validate_cases(cases)
    if args.reviews:
        validate_reviews(cases, _load(args.reviews))
    print(f"Validated {len(cases)} anonymized real-material cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

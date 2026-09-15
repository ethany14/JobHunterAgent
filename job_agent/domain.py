"""Deterministic business rules shared by agent backends."""

from __future__ import annotations

import hashlib
import re

from job_agent.schemas import SkillEvidence


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def make_evidence_id(text: str) -> str:
    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:8]
    return f"EXP-{digest}"


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def extract_minimum_years(text: str) -> int | None:
    match = re.search(
        r"\b(?:at\s+least\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s*\+?\s+years?\b",
        normalize_text(text),
    )
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else _NUMBER_WORDS[value]


def canonicalize_requirement(name: str, original_text: str) -> str:
    normalized = normalize_text(name).replace("ci / cd", "ci/cd")
    combined = f"{normalized} {normalize_text(original_text)}"
    if "leadership" in combined:
        return "leadership_experience"
    if "kubernetes" in combined:
        return "kubernetes"
    if re.search(r"\baws\b", combined):
        return "aws"
    if "ci/cd" in combined or "continuous integration" in combined:
        return "ci/cd"
    if "postgres" in combined:
        return "postgresql"
    if re.search(r"\bsql\b", combined):
        return "sql"
    if re.search(r"\bpython\b", combined):
        return "python"
    return normalized.replace(" ", "_")


def calculate_match_score(matches: list[SkillEvidence]) -> float:
    weights = {"required": 2.0, "preferred": 1.0}
    values = {"matched": 1.0, "partial": 0.5, "missing": 0.0}
    possible = sum(weights[item.requirement_level] for item in matches)
    if not possible:
        return 0.0
    earned = sum(
        weights[item.requirement_level] * values[item.match_status]
        for item in matches
    )
    return round(earned / possible * 100, 1)

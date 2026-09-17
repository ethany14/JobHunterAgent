"""Deterministic business rules shared by agent backends."""

from __future__ import annotations

import hashlib
import re

from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    MissingRequirement,
    RequirementCategory,
    ResumeAnalysis,
    ScoreBreakdown,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
    VerificationMode,
)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalize_evidence_text(text: str) -> str:
    """Normalize formatting punctuation while preserving meaningful skill symbols."""
    return re.sub(r"[^\w+#/%$]+", " ", normalize_text(text)).strip()


def make_evidence_id(text: str) -> str:
    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:8]
    return f"EXP-{digest}"


def _stable_id(prefix: str, *parts: str) -> str:
    normalized = "\x1f".join(normalize_text(part) for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def make_requirement_group_id(source_text: str) -> str:
    return _stable_id("REQG", source_text)


def make_requirement_id(requirement_group_id: str, atomic_text: str) -> str:
    return _stable_id("REQ", requirement_group_id, atomic_text)


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


_ELIGIBILITY_PHRASES = (
    "authorized to work",
    "authorised to work",
    "work authorization",
    "work authorisation",
    "visa sponsorship",
    "require sponsorship",
    "citizenship",
    "security clearance",
    "background check",
    "drug test",
    "willing to relocate",
    "willingness to relocate",
    "willing to travel",
    "driver's license",
    "driving licence",
)


def is_eligibility_requirement(requirement: JobRequirement) -> bool:
    if requirement.category == RequirementCategory.ELIGIBILITY:
        return True
    searchable = normalize_text(
        " ".join(
            (
                requirement.canonical_name.replace("_", " "),
                requirement.display_name,
                requirement.atomic_text,
                requirement.source_text,
            )
        )
    )
    return any(phrase in searchable for phrase in _ELIGIBILITY_PHRASES)


def normalize_job_analysis(
    analysis: JobAnalysis,
    original_job_description: str,
) -> JobAnalysis:
    """Ground, identify, normalize, and deduplicate extracted requirements."""
    original = normalize_text(original_job_description)
    invalid_sources = [
        item.requirement_id
        for item in analysis.requirements
        if normalize_text(item.source_text) not in original
    ]
    if invalid_sources:
        raise ValueError(
            "Requirement source_text is not present in the original job "
            f"description: {invalid_sources}"
        )

    requirements_by_name: dict[str, JobRequirement] = {}
    for item in analysis.requirements:
        canonical_name = canonicalize_requirement(
            item.canonical_name,
            item.atomic_text,
        )
        if not canonical_name:
            continue

        group_id = make_requirement_group_id(item.source_text)
        minimum_years = extract_minimum_years(item.atomic_text)
        if minimum_years is None:
            minimum_years = extract_minimum_years(item.source_text)

        normalized = JobRequirement.model_validate(
            {
                **item.model_dump(mode="python"),
                "requirement_id": make_requirement_id(group_id, item.atomic_text),
                "requirement_group_id": group_id,
                "canonical_name": canonical_name,
                "original_text": item.atomic_text,
                "minimum_years": minimum_years,
            }
        )
        if is_eligibility_requirement(normalized):
            normalized = JobRequirement.model_validate(
                {
                    **normalized.model_dump(mode="python"),
                    "verification_mode": VerificationMode.USER_CONFIRMATION,
                }
            )

        existing = requirements_by_name.get(canonical_name)
        if existing is None or (
            existing.level == "preferred" and normalized.level == "required"
        ):
            requirements_by_name[canonical_name] = normalized

    return JobAnalysis.model_validate(
        {
            **analysis.model_dump(mode="python"),
            "requirements": list(requirements_by_name.values()),
        }
    )


_IN_PROGRESS_EDUCATION_PHRASES = (
    "currently pursuing",
    "in progress",
    "expected graduation",
    "graduation expected",
    "anticipated graduation",
    "candidate for",
    "currently enrolled",
)


def _requires_explicit_tool_evidence(requirement: JobRequirement) -> str | None:
    target = normalize_text(
        " ".join(
            (
                requirement.canonical_name.replace("_", " "),
                requirement.display_name,
                requirement.atomic_text,
            )
        )
    )
    if re.search(r"\bpower\s*bi\b", target):
        return "power bi"
    if "claude code" in target:
        return "claude code"
    if re.search(r"\bclaude\b", target):
        return "claude"
    return None


def _evidence_mentions_tool(evidence: list[str], tool: str) -> bool:
    combined = " ".join(normalize_text(item) for item in evidence)
    if tool == "power bi":
        return re.search(r"\bpower\s*bi\b", combined) is not None
    return tool in combined


def _missing_requirement(requirement: JobRequirement) -> MissingRequirement:
    return MissingRequirement(
        canonical_name=requirement.canonical_name,
        original_text=requirement.original_text,
        minimum_years=requirement.minimum_years,
    )


def normalize_skill_match(
    resume: ResumeAnalysis,
    job: JobAnalysis,
    assessment: SkillAssessment,
) -> SkillMatch:
    """Apply deterministic grounding rules to a model-produced skill assessment."""
    allowed_by_normalized = {
        normalize_evidence_text(item.exact_text): item.exact_text
        for item in resume.evidence
    }
    supplied_by_id = {item.requirement_id: item for item in assessment.matches}
    supplied_by_name = {
        normalize_text(item.job_skill): item for item in assessment.matches
    }
    matches: list[SkillEvidence] = []

    for requirement in job.requirements:
        item = supplied_by_id.get(requirement.requirement_id)
        if item is None:
            item = supplied_by_name.get(normalize_text(requirement.canonical_name))
        if item is None:
            item = SkillEvidence(
                requirement_id=requirement.requirement_id,
                job_skill=requirement.canonical_name,
                requirement_level=requirement.level,
                match_status="missing",
                match_reason="No matching assessment or resume evidence was supplied.",
                resume_evidence=[],
                confidence=1,
            )

        valid_evidence: list[str] = []
        for evidence in item.resume_evidence:
            exact_text = allowed_by_normalized.get(normalize_evidence_text(evidence))
            if exact_text is not None and exact_text not in valid_evidence:
                valid_evidence.append(exact_text)

        status = item.match_status
        reason = item.match_reason
        confidence = item.confidence
        supported_years = item.supported_years

        if requirement.verification_mode == VerificationMode.USER_CONFIRMATION:
            status = "needs_confirmation"
            reason = "This requirement must be confirmed by the user."
            supported_years = None
        else:
            tool = _requires_explicit_tool_evidence(requirement)
            if tool is not None and not _evidence_mentions_tool(valid_evidence, tool):
                valid_evidence = []
                status = "missing"
                reason = f"The resume evidence does not explicitly mention {tool}."
                confidence = 0
                supported_years = None

            if status in {"matched", "partial"} and not valid_evidence:
                status = "missing"
                reason = "No valid resume evidence supports this requirement."
                confidence = 0
                supported_years = None

            if (
                status == "matched"
                and requirement.category == RequirementCategory.EDUCATION
                and any(
                    phrase in normalize_text(evidence)
                    for evidence in valid_evidence
                    for phrase in _IN_PROGRESS_EDUCATION_PHRASES
                )
            ):
                status = "partial"
                reason = "The cited education is still in progress."

            if requirement.minimum_years is not None and valid_evidence:
                explicit_years = [
                    years
                    for evidence in valid_evidence
                    if (years := extract_minimum_years(evidence)) is not None
                ]
                supported_years = max(explicit_years) if explicit_years else None
                if status == "matched" and (
                    supported_years is None
                    or supported_years < requirement.minimum_years
                ):
                    status = "partial"
                    reason = (
                        "The resume evidence does not explicitly support the required "
                        f"minimum of {requirement.minimum_years} years."
                    )

        matches.append(
            SkillEvidence.model_validate(
                {
                    **item.model_dump(mode="python"),
                    "requirement_id": requirement.requirement_id,
                    "job_skill": requirement.canonical_name,
                    "requirement_level": requirement.level,
                    "match_status": status,
                    "match_reason": reason,
                    "supported_years": supported_years,
                    "resume_evidence": valid_evidence,
                    "confidence": confidence,
                }
            )
        )

    requirements_by_id = {item.requirement_id: item for item in job.requirements}
    missing_required = [
        _missing_requirement(requirements_by_id[item.requirement_id])
        for item in matches
        if item.requirement_level == "required"
        and item.match_status in {"missing", "partial"}
    ]
    missing_preferred = [
        _missing_requirement(requirements_by_id[item.requirement_id])
        for item in matches
        if item.requirement_level == "preferred"
        and item.match_status in {"missing", "partial"}
    ]
    confirmation_requirements = [
        requirements_by_id[item.requirement_id]
        for item in matches
        if item.match_status == "needs_confirmation"
    ]
    score_breakdown = calculate_score_breakdown(matches, job.requirements)
    return SkillMatch(
        **assessment.model_dump(exclude={"matches"}),
        matches=matches,
        missing_required_requirements=missing_required,
        missing_preferred_requirements=missing_preferred,
        confirmation_requirements=confirmation_requirements,
        overall_score=score_breakdown.overall_score,
        score_breakdown=score_breakdown,
    )


def calculate_score_breakdown(
    matches: list[SkillEvidence],
    requirements: list[JobRequirement] | None = None,
) -> ScoreBreakdown:
    """Score requirement groups without rewarding compound-requirement splitting."""
    weights = {"required": 2.0, "preferred": 1.0}
    values = {"matched": 1.0, "partial": 0.5, "missing": 0.0}
    requirements_by_id = {
        item.requirement_id: item for item in requirements or []
    }
    grouped_values: dict[str, list[float]] = {}
    grouped_levels: dict[str, set[str]] = {}

    for item in matches:
        if item.match_status == "needs_confirmation":
            continue
        requirement = requirements_by_id.get(item.requirement_id)
        group_id = (
            requirement.requirement_group_id
            if requirement is not None
            else item.requirement_id
        )
        level = requirement.level if requirement is not None else item.requirement_level
        grouped_values.setdefault(group_id, []).append(values[item.match_status])
        grouped_levels.setdefault(group_id, set()).add(level)

    required_group_values: list[float] = []
    preferred_group_values: list[float] = []
    weighted_earned = 0.0
    weighted_possible = 0.0
    for group_id, atomic_values in grouped_values.items():
        group_value = sum(atomic_values) / len(atomic_values)
        group_level = (
            "required"
            if "required" in grouped_levels[group_id]
            else "preferred"
        )
        if group_level == "required":
            required_group_values.append(group_value)
        else:
            preferred_group_values.append(group_value)
        weight = weights[group_level]
        weighted_earned += weight * group_value
        weighted_possible += weight

    def percentage(group_values: list[float]) -> float | None:
        if not group_values:
            return None
        return round(sum(group_values) / len(group_values) * 100, 1)

    overall = (
        round(weighted_earned / weighted_possible * 100, 1)
        if weighted_possible
        else 0.0
    )
    return ScoreBreakdown(
        required_score=percentage(required_group_values),
        preferred_score=percentage(preferred_group_values),
        overall_score=overall,
    )


def calculate_match_score(
    matches: list[SkillEvidence],
    requirements: list[JobRequirement] | None = None,
) -> float:
    """Compatibility wrapper returning the grouped overall score."""
    return calculate_score_breakdown(matches, requirements).overall_score

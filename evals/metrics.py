"""Deterministic metrics for job-agent quality evaluations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MissingRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    original_text: str | None = None
    minimum_years: int | None = Field(default=None, ge=0)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    case_type: Literal["valid_workflow", "input_validation"]
    expected_behavior_achieved: bool
    reached_human_review: bool
    input_validation_passed: bool | None
    strict_missing_requirement_recall: float | None = Field(default=None, ge=0, le=1)
    canonical_missing_requirement_recall: float | None = Field(default=None, ge=0, le=1)
    expected_missing_requirement_count: int = Field(ge=0)
    strict_matched_requirement_count: int = Field(ge=0)
    canonical_matched_requirement_count: int = Field(ge=0)
    actual_missing_requirements: list[MissingRequirement]
    forbidden_claim_count: int = Field(ge=0)
    verification_passed: bool | None
    revision_count: int = Field(ge=0)
    latency_seconds: float = Field(ge=0)
    error: str | None


class ReflectionEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    injected_unsupported_claims: int = Field(ge=0)
    detected_unsupported_claims: int = Field(ge=0)
    supported_claims: int = Field(ge=0)
    supported_claims_incorrectly_rejected: int = Field(ge=0)
    initial_verification_passed: bool
    revision_successful: bool
    remaining_unsupported_claims: int = Field(ge=0)
    revision_count: int = Field(ge=0)
    latency_seconds: float = Field(ge=0)
    error: str | None


def calculate_recall(expected: list[str], actual: list[str]) -> float | None:
    expected_set = {item.casefold() for item in expected}
    if not expected_set:
        return None
    actual_set = {item.casefold() for item in actual}
    return len(expected_set & actual_set) / len(expected_set)


def count_strict_matches(expected: list[str], actual: list[str]) -> int:
    expected_set = {item.casefold() for item in expected}
    actual_set = {item.casefold() for item in actual}
    return len(expected_set & actual_set)


def requirement_key(requirement: MissingRequirement) -> tuple[str, int | None]:
    return (requirement.canonical_name.casefold(), requirement.minimum_years)


def calculate_requirement_recall(
    expected: list[MissingRequirement], actual: list[MissingRequirement]
) -> float | None:
    expected_keys = {requirement_key(item) for item in expected}
    if not expected_keys:
        return None
    actual_keys = {requirement_key(item) for item in actual}
    return len(expected_keys & actual_keys) / len(expected_keys)


def count_requirement_matches(
    expected: list[MissingRequirement], actual: list[MissingRequirement]
) -> int:
    expected_keys = {requirement_key(item) for item in expected}
    actual_keys = {requirement_key(item) for item in actual}
    return len(expected_keys & actual_keys)


def count_forbidden_claims(generated_text: str, forbidden_claims: list[str]) -> int:
    text = generated_text.casefold()
    return sum(claim.casefold() in text for claim in forbidden_claims)


def phrase_detected(phrase: str, detected_claims: list[str]) -> bool:
    expected = phrase.casefold()
    return any(
        expected in detected.casefold() or detected.casefold() in expected
        for detected in detected_claims
    )

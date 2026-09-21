"""Governed evolution contracts; separate from the existing Skill registry."""
from __future__ import annotations

from enum import StrEnum


class SkillCandidateStatus(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED_FOR_EVALUATION = "approved_for_evaluation"
    EVALUATING = "evaluating"
    EVALUATION_FAILED = "evaluation_failed"
    READY_FOR_PUBLICATION = "ready_for_publication"
    REJECTED = "rejected"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class EvolutionVersionStatus(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    INACTIVE = "inactive"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class ActivationMode(StrEnum):
    INACTIVE = "inactive"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"


class EvaluationVariant(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"

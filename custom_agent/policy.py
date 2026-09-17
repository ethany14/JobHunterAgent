"""Pure transition rules for the custom agent state machine."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from custom_agent.errors import InvalidTransitionError
from custom_agent.events import AgentEventType
from custom_agent.state import AgentState, AgentStatus, Step


@dataclass(frozen=True)
class Transition:
    step: Step
    status: AgentStatus
    event_type: AgentEventType
    updates: Mapping[str, Any] = field(default_factory=dict)
    event_payload: Mapping[str, Any] = field(default_factory=dict)


class TransitionPolicy:
    """Stateless, deterministic transition policy."""

    _HAPPY_PATH = {
        Step.VALIDATE_INPUT: Step.ANALYZE_RESUME,
        Step.ANALYZE_RESUME: Step.VALIDATE_EVIDENCE,
        Step.VALIDATE_EVIDENCE: Step.ANALYZE_JOB,
        Step.ANALYZE_JOB: Step.MATCH_SKILLS,
        Step.MATCH_SKILLS: Step.WRITE_RESUME,
        Step.WRITE_RESUME: Step.VERIFY_RESUME,
        Step.REVISE_RESUME: Step.VERIFY_RESUME,
    }

    _EXPECTED_HANDLER_OUTPUT = {
        Step.ANALYZE_RESUME: "resume_analysis",
        Step.ANALYZE_JOB: "job_analysis",
        Step.MATCH_SKILLS: "skill_match",
        Step.WRITE_RESUME: "tailored_resume",
        Step.VERIFY_RESUME: "verification",
        Step.REVISE_RESUME: "tailored_resume",
    }

    @classmethod
    def after_step(
        cls,
        state: AgentState,
        *,
        produced_fields: Collection[str] = (),
    ) -> Transition:
        cls._require_active(state)
        if state.step == Step.HUMAN_REVIEW:
            raise InvalidTransitionError("Human review cannot run as a computation step.")
        cls._validate_step_output(state, produced_fields=set(produced_fields))
        if state.step == Step.VERIFY_RESUME:
            assert state.verification is not None
            if state.verification.passed:
                return Transition(
                    Step.HUMAN_REVIEW,
                    AgentStatus.AWAITING_REVIEW,
                    AgentEventType.PAUSED_FOR_REVIEW,
                    event_payload={"pause_reason": "verification_passed"},
                )
            if state.revision_count < state.max_revisions:
                return Transition(
                    Step.REVISE_RESUME,
                    AgentStatus.REVISING,
                    AgentEventType.STEP_COMPLETED,
                    event_payload={"reason": "verification_failed"},
                )
            return Transition(
                Step.HUMAN_REVIEW,
                AgentStatus.AWAITING_REVIEW,
                AgentEventType.PAUSED_FOR_REVIEW,
                event_payload={"pause_reason": "revision_limit_reached"},
            )
        next_step = cls._HAPPY_PATH.get(state.step)
        if next_step is None:
            raise InvalidTransitionError(f"No transition is defined after {state.step}.")
        return Transition(
            next_step,
            AgentStatus.RUNNING,
            AgentEventType.STEP_COMPLETED,
        )

    @classmethod
    def after_review(
        cls,
        state: AgentState,
        *,
        approved: bool,
        feedback: str | None,
    ) -> Transition:
        cls._require_active(state)
        if state.step != Step.HUMAN_REVIEW or state.status != AgentStatus.AWAITING_REVIEW:
            raise InvalidTransitionError("Run is not awaiting human review.")
        cleaned_feedback = feedback.strip() if isinstance(feedback, str) else None
        cleaned_feedback = cleaned_feedback or None
        if approved:
            if state.verification is None or not state.verification.passed:
                raise InvalidTransitionError(
                    "A resume with failed verification cannot be approved."
                )
            return Transition(
                Step.COMPLETED,
                AgentStatus.APPROVED,
                AgentEventType.REVIEW_APPROVED,
                updates={"approved": True, "human_feedback": cleaned_feedback},
            )
        if cleaned_feedback is None:
            raise ValueError("Feedback is required when the resume is rejected.")
        return Transition(
            Step.REVISE_RESUME,
            AgentStatus.REVISING,
            AgentEventType.REVIEW_REJECTED,
            updates={"approved": False, "human_feedback": cleaned_feedback},
            event_payload={"reason": "human_feedback"},
        )

    @classmethod
    def _validate_step_output(
        cls,
        state: AgentState,
        *,
        produced_fields: set[str],
    ) -> None:
        expected_field = cls._EXPECTED_HANDLER_OUTPUT.get(state.step)
        if expected_field is None:
            return
        if expected_field not in produced_fields:
            raise InvalidTransitionError(
                f"Handler for '{state.step.value}' did not produce "
                f"required field '{expected_field}'."
            )
        if getattr(state, expected_field) is None:
            raise InvalidTransitionError(
                f"Handler for '{state.step.value}' produced an empty "
                f"'{expected_field}' result."
            )

    @staticmethod
    def _require_active(state: AgentState) -> None:
        if state.terminal:
            raise InvalidTransitionError("A terminal run cannot continue.")

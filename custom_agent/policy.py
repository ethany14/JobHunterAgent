"""Pure transition rules for the custom agent state machine."""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_agent.errors import InvalidTransitionError
from custom_agent.events import AgentEventType
from custom_agent.state import AgentState, AgentStatus, Step


@dataclass(frozen=True)
class Transition:
    step: Step
    status: AgentStatus
    event_type: AgentEventType
    updates: dict = field(default_factory=dict)


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

    @classmethod
    def after_step(cls, state: AgentState) -> Transition:
        cls._require_active(state)
        if state.step == Step.HUMAN_REVIEW:
            raise InvalidTransitionError("Human review cannot run as a computation step.")
        if state.step == Step.VERIFY_RESUME:
            if state.verification is None:
                raise InvalidTransitionError("Verification result is missing.")
            if (
                not state.verification.passed
                and state.revision_count < state.max_revisions
            ):
                return Transition(
                    Step.REVISE_RESUME,
                    AgentStatus.REVISING,
                    AgentEventType.STEP_COMPLETED,
                )
            return Transition(
                Step.HUMAN_REVIEW,
                AgentStatus.AWAITING_REVIEW,
                AgentEventType.PAUSED_FOR_REVIEW,
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
            return Transition(
                Step.COMPLETED,
                AgentStatus.APPROVED,
                AgentEventType.REVIEW_APPROVED,
                {"approved": True, "human_feedback": cleaned_feedback},
            )
        if cleaned_feedback is None:
            raise ValueError("Feedback is required when the resume is rejected.")
        return Transition(
            Step.REVISE_RESUME,
            AgentStatus.REVISING,
            AgentEventType.REVIEW_REJECTED,
            {"approved": False, "human_feedback": cleaned_feedback},
        )

    @staticmethod
    def _require_active(state: AgentState) -> None:
        if state.terminal:
            raise InvalidTransitionError("A terminal run cannot continue.")

"""Injectable structured model boundary for interview decisions."""
from __future__ import annotations

from typing import Protocol

from agent_runtime.interviewer.prompts import ANSWER_SYSTEM, QUESTION_SYSTEM
from agent_runtime.interviewer.types import (
    ApplicationRequirementAssessment, InterviewAnswerAssessment, InterviewQuestion,
)
from job_agent.model import create_model, invoke_structured


class InterviewModelClient(Protocol):
    def question(self, assessment: ApplicationRequirementAssessment, context: str) -> InterviewQuestion: ...
    def classify(self, assessment: ApplicationRequirementAssessment, answer: str, context: str) -> InterviewAnswerAssessment: ...


class SharedInterviewModel:
    def question(self, assessment: ApplicationRequirementAssessment, context: str) -> InterviewQuestion:
        model = create_model()
        return invoke_structured(model, InterviewQuestion, QUESTION_SYSTEM, context)

    def classify(self, assessment: ApplicationRequirementAssessment, answer: str, context: str) -> InterviewAnswerAssessment:
        model = create_model()
        return invoke_structured(model, InterviewAnswerAssessment, ANSWER_SYSTEM, context)

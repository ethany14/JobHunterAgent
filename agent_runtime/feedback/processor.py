"""Resumable, deterministic feedback processing. No model or tool calls."""
from __future__ import annotations

from agent_runtime.feedback.policy import LearningThresholds, classify
from agent_runtime.feedback.repository import FeedbackRepository
from agent_runtime.feedback.types import FeedbackProcessStatus, LearningCandidate


class FeedbackProcessor:
    def __init__(self, repository: FeedbackRepository, *, thresholds: LearningThresholds | None = None):
        self.repository = repository
        self.thresholds = thresholds or LearningThresholds()

    def process(self, event_id: str, *, owner_id: str) -> LearningCandidate | None:
        event, attempt_id = self.repository.begin_attempt(
            event_id, owner_id=owner_id,
            max_attempts=self.thresholds.max_processing_attempts,
        )
        if not attempt_id:
            return self.repository.candidate_for_event(event_id, owner_id=owner_id)
        try:
            if event.processed_status in {FeedbackProcessStatus.UNPROCESSED,
                                          FeedbackProcessStatus.FAILED}:
                event = self.repository.stage(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, expected_version=event.processing_version,
                    status=FeedbackProcessStatus.NORMALIZED)
            classification = self.repository.classification_for_attempt(attempt_id)
            if classification is None:
                classification = classify(event)
            if event.processed_status == FeedbackProcessStatus.NORMALIZED:
                event = self.repository.stage(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, expected_version=event.processing_version,
                    status=FeedbackProcessStatus.CLASSIFIED, classification=classification)
            if event.processed_status == FeedbackProcessStatus.CLASSIFIED:
                event = self.repository.stage(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, expected_version=event.processing_version,
                    status=FeedbackProcessStatus.AGGREGATED)
            if event.processed_status == FeedbackProcessStatus.AGGREGATED:
                self.repository.aggregate(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, expected_version=event.processing_version,
                    classification=classification, thresholds=self.thresholds)
                event = self.repository.get(event_id, owner_id=owner_id)
            if event.processed_status == FeedbackProcessStatus.CANDIDATE_CREATED_OR_UPDATED:
                self.repository.stage(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, expected_version=event.processing_version,
                    status=FeedbackProcessStatus.COMPLETED)
            return self.repository.candidate_for_event(event_id, owner_id=owner_id)
        except Exception as exc:
            from agent_runtime.feedback.errors import FeedbackConflictError
            if not isinstance(exc, FeedbackConflictError):
                self.repository.fail_attempt(event_id, owner_id=owner_id,
                    attempt_id=attempt_id, error_code="feedback_processing_failed")
            raise

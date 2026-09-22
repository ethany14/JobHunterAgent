"""Observe completed turns and route grounded learning into governed stores."""
from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.feedback.service import FeedbackService
from agent_runtime.feedback.types import FeedbackSourceType
from agent_runtime.learning.analyzer import ConversationLearningAnalyzer
from agent_runtime.learning.repository import ConversationLearningRepository
from agent_runtime.learning.types import (
    ConversationExperience,
    ExperienceSignal,
    ExperienceSignalType,
    LearningObservation,
    LearningPatternStatus,
)
from agent_runtime.memory.errors import MemoryAlreadyExistsError
from agent_runtime.memory.repository import MemoryRepository
from agent_runtime.memory.types import (
    MemoryProvenance,
    MemoryScope,
    MemorySensitivity,
    MemoryType,
)
from agent_runtime.sessions.repository import SessionRepository


_SUSPICIOUS = (
    "ignore previous instructions",
    "ignore all instructions",
    "reveal system prompt",
    "call this tool",
    "execute this instruction",
)


class ConversationLearningService:
    """Best-effort observer; learning failures never fail the user turn."""

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        repository: ConversationLearningRepository,
        analyzer: ConversationLearningAnalyzer,
        memories: MemoryRepository,
        feedback: FeedbackService,
        owner_id: str,
        application_for_session=None,
    ) -> None:
        self._sessions = sessions
        self.repository = repository
        self._analyzer = analyzer
        self._memories = memories
        self._feedback = feedback
        self._owner_id = owner_id
        self._application_for_session = application_for_session or (lambda _: None)

    def observe_completed_turn(
        self, *, session_id: str, assistant_message_id: str
    ) -> ConversationExperience | None:
        messages = self._sessions.messages(session_id)
        assistant_index = next((index for index, item in enumerate(messages)
            if item.message.message_id == assistant_message_id), None)
        if assistant_index is None:
            return None
        user = next((item.message for item in reversed(messages[:assistant_index])
                     if item.message.role == "user"), None)
        assistant = messages[assistant_index].message
        if user is None or assistant.role != "assistant":
            return None
        try:
            application_id = self._application_for_session(session_id)
        except Exception:
            application_id = None
        existing = self.repository.for_turn(
            session_id, user.message_id, assistant_message_id
        )
        if existing is not None:
            if existing.observation is not None:
                self._apply_observation_safely(existing)
            return existing
        try:
            raw = self._analyzer.analyze(user_message=user, assistant_message=assistant)
            observation = self._validate_observation(raw, user.content)
        except Exception:
            experience, _ = self.repository.record(
                owner_id=self._owner_id,
                session_id=session_id,
                application_id=application_id,
                user_message_id=user.message_id,
                assistant_message_id=assistant_message_id,
                observation=None,
                error_code="conversation_observation_failed",
            )
            return experience
        experience, created = self.repository.record(
            owner_id=self._owner_id,
            session_id=session_id,
            application_id=application_id,
            user_message_id=user.message_id,
            assistant_message_id=assistant_message_id,
            observation=observation,
        )
        if not created:
            return experience
        self._apply_observation_safely(experience)
        return experience

    def _apply_observation_safely(self, experience: ConversationExperience) -> None:
        try:
            self._apply_observation(experience)
        except Exception:
            self.repository.record_event(
                experience.experience_id,
                "post_processing_failed",
                {"error_code": "conversation_learning_post_processing_failed"},
            )

    def _apply_observation(self, experience: ConversationExperience) -> None:
        observation = experience.observation
        if observation is None:
            return
        for index, signal in enumerate(observation.signals):
            if signal.signal_type in {
                ExperienceSignalType.PREFERENCE,
                ExperienceSignalType.CAREER_FACT,
            }:
                self._create_memory_candidate(experience, signal, index)
            elif signal.reusable_across_sessions and signal.confidence >= 0.7:
                pattern = self.repository.aggregate_procedural_signal(experience, signal)
                if pattern.status == LearningPatternStatus.READY_FOR_REVIEW:
                    event, _ = self._feedback.record(
                        owner_id=self._owner_id,
                        source_type=FeedbackSourceType.CONVERSATION_PATTERN,
                        source_action_id=f"conversation-pattern:{pattern.pattern_id}",
                        original_content=pattern.proposed_instruction,
                        session_id=experience.session_id,
                        application_id=experience.application_id,
                        context_metadata_json={
                            "canonical_key": pattern.canonical_key,
                            "experience_count": pattern.occurrence_count,
                            "session_count": pattern.session_count,
                            "experience_ids": pattern.experience_ids,
                            "provenance": "conversation_trajectory",
                        },
                    )
                    self.repository.mark_candidate_emitted(
                        pattern.pattern_id,
                        expected_version=pattern.version,
                        feedback_event_id=event.feedback_event_id,
                    )

    @staticmethod
    def _validate_observation(
        observation: LearningObservation, user_text: str
    ) -> LearningObservation:
        accepted: list[ExperienceSignal] = []
        lowered = user_text.casefold()
        for signal in observation.signals:
            if signal.signal_type == ExperienceSignalType.NONE:
                continue
            searchable = f"{signal.summary}\n{signal.evidence_quote or ''}".casefold()
            if any(phrase in searchable for phrase in _SUSPICIOUS):
                continue
            # Every durable signal must point to words the user actually
            # supplied. Assistant/tool/JD text is context, never learning
            # evidence by itself.
            quote = (signal.evidence_quote or "").strip()
            if not quote or quote.casefold() not in lowered:
                continue
            if not signal.canonical_key or not signal.summary.strip():
                continue
            accepted.append(signal)
        return LearningObservation(signals=accepted, turn_outcome=observation.turn_outcome)

    def _create_memory_candidate(
        self, experience: ConversationExperience, signal: ExperienceSignal, index: int
    ) -> None:
        key = re.sub(r"[^a-z0-9._:-]+", ".", signal.canonical_key.casefold()).strip(".")
        if not key:
            return
        memory_id = str(uuid5(
            NAMESPACE_URL, f"conversation:{experience.experience_id}:{index}:memory"
        ))
        try:
            self._memories.create_candidate(
                memory_id=memory_id,
                owner_id=self._owner_id,
                scope=MemoryScope.USER,
                scope_id=self._owner_id,
                memory_key=key[:160],
                memory_type=(MemoryType.PREFERENCE
                             if signal.signal_type == ExperienceSignalType.PREFERENCE
                             else MemoryType.SEMANTIC),
                display_text=signal.summary,
                content={"value": signal.normalized_value, "source_quote": signal.evidence_quote},
                provenance=[MemoryProvenance(
                    source_type="conversation_observation",
                    source_id=experience.experience_id,
                    actor_type="user",
                    user_confirmed=False,
                    metadata={
                        "session_id": experience.session_id,
                        "user_message_id": experience.user_message_id,
                    },
                )],
                sensitivity=(MemorySensitivity.SENSITIVE if signal.sensitive
                             else MemorySensitivity.PERSONAL),
                confidence=signal.confidence,
            )
        except MemoryAlreadyExistsError:
            return

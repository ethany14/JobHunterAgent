from __future__ import annotations

from alembic import command
from alembic.config import Config
from pathlib import Path
from sqlalchemy import inspect

from agent_runtime.feedback.repository import FeedbackRepository
from agent_runtime.feedback.service import FeedbackService
from agent_runtime.feedback.types import CandidateType
from agent_runtime.learning.repository import ConversationLearningRepository
from agent_runtime.learning.service import ConversationLearningService
from agent_runtime.learning.types import (
    ExperienceSignal,
    ExperienceSignalType,
    LearningObservation,
    LearningPatternStatus,
)
from agent_runtime.memory.repository import MemoryRepository
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionMessageDraft, SessionState
from agent_runtime.tools.messages import AgentMessage
from api.db import create_database


class ScriptedObserver:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = []

    def analyze(self, *, user_message, assistant_message):
        self.calls.append((user_message, assistant_message))
        result = self.observations.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _runtime(tmp_path, observations):
    database = create_database(
        f"sqlite:///{tmp_path / 'learning.db'}", create_schema_for_tests=True
    )
    sessions = SessionRepository(database.session_factory)
    memories = MemoryRepository(database.session_factory)
    feedback = FeedbackService(FeedbackRepository(database.session_factory), memories=memories)
    repository = ConversationLearningRepository(database.session_factory)
    service = ConversationLearningService(
        sessions=sessions,
        repository=repository,
        analyzer=ScriptedObserver(observations),
        memories=memories,
        feedback=feedback,
        owner_id="local-user",
    )
    return database, sessions, memories, feedback, repository, service


def _turn(sessions, session_id, user_text, assistant_text):
    user = AgentMessage(message_id=f"user-{session_id}", role="user", content=user_text)
    assistant = AgentMessage(
        message_id=f"assistant-{session_id}", role="assistant", content=assistant_text
    )
    sessions.create(
        SessionState(session_id=session_id, user_id="local-user"),
        SessionEvent(session_id=session_id, event_type=SessionEventType.SESSION_CREATED),
        messages=[SessionMessageDraft(message=user), SessionMessageDraft(message=assistant)],
    )
    return user, assistant


def test_conversation_preference_becomes_unconfirmed_memory_candidate(tmp_path):
    quote = "I prefer resume summaries with no more than two sentences."
    observation = LearningObservation(signals=[ExperienceSignal(
        signal_type=ExperienceSignalType.PREFERENCE,
        canonical_key="resume.summary.max_sentences",
        summary="Prefers resume summaries with no more than two sentences.",
        evidence_quote=quote,
        normalized_value=2,
        confidence=0.96,
    )])
    database, sessions, memories, _, repository, service = _runtime(tmp_path, [observation])
    _, assistant = _turn(sessions, "session-1", quote, "Understood.")

    experience = service.observe_completed_turn(
        session_id="session-1", assistant_message_id=assistant.message_id
    )

    assert experience.status == "observed"
    candidates = memories.list_candidates(owner_id="local-user")
    assert len(candidates) == 1
    assert candidates[0].memory_key == "resume.summary.max_sentences"
    assert candidates[0].content["value"] == 2
    assert candidates[0].provenance[0].source_id == experience.experience_id
    assert memories.list_confirmed(owner_id="local-user") == []
    replay = service.observe_completed_turn(
        session_id="session-1", assistant_message_id=assistant.message_id
    )
    assert replay.experience_id == experience.experience_id
    assert len(repository.experiences(owner_id="local-user")) == 1
    database.close()


def test_ungrounded_memory_observation_is_discarded(tmp_path):
    observation = LearningObservation(signals=[ExperienceSignal(
        signal_type=ExperienceSignalType.PREFERENCE,
        canonical_key="writing.style",
        summary="Prefers terse writing.",
        evidence_quote="I prefer terse writing.",
        normalized_value="terse",
        confidence=0.9,
    )])
    database, sessions, memories, _, _, service = _runtime(tmp_path, [observation])
    _, assistant = _turn(sessions, "session-1", "Tell me about this role.", "Here is the role.")
    experience = service.observe_completed_turn(
        session_id="session-1", assistant_message_id=assistant.message_id
    )
    assert experience.observation.signals == []
    assert memories.list_candidates(owner_id="local-user") == []
    database.close()


def test_repeated_cross_session_pattern_enters_existing_skill_governance(tmp_path):
    user_texts = [
        "This cover letter is just copied bullets; write a connected narrative.",
        "That still reads like copied bullets.",
        "This cover letter is just copied bullets; write a connected narrative.",
    ]
    observations = [LearningObservation(signals=[ExperienceSignal(
        signal_type=ExperienceSignalType.CORRECTION,
        canonical_key="cover-letter.compose-narrative",
        summary="Compose a coherent cover-letter narrative instead of concatenating resume bullets.",
        evidence_quote=user_text,
        confidence=0.9,
        reusable_across_sessions=True,
        rationale="The user corrected a repeated output failure.",
    )]) for user_text in user_texts]
    database, sessions, _, feedback, repository, service = _runtime(tmp_path, observations)
    for number in range(3):
        session_id = "session-a" if number < 2 else "session-b"
        if number == 1:
            # One persisted session can contain more than one turn.
            user = AgentMessage(message_id="user-session-a-2", role="user",
                                content="That still reads like copied bullets.")
            assistant = AgentMessage(message_id="assistant-session-a-2", role="assistant",
                                     content="I will rewrite it as a narrative.")
            state = sessions.require(session_id)
            sessions.save_transition(
                state,
                SessionEvent(session_id=session_id,
                             event_type=SessionEventType.MESSAGES_APPENDED),
                expected_version=state.version,
                messages=[SessionMessageDraft(message=user), SessionMessageDraft(message=assistant)],
            )
        else:
            _, assistant = _turn(
                sessions, session_id,
                "This cover letter is just copied bullets; write a connected narrative.",
                "I rewrote the letter.",
            )
        service.observe_completed_turn(
            session_id=session_id, assistant_message_id=assistant.message_id
        )

    candidates = feedback.repository.list_candidates(
        owner_id="local-user", candidate_type=CandidateType.SKILL
    )
    assert len(candidates) == 1
    assert candidates[0].status.value == "ready_for_review"
    pattern_id = feedback.repository.candidate_events(
        candidates[0].candidate_id, owner_id="local-user"
    )[0].context_metadata_json["canonical_key"]
    patterns = [item for item in repository.experiences(owner_id="local-user")]
    assert len(patterns) == 3
    assert pattern_id == "cover-letter.compose-narrative"
    database.close()


def test_observer_failure_is_audited_without_creating_learning_candidates(tmp_path):
    database, sessions, memories, feedback, _, service = _runtime(
        tmp_path, [RuntimeError("provider secret")]
    )
    _, assistant = _turn(sessions, "session-1", "Hello", "Hi")
    experience = service.observe_completed_turn(
        session_id="session-1", assistant_message_id=assistant.message_id
    )
    assert experience.status == "failed"
    assert experience.error_code == "conversation_observation_failed"
    assert memories.list_candidates(owner_id="local-user") == []
    assert feedback.repository.list_candidates(owner_id="local-user") == []
    database.close()


def test_conversation_learning_migration(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    database = create_database(url)
    tables = set(inspect(database.engine).get_table_names())
    assert {
        "conversation_experiences",
        "conversation_experience_events",
        "conversation_learning_patterns",
        "conversation_pattern_experiences",
    } <= tables
    database.close()

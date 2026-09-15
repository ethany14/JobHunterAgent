from __future__ import annotations

from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from api.db import create_database
from api.models import Run
from custom_agent.errors import InvalidTransitionError, StaleStateError
from custom_agent.events import AgentEvent, AgentEventType
from custom_agent.handlers import JobAgentStepHandler
from custom_agent.loop import AgentLoop
from custom_agent.models import CustomAgentStateRow
from custom_agent.policy import TransitionPolicy
from custom_agent.repository import StateRepository
from custom_agent.state import AgentState, AgentStatus, Step
from custom_agent.steps import StepOutcome
from job_agent.domain import make_evidence_id
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
    SupportedClaim,
    TailoredResume,
    UnsupportedClaim,
    VerificationResult,
)
from job_agent.prompts import (
    JOB_PROMPT,
    MATCH_PROMPT,
    RESUME_PROMPT,
    VERIFY_RESUME_PROMPT,
    WRITE_RESUME_PROMPT,
)

FACT = "Built Python APIs."
FACT_ID = make_evidence_id(FACT)


def database_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def resume_analysis() -> ResumeAnalysis:
    return ResumeAnalysis(
        summary="Python developer",
        skills=["Python"],
        evidence=[
            ResumeEvidence(
                evidence_id=FACT_ID,
                source_section="Experience",
                exact_text=FACT,
            )
        ],
        education=[],
    )


def job_analysis() -> JobAnalysis:
    return JobAnalysis(
        title="Backend Engineer",
        summary="Build APIs",
        requirements=[
            JobRequirement(
                requirement_id="REQ-001",
                canonical_name="python",
                original_text="Python",
                level="required",
            )
        ],
        responsibilities=["Build APIs"],
    )


def skill_match() -> SkillMatch:
    return SkillMatch(
        matches=[
            SkillEvidence(
                requirement_id="REQ-001",
                job_skill="python",
                requirement_level="required",
                match_status="matched",
                resume_evidence=[FACT],
                confidence=1,
            )
        ],
        explanation="Python is supported.",
        recommendations=[],
        missing_required_requirements=[],
        missing_preferred_requirements=[],
        overall_score=100,
    )


def tailored(text: str = "Python developer") -> TailoredResume:
    claim = SupportedClaim(text=text, evidence_ids=[FACT_ID])
    return TailoredResume(
        professional_summary=[claim],
        experience_bullets=[SupportedClaim(text=FACT, evidence_ids=[FACT_ID])],
        highlighted_skills=[SupportedClaim(text="Python", evidence_ids=[FACT_ID])],
    )


def passing() -> VerificationResult:
    return VerificationResult(
        passed=True, unsupported_claims=[], revision_feedback=[]
    )


def failing() -> VerificationResult:
    return VerificationResult(
        passed=False,
        unsupported_claims=[UnsupportedClaim(claim="AWS", reason="No evidence")],
        revision_feedback=["Remove AWS"],
    )


class FakeHandler:
    def __init__(self, verifications=None, on_revise=None) -> None:
        self.verifications = list(verifications or [passing()])
        self.on_revise = on_revise
        self.calls = []

    def execute(self, step, state):
        self.calls.append(step)
        updates = {
            Step.VALIDATE_INPUT: {},
            Step.ANALYZE_RESUME: {"resume_analysis": resume_analysis()},
            Step.VALIDATE_EVIDENCE: {},
            Step.ANALYZE_JOB: {"job_analysis": job_analysis()},
            Step.MATCH_SKILLS: {"skill_match": skill_match()},
            Step.WRITE_RESUME: {"tailored_resume": tailored()},
            Step.REVISE_RESUME: {"tailored_resume": tailored("Revised Python developer")},
        }
        if step == Step.REVISE_RESUME and self.on_revise:
            self.on_revise(state)
        if step == Step.VERIFY_RESUME:
            verification = self.verifications.pop(0)
            return StepOutcome(
                updates={
                    "verification": verification,
                    "revision_feedback": verification.revision_feedback,
                }
            )
        return StepOutcome(updates=updates[step])


class QueueAnalyzer:
    def __init__(self, outputs) -> None:
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, schema, system_message, human_message):
        self.calls.append((schema, system_message, human_message))
        return self.outputs.pop(0)


@pytest.fixture
def custom_runtime(tmp_path):
    database = create_database(
        database_url(tmp_path / "custom.sqlite"), create_schema_for_tests=True
    )
    repository = StateRepository(database.session_factory)
    resources = SimpleNamespace(database=database, repository=repository)
    try:
        yield resources
    finally:
        database.close()


def make_loop(repository, handler=None):
    return AgentLoop(repository=repository, handler=handler or FakeHandler())


def start(loop, max_revisions=3):
    return loop.start(
        resume_text=FACT,
        job_description="Requires Python",
        max_revisions=max_revisions,
    )


def test_alembic_migrations_create_runs_state_and_event_tables(tmp_path):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url(tmp_path / "migration.sqlite"))
    command.upgrade(config, "head")
    database = create_database(database_url(tmp_path / "migration.sqlite"))
    try:
        assert set(inspect(database.engine).get_table_names()) >= {
            "alembic_version",
            "runs",
            "custom_agent_states",
            "custom_agent_events",
        }
    finally:
        database.close()


def test_existing_v04_database_can_be_stamped_then_upgraded(tmp_path):
    path = tmp_path / "legacy-v04.sqlite"
    url = database_url(path)
    legacy_database = create_database(url)
    Run.__table__.create(legacy_database.engine)
    with legacy_database.session_factory.begin() as session:
        session.add(
            Run(
                run_id="legacy-run",
                thread_id="legacy-run",
                status="awaiting_review",
                resume_text="Legacy resume",
                job_description="Legacy job",
            )
        )
    legacy_database.close()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.stamp(config, "0001_create_runs")
    command.upgrade(config, "head")

    upgraded = create_database(url)
    try:
        with upgraded.session_factory() as session:
            legacy_run = session.get(Run, "legacy-run")
        assert legacy_run is not None
        assert legacy_run.resume_text == "Legacy resume"
        assert set(inspect(upgraded.engine).get_table_names()) >= {
            "runs",
            "custom_agent_states",
            "custom_agent_events",
        }
    finally:
        upgraded.close()


def test_sqlite_foreign_keys_reject_orphan_custom_state(tmp_path):
    database = create_database(
        database_url(tmp_path / "foreign-key.sqlite"),
        create_schema_for_tests=True,
    )
    try:
        with database.engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        with pytest.raises(IntegrityError):
            with database.session_factory.begin() as session:
                session.add(
                    CustomAgentStateRow(
                        run_id="missing-run",
                        state_json="{}",
                        step=Step.VALIDATE_INPUT.value,
                        status=AgentStatus.RUNNING.value,
                        version=0,
                        updated_at=AgentState(
                            run_id="missing-run",
                            resume_text="Resume",
                            job_description="Job",
                        ).updated_at,
                    )
                )
    finally:
        database.close()


def test_complete_happy_path_pauses_then_approves(custom_runtime):
    loop = make_loop(custom_runtime.repository)
    paused = start(loop)
    assert paused.step == Step.HUMAN_REVIEW
    assert paused.status == AgentStatus.AWAITING_REVIEW
    assert custom_runtime.repository.events(paused.run_id)[-1].event_type == (
        AgentEventType.PAUSED_FOR_REVIEW
    )

    completed = loop.review(
        paused.run_id,
        approved=True,
        feedback=None,
        expected_version=paused.version,
    )
    assert completed.step == Step.COMPLETED
    assert completed.status == AgentStatus.APPROVED
    with pytest.raises(InvalidTransitionError, match="terminal"):
        loop.run_until_pause(paused.run_id)


def test_real_step_handler_completes_happy_path_with_fake_model(custom_runtime):
    matched = skill_match()
    analyzer = QueueAnalyzer(
        [
            resume_analysis(),
            job_analysis(),
            SkillAssessment(
                matches=matched.matches,
                explanation=matched.explanation,
                recommendations=matched.recommendations,
            ),
            tailored(),
            passing(),
        ]
    )
    loop = make_loop(custom_runtime.repository, JobAgentStepHandler(analyzer))
    paused = start(loop)
    assert paused.status == AgentStatus.AWAITING_REVIEW
    assert [call[0] for call in analyzer.calls] == [
        ResumeAnalysis,
        JobAnalysis,
        SkillAssessment,
        TailoredResume,
        VerificationResult,
    ]
    assert [call[1] for call in analyzer.calls] == [
        RESUME_PROMPT,
        JOB_PROMPT,
        MATCH_PROMPT,
        WRITE_RESUME_PROMPT,
        VERIFY_RESUME_PROMPT,
    ]
    assert analyzer.calls[0][2] == FACT
    assert analyzer.calls[1][2] == "Requires Python"


def test_pause_reject_revise_verify_and_pause_again(custom_runtime):
    handler = FakeHandler(verifications=[passing(), passing()])
    loop = make_loop(custom_runtime.repository, handler)
    paused = start(loop)
    revised = loop.review(
        paused.run_id,
        approved=False,
        feedback="Shorten the summary.",
        expected_version=paused.version,
    )
    assert revised.step == Step.HUMAN_REVIEW
    assert revised.status == AgentStatus.AWAITING_REVIEW
    assert revised.revision_count == 1
    assert revised.human_feedback is None
    event_types = [event.event_type for event in custom_runtime.repository.events(paused.run_id)]
    assert AgentEventType.REVIEW_REJECTED in event_types
    assert event_types[-1] == AgentEventType.PAUSED_FOR_REVIEW


def test_verifier_failure_respects_revision_limit(custom_runtime):
    handler = FakeHandler(verifications=[failing(), failing()])
    paused = start(make_loop(custom_runtime.repository, handler), max_revisions=1)
    assert paused.status == AgentStatus.AWAITING_REVIEW
    assert paused.revision_count == 1
    assert paused.verification.passed is False
    assert handler.calls.count(Step.REVISE_RESUME) == 1


def test_stale_version_and_duplicate_review_are_rejected(custom_runtime):
    loop = make_loop(custom_runtime.repository)
    paused = start(loop)
    with pytest.raises(StaleStateError, match="stale"):
        loop.review(
            paused.run_id,
            approved=True,
            feedback=None,
            expected_version=paused.version - 1,
        )
    completed = loop.review(
        paused.run_id,
        approved=True,
        feedback=None,
        expected_version=paused.version,
    )
    with pytest.raises(InvalidTransitionError):
        loop.review(completed.run_id, approved=True, feedback=None)


def test_rejection_without_feedback_is_validation_error(custom_runtime):
    loop = make_loop(custom_runtime.repository)
    paused = start(loop)
    with pytest.raises(ValueError, match="Feedback is required"):
        loop.review(paused.run_id, approved=False, feedback=" ")
    unchanged = custom_runtime.repository.require(paused.run_id)
    assert unchanged.version == paused.version
    assert unchanged.status == AgentStatus.AWAITING_REVIEW


def test_rejection_state_event_and_projection_commit_before_revision(custom_runtime):
    observed = {}

    def inspect_rejection(state):
        stored = custom_runtime.repository.require(state.run_id)
        events = custom_runtime.repository.events(state.run_id)
        with custom_runtime.database.session_factory() as session:
            run = session.get(Run, state.run_id)
            observed.update(
                state_status=stored.status,
                feedback=stored.human_feedback,
                event_type=events[-1].event_type,
                run_status=run.status,
            )

    handler = FakeHandler(verifications=[passing(), passing()], on_revise=inspect_rejection)
    loop = make_loop(custom_runtime.repository, handler)
    paused = start(loop)
    loop.review(paused.run_id, approved=False, feedback="Emphasize Python.")
    assert observed == {
        "state_status": AgentStatus.REVISING,
        "feedback": "Emphasize Python.",
        "event_type": AgentEventType.REVIEW_REJECTED,
        "run_status": "revising",
    }


def test_state_event_projection_roll_back_together_on_event_conflict(custom_runtime):
    loop = make_loop(custom_runtime.repository)
    paused = start(loop)
    original_events = custom_runtime.repository.events(paused.run_id)
    conflicting = AgentEvent(
        event_id=original_events[0].event_id,
        run_id=paused.run_id,
        sequence=paused.event_sequence + 1,
        event_type=AgentEventType.REVIEW_APPROVED,
        step=Step.HUMAN_REVIEW,
    )
    approved = AgentState.model_validate(
        {
            **paused.model_dump(),
            "step": Step.COMPLETED,
            "status": AgentStatus.APPROVED,
            "approved": True,
        }
    )
    with pytest.raises(IntegrityError):
        custom_runtime.repository.save(
            approved,
            conflicting,
            expected_version=paused.version,
            result={"approved": True},
        )
    stored = custom_runtime.repository.require(paused.run_id)
    with custom_runtime.database.session_factory() as session:
        projection = session.get(Run, paused.run_id)
        assert projection.status == "awaiting_review"
    assert stored.version == paused.version
    assert stored.status == AgentStatus.AWAITING_REVIEW
    assert len(custom_runtime.repository.events(paused.run_id)) == len(original_events)


def test_new_repository_instance_recovers_awaiting_review(tmp_path):
    path = tmp_path / "restart.sqlite"
    first_database = create_database(database_url(path), create_schema_for_tests=True)
    first_loop = make_loop(StateRepository(first_database.session_factory))
    paused = start(first_loop)
    first_database.close()

    restarted_database = create_database(database_url(path))
    try:
        restarted_loop = make_loop(StateRepository(restarted_database.session_factory))
        recovered = restarted_loop.review(
            paused.run_id,
            approved=True,
            feedback=None,
            expected_version=paused.version,
        )
        assert recovered.status == AgentStatus.APPROVED
        assert recovered.step == Step.COMPLETED
    finally:
        restarted_database.close()


def test_transition_policy_is_pure_and_covers_required_branches():
    policy = TransitionPolicy()
    base = AgentState(run_id="run", resume_text="Resume", job_description="Job")
    assert policy.after_step(base).step == Step.ANALYZE_RESUME

    failed = base.model_copy(
        update={
            "step": Step.VERIFY_RESUME,
            "verification": failing(),
            "revision_count": 0,
        }
    )
    assert policy.after_step(failed).step == Step.REVISE_RESUME
    limited = failed.model_copy(update={"revision_count": 3})
    assert policy.after_step(limited).step == Step.HUMAN_REVIEW

    review = base.model_copy(
        update={
            "step": Step.HUMAN_REVIEW,
            "status": AgentStatus.AWAITING_REVIEW,
        }
    )
    assert policy.after_review(review, approved=True, feedback=None).step == Step.COMPLETED
    assert (
        policy.after_review(review, approved=False, feedback="Revise").step
        == Step.REVISE_RESUME
    )
    with pytest.raises(ValueError):
        policy.after_review(review, approved=False, feedback=None)
    terminal = base.model_copy(
        update={"step": Step.COMPLETED, "status": AgentStatus.APPROVED}
    )
    with pytest.raises(InvalidTransitionError):
        policy.after_step(terminal)

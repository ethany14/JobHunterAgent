from __future__ import annotations

import json
from datetime import UTC
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from api.db import create_database
from api.models import Run
from custom_agent.errors import (
    InvalidTransitionError,
    RunAlreadyExistsError,
    StaleStateError,
)
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
    ResumeEntry,
    ResumeEvidence,
    ResumeSection,
    ResumeSourceEntry,
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
    def __init__(
        self, verifications=None, on_revise=None, missing_output_step=None
    ) -> None:
        self.verifications = list(verifications or [passing()])
        self.on_revise = on_revise
        self.missing_output_step = missing_output_step
        self.calls = []
        self.verification_inputs = []

    def execute(self, step, state):
        self.calls.append(step)
        if step == self.missing_output_step:
            return StepOutcome(updates={})
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
            self.verification_inputs.append(state)
            verification = self.verifications.pop(0)
            if isinstance(verification, Exception):
                raise verification
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
    with legacy_database.engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE runs (
                run_id VARCHAR(36) PRIMARY KEY NOT NULL,
                thread_id VARCHAR(36) UNIQUE NOT NULL,
                status VARCHAR(32) NOT NULL,
                resume_text TEXT NOT NULL,
                job_description TEXT NOT NULL,
                result_json TEXT,
                error_message TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
        connection.exec_driver_sql("""
            INSERT INTO runs (
                run_id, thread_id, status, resume_text, job_description,
                created_at, updated_at
            ) VALUES (
                'legacy-run', 'legacy-run', 'awaiting_review',
                'Legacy resume', 'Legacy job', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """)
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


def test_create_rejects_mismatched_state_and_event_identity(custom_runtime):
    state = AgentState(
        run_id="state-run",
        resume_text="Resume",
        job_description="Job",
    )
    event = AgentEvent(
        run_id="event-run",
        sequence=1,
        event_type=AgentEventType.RUN_CREATED,
        step=Step.VALIDATE_INPUT,
    )
    with pytest.raises(ValueError, match="run IDs do not match"):
        custom_runtime.repository.create(state, event)
    assert custom_runtime.repository.get(state.run_id) is None
    with custom_runtime.database.session_factory() as session:
        assert session.get(Run, state.run_id) is None


@pytest.mark.parametrize(
    ("event_type", "event_step", "message"),
    [
        (
            AgentEventType.STEP_COMPLETED,
            Step.VALIDATE_INPUT,
            "Initial event must be RUN_CREATED",
        ),
        (
            AgentEventType.RUN_CREATED,
            Step.ANALYZE_RESUME,
            "Initial event step does not match",
        ),
    ],
)
def test_create_rejects_invalid_initial_event(
    custom_runtime, event_type, event_step, message
):
    state = AgentState(
        run_id=f"invalid-{event_type.value}-{event_step.value}",
        resume_text="Resume",
        job_description="Job",
    )
    event = AgentEvent(
        run_id=state.run_id,
        sequence=1,
        event_type=event_type,
        step=event_step,
    )
    with pytest.raises(ValueError, match=message):
        custom_runtime.repository.create(state, event)
    assert custom_runtime.repository.get(state.run_id) is None


def test_duplicate_run_id_raises_stable_domain_error(custom_runtime):
    state = AgentState(
        run_id="duplicate-run",
        resume_text="Resume",
        job_description="Job",
    )

    def created_event():
        return AgentEvent(
            run_id=state.run_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            step=Step.VALIDATE_INPUT,
        )

    custom_runtime.repository.create(state, created_event())
    with pytest.raises(RunAlreadyExistsError, match="already exists"):
        custom_runtime.repository.create(state, created_event())
    assert len(custom_runtime.repository.events(state.run_id)) == 1


def test_repository_updates_revalidate_agent_state_contract(custom_runtime):
    state = AgentState(
        run_id="revalidate-state",
        resume_text="Resume",
        job_description="Job",
    )
    created = custom_runtime.repository.create(
        state,
        AgentEvent(
            run_id=state.run_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            step=Step.VALIDATE_INPUT,
        ),
    )
    invalid = created.model_copy(
        update={
            "step": Step.HUMAN_REVIEW,
            "status": AgentStatus.RUNNING,
            "verification": passing(),
        }
    )
    event = AgentEvent(
        run_id=state.run_id,
        sequence=2,
        event_type=AgentEventType.STEP_COMPLETED,
        step=Step.VALIDATE_INPUT,
    )
    with pytest.raises(ValidationError, match="requires status"):
        custom_runtime.repository.save(
            invalid,
            event,
            expected_version=0,
            result=None,
        )
    unchanged = custom_runtime.repository.require(state.run_id)
    assert unchanged.step == Step.VALIDATE_INPUT
    assert unchanged.version == 0
    assert len(custom_runtime.repository.events(state.run_id)) == 1


def test_same_expected_version_allows_only_one_save(custom_runtime):
    state = AgentState(
        run_id="optimistic-run",
        resume_text="Resume",
        job_description="Job",
    )
    created = custom_runtime.repository.create(
        state,
        AgentEvent(
            run_id=state.run_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            step=Step.VALIDATE_INPUT,
        ),
    )
    next_state = AgentState.model_validate(
        {**created.model_dump(mode="python"), "step": Step.ANALYZE_RESUME}
    )

    def completed_event():
        return AgentEvent(
            run_id=state.run_id,
            sequence=2,
            event_type=AgentEventType.STEP_COMPLETED,
            step=Step.VALIDATE_INPUT,
        )

    saved = custom_runtime.repository.save(
        next_state,
        completed_event(),
        expected_version=0,
        result=None,
    )
    assert saved.version == 1
    with pytest.raises(StaleStateError, match="stale"):
        custom_runtime.repository.save(
            next_state,
            completed_event(),
            expected_version=0,
            result=None,
        )
    assert custom_runtime.repository.require(state.run_id).version == 1
    assert len(custom_runtime.repository.events(state.run_id)) == 2


def test_event_datetime_round_trip_is_utc_aware(custom_runtime):
    state = AgentState(
        run_id="utc-event",
        resume_text="Resume",
        job_description="Job",
    )
    custom_runtime.repository.create(
        state,
        AgentEvent(
            run_id=state.run_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            step=Step.VALIDATE_INPUT,
        ),
    )
    occurred_at = custom_runtime.repository.events(state.run_id)[0].occurred_at
    assert occurred_at.tzinfo is not None
    assert occurred_at.utcoffset() == UTC.utcoffset(occurred_at)


def test_complete_happy_path_pauses_then_approves(custom_runtime):
    loop = make_loop(custom_runtime.repository)
    paused = start(loop)
    assert paused.step == Step.HUMAN_REVIEW
    assert paused.status == AgentStatus.AWAITING_REVIEW
    assert custom_runtime.repository.events(paused.run_id)[-1].event_type == (
        AgentEventType.PAUSED_FOR_REVIEW
    )
    with custom_runtime.database.session_factory() as session:
        paused_projection = session.get(Run, paused.run_id)
        paused_result = json.loads(paused_projection.result_json)
        assert paused_projection.status == "awaiting_review"
        assert "workflow_status" not in paused_result
        assert not {
            "resume_text",
            "job_description",
            "version",
            "error_message",
        } & paused_result.keys()
        assert set(paused_result) == AgentState.PUBLIC_RESULT_FIELDS
    assert custom_runtime.repository.events(paused.run_id)[-1].payload[
        "pause_reason"
    ] == "verification_passed"

    completed = loop.review(
        paused.run_id,
        approved=True,
        feedback=None,
        expected_version=paused.version,
    )
    assert completed.step == Step.COMPLETED
    assert completed.status == AgentStatus.APPROVED
    with custom_runtime.database.session_factory() as session:
        approved_projection = session.get(Run, paused.run_id)
        approved_result = json.loads(approved_projection.result_json)
        assert approved_projection.status == "approved"
        assert "workflow_status" not in approved_result
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


def test_skill_matching_normalizes_evidence_and_restores_exact_resume_text():
    assessment = SkillAssessment(
        matches=[
            SkillEvidence(
                requirement_id="REQ-001",
                job_skill="python",
                requirement_level="required",
                match_status="matched",
                resume_evidence=["  BUILT   python APIs!  "],
                confidence=1,
            )
        ],
        explanation="Python is supported.",
        recommendations=[],
    )
    state = AgentState(
        run_id="normalized-evidence",
        resume_text=FACT,
        job_description="Requires Python",
        step=Step.MATCH_SKILLS,
        resume_analysis=resume_analysis(),
        job_analysis=job_analysis(),
    )
    outcome = JobAgentStepHandler(QueueAnalyzer([assessment])).execute(
        Step.MATCH_SKILLS, state
    )
    matched = outcome.updates["skill_match"].matches[0]
    assert matched.match_status == "matched"
    assert matched.resume_evidence == [FACT]


def test_custom_resume_analysis_preserves_verbatim_evidence_text():
    verbatim = "  Built Python APIs.  "
    analysis = ResumeAnalysis(
        summary="Developer",
        skills=["Python"],
        evidence=[
            ResumeEvidence(
                evidence_id="temporary",
                source_section="Experience",
                exact_text=verbatim,
            )
        ],
        education=[],
    )
    state = AgentState(
        run_id="verbatim-evidence",
        resume_text=verbatim,
        job_description="Requires Python",
        step=Step.ANALYZE_RESUME,
    )
    outcome = JobAgentStepHandler(QueueAnalyzer([analysis])).execute(
        Step.ANALYZE_RESUME, state
    )
    assert outcome.updates["resume_analysis"].evidence[0].exact_text == verbatim


def test_custom_loop_retries_then_discards_non_verbatim_model_evidence(custom_runtime):
    analysis = ResumeAnalysis(
        summary="Developer",
        skills=["Python"],
        evidence=[
            ResumeEvidence(
                evidence_id="valid",
                source_section="Experience",
                exact_text="Built Python APIs.",
            ),
            ResumeEvidence(
                evidence_id="combined",
                source_section="Education",
                exact_text="Example University\nExpected May 2027",
            ),
        ],
        source_entries=[
            ResumeSourceEntry(
                source_entry_id="temporary-experience",
                entry_type="experience",
                heading="Developer",
                evidence_ids=["valid"],
            ),
            ResumeSourceEntry(
                source_entry_id="temporary-education",
                entry_type="education",
                heading="Example University",
                evidence_ids=["combined"],
            ),
        ],
        education=[],
    )
    analyzer = QueueAnalyzer([analysis, analysis])
    loop = make_loop(custom_runtime.repository, JobAgentStepHandler(analyzer))
    state = AgentState(
        run_id="discard-invalid-evidence",
        resume_text="Built Python APIs.\nExample University\nOther text\nExpected May 2027",
        job_description="Requires Python",
        step=Step.ANALYZE_RESUME,
    )
    outcome = loop._execute_step(state)
    grounded = ResumeAnalysis.model_validate(outcome.updates["resume_analysis"])

    assert [item.exact_text for item in grounded.evidence] == [
        "Built Python APIs."
    ]
    assert len(grounded.source_entries) == 1
    assert grounded.source_entries[0].entry_type == "experience"
    assert [call[0] for call in analyzer.calls].count(ResumeAnalysis) == 2


def test_verifier_rejects_claim_without_evidence_ids():
    unsupported_claim = SupportedClaim.model_construct(
        claim_id="unsupported",
        text="Python developer",
        evidence_ids=[],
        source_entry_id="legacy:summary",
        target_requirement_ids=[],
    )
    unvalidated_resume = TailoredResume.model_construct(sections=[
        ResumeSection.model_construct(section_type="summary", title="Summary", entries=[
            ResumeEntry.model_construct(entry_id="summary", bullets=[unsupported_claim])]),
        ResumeSection.model_construct(section_type="experience", title="Experience", entries=[
            ResumeEntry.model_construct(entry_id="experience", bullets=[
                SupportedClaim(text=FACT, evidence_ids=[FACT_ID])])]),
        ResumeSection.model_construct(section_type="skills", title="Skills", entries=[
            ResumeEntry.model_construct(entry_id="skills", bullets=[
                SupportedClaim(text="Python", evidence_ids=[FACT_ID])])]),
    ])
    state = AgentState(
        run_id="missing-evidence-id",
        resume_text=FACT,
        job_description="Requires Python",
        step=Step.VERIFY_RESUME,
        resume_analysis=resume_analysis(),
        tailored_resume=tailored(),
    ).model_copy(update={"tailored_resume": unvalidated_resume})
    outcome = JobAgentStepHandler(QueueAnalyzer([passing()])).execute(
        Step.VERIFY_RESUME, state
    )
    verification = outcome.updates["verification"]
    assert verification.passed is False
    assert verification.unsupported_claims[0].claim == "Python developer"
    assert verification.unsupported_claims[0].reason == (
        "The claim does not reference any resume evidence."
    )


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
    assert custom_runtime.repository.events(paused.run_id)[-1].payload[
        "pause_reason"
    ] == "revision_limit_reached"


def test_new_verification_replaces_failed_verification_after_revision(custom_runtime):
    handler = FakeHandler(verifications=[failing(), passing()])
    paused = start(make_loop(custom_runtime.repository, handler))
    assert paused.verification == passing()
    assert paused.revision_count == 1


@pytest.mark.parametrize(
    ("missing_step", "verifications", "required_field"),
    [
        (Step.ANALYZE_RESUME, [passing()], "resume_analysis"),
        (Step.WRITE_RESUME, [passing()], "tailored_resume"),
        (Step.VERIFY_RESUME, [passing()], "verification"),
        (Step.REVISE_RESUME, [failing()], "tailored_resume"),
    ],
)
def test_handler_must_produce_required_step_output(
    custom_runtime, missing_step, verifications, required_field
):
    run_id = f"missing-{missing_step.value}"
    handler = FakeHandler(
        verifications=verifications,
        missing_output_step=missing_step,
    )
    with pytest.raises(InvalidTransitionError, match=required_field):
        make_loop(custom_runtime.repository, handler).start(
            run_id=run_id,
            resume_text=FACT,
            job_description="Requires Python",
        )
    stored = custom_runtime.repository.require(run_id)
    assert stored.step == missing_step


def test_successful_revision_invalidates_verification_before_reverification_failure(
    custom_runtime,
):
    run_id = "revision-then-verifier-failure"
    handler = FakeHandler(
        verifications=[failing(), RuntimeError("verification crashed")]
    )
    loop = make_loop(custom_runtime.repository, handler)

    with pytest.raises(RuntimeError, match="verification crashed"):
        loop.start(
            run_id=run_id,
            resume_text=FACT,
            job_description="Requires Python",
        )

    reverification_input = handler.verification_inputs[1]
    assert reverification_input.step == Step.VERIFY_RESUME
    assert reverification_input.tailored_resume == tailored("Revised Python developer")
    assert reverification_input.verification is None
    assert reverification_input.revision_count == 1
    stored = custom_runtime.repository.require(run_id)
    assert stored.step == Step.FAILED
    assert stored.status == AgentStatus.FAILED
    assert stored.tailored_resume == tailored("Revised Python developer")
    assert stored.verification is None
    assert stored.revision_count == 1
    with custom_runtime.database.session_factory() as session:
        projection = session.get(Run, run_id)
        assert projection.status == "failed"
        assert projection.result_json is None


def test_failed_reverification_preserves_previous_stable_public_result(custom_runtime):
    handler = FakeHandler(
        verifications=[passing(), RuntimeError("verification crashed")]
    )
    loop = make_loop(custom_runtime.repository, handler)
    paused = start(loop)
    with custom_runtime.database.session_factory() as session:
        before = session.get(Run, paused.run_id)
        previous_result_json = before.result_json

    with pytest.raises(RuntimeError, match="verification crashed"):
        loop.review(
            paused.run_id,
            approved=False,
            feedback="Shorten the summary.",
            expected_version=paused.version,
        )

    stored = custom_runtime.repository.require(paused.run_id)
    assert stored.status == AgentStatus.FAILED
    assert stored.tailored_resume == tailored("Revised Python developer")
    assert stored.verification is None
    with custom_runtime.database.session_factory() as session:
        projection = session.get(Run, paused.run_id)
        assert projection.status == "failed"
        assert projection.result_json == previous_result_json
        retained = json.loads(projection.result_json)
    assert retained["tailored_resume"] == tailored().model_dump(mode="json")
    assert retained["verification"] == passing().model_dump(mode="json")
    assert "workflow_status" not in retained


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


def test_failed_verification_cannot_be_approved_but_can_be_revised(custom_runtime):
    handler = FakeHandler(verifications=[failing(), passing()])
    loop = make_loop(custom_runtime.repository, handler)
    paused = start(loop, max_revisions=0)
    assert paused.verification.passed is False
    with pytest.raises(InvalidTransitionError, match="cannot be approved"):
        loop.review(
            paused.run_id,
            approved=True,
            feedback=None,
            expected_version=paused.version,
        )
    unchanged = custom_runtime.repository.require(paused.run_id)
    assert unchanged.version == paused.version
    revised = loop.review(
        paused.run_id,
        approved=False,
        feedback="Remove the unsupported claim.",
        expected_version=paused.version,
    )
    assert revised.status == AgentStatus.AWAITING_REVIEW
    assert revised.verification.passed is True
    rejected_event = next(
        event
        for event in custom_runtime.repository.events(paused.run_id)
        if event.event_type == AgentEventType.REVIEW_REJECTED
    )
    assert rejected_event.payload["reason"] == "human_feedback"


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

    failed = AgentState(
        run_id="failed-verification",
        resume_text="Resume",
        job_description="Job",
        step=Step.VERIFY_RESUME,
        verification=failing(),
    )
    assert policy.after_step(
        failed, produced_fields={"verification"}
    ).step == Step.REVISE_RESUME
    limited = AgentState(
        run_id="limited-verification",
        resume_text="Resume",
        job_description="Job",
        step=Step.VERIFY_RESUME,
        verification=failing(),
        revision_count=3,
    )
    assert policy.after_step(
        limited, produced_fields={"verification"}
    ).step == Step.HUMAN_REVIEW

    review = AgentState(
        run_id="review",
        resume_text="Resume",
        job_description="Job",
        step=Step.HUMAN_REVIEW,
        status=AgentStatus.AWAITING_REVIEW,
        verification=passing(),
    )
    assert policy.after_review(review, approved=True, feedback=None).step == Step.COMPLETED
    assert (
        policy.after_review(review, approved=False, feedback="Revise").step
        == Step.REVISE_RESUME
    )
    with pytest.raises(ValueError):
        policy.after_review(review, approved=False, feedback=None)
    terminal = AgentState(
        run_id="terminal",
        resume_text="Resume",
        job_description="Job",
        step=Step.COMPLETED,
        status=AgentStatus.APPROVED,
        approved=True,
        verification=passing(),
    )
    with pytest.raises(InvalidTransitionError):
        policy.after_step(terminal)


def test_agent_state_accepts_valid_lifecycle_states():
    states = [
        AgentState(run_id="initial", resume_text="Resume", job_description="Job"),
        AgentState(
            run_id="running",
            resume_text="Resume",
            job_description="Job",
            step=Step.ANALYZE_JOB,
        ),
        AgentState(
            run_id="revising",
            resume_text="Resume",
            job_description="Job",
            step=Step.REVISE_RESUME,
            status=AgentStatus.REVISING,
            verification=failing(),
        ),
        AgentState(
            run_id="paused",
            resume_text="Resume",
            job_description="Job",
            step=Step.HUMAN_REVIEW,
            status=AgentStatus.AWAITING_REVIEW,
            verification=passing(),
        ),
        AgentState(
            run_id="completed",
            resume_text="Resume",
            job_description="Job",
            step=Step.COMPLETED,
            status=AgentStatus.APPROVED,
            approved=True,
            verification=passing(),
        ),
        AgentState(
            run_id="failed",
            resume_text="Resume",
            job_description="Job",
            step=Step.FAILED,
            status=AgentStatus.FAILED,
            error_message="Safe failure message.",
        ),
    ]
    assert all(isinstance(state, AgentState) for state in states)


@pytest.mark.parametrize(
    "updates",
    [
        {
            "step": Step.HUMAN_REVIEW,
            "status": AgentStatus.RUNNING,
            "verification": passing(),
        },
        {
            "step": Step.COMPLETED,
            "status": AgentStatus.APPROVED,
            "approved": False,
            "verification": passing(),
        },
        {
            "step": Step.COMPLETED,
            "status": AgentStatus.APPROVED,
            "approved": True,
            "verification": None,
        },
        {
            "step": Step.FAILED,
            "status": AgentStatus.FAILED,
            "error_message": None,
        },
    ],
)
def test_agent_state_rejects_invalid_invariants(updates):
    with pytest.raises(ValidationError):
        AgentState.model_validate(
            {
                "run_id": "invalid",
                "resume_text": "Resume",
                "job_description": "Job",
                **updates,
            }
        )

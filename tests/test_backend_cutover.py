from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import NotRequired, TypedDict

import pytest
from alembic import command
from alembic.config import Config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from api.backend_classification import classify_legacy_run_backends
from api.db import create_database
from api.repositories.run_repository import RunRepository
from api.runtime import create_frozen_langgraph_run_service, create_run_service
from api.schemas.runs import CreateRunRequest, ReviewRequest
from api.services.run_service import BackendRoutingRunService, InvalidRunStateError
from custom_agent.state import Step
from custom_agent.steps import StepOutcome
from job_agent.domain import make_evidence_id
from job_agent.schemas import (JobAnalysis, JobRequirement, ResumeAnalysis,
    ResumeEvidence, ScoreBreakdown, SkillEvidence, SkillMatch, SupportedClaim,
    TailoredResume, VerificationResult)

FACT = "Built Python APIs."
FACT_ID = make_evidence_id(FACT)


class CompleteFakeHandler:
    def execute(self, step, state):
        resume = ResumeAnalysis(summary="Developer", skills=["Python"], evidence=[
            ResumeEvidence(evidence_id=FACT_ID, source_section="Experience", exact_text=FACT)], education=[])
        requirement = JobRequirement(requirement_id="REQ-1", canonical_name="python",
            original_text="Python", level="required")
        job = JobAnalysis(title="Backend Engineer", summary="Build APIs",
            requirements=[requirement], responsibilities=[])
        match = SkillMatch(matches=[SkillEvidence(requirement_id="REQ-1",
            job_skill="python", requirement_level="required", match_status="matched",
            resume_evidence=[FACT], confidence=1)], explanation="Supported",
            recommendations=[], missing_required_requirements=[],
            missing_preferred_requirements=[], overall_score=100,
            score_breakdown=ScoreBreakdown(overall_score=100))
        claim = SupportedClaim(text=FACT, evidence_ids=[FACT_ID])
        updates = {
            Step.VALIDATE_INPUT: {}, Step.ANALYZE_RESUME: {"resume_analysis": resume},
            Step.VALIDATE_EVIDENCE: {}, Step.ANALYZE_JOB: {"job_analysis": job},
            Step.MATCH_SKILLS: {"skill_match": match},
            Step.WRITE_RESUME: {"tailored_resume": TailoredResume(
                professional_summary=[claim], experience_bullets=[claim],
                highlighted_skills=[SupportedClaim(text="Python", evidence_ids=[FACT_ID])])},
            Step.VERIFY_RESUME: {"verification": VerificationResult(
                passed=True, unsupported_claims=[], revision_feedback=[]),
                "revision_feedback": []},
            Step.REVISE_RESUME: {"tailored_resume": state.tailored_resume},
        }
        return StepOutcome(updates=updates[step])


def sqlite_url(path): return f"sqlite:///{path.as_posix()}"


class LegacyState(TypedDict):
    approved: NotRequired[bool | None]
    revision_count: NotRequired[int]


def legacy_graph_builder():
    graph = StateGraph(LegacyState)
    def review(_):
        decision = interrupt({"question": "Approve?"})
        return {"approved": bool(decision["approved"]), "revision_count": 0}
    graph.add_node("review", review); graph.add_edge(START, "review"); graph.add_edge("review", END)
    return graph


def legacy_result(state):
    return {"approved": state.get("approved"), "revision_count": state.get("revision_count", 0)}


def test_migration_classifies_custom_state_and_leaves_unproven_unknown(tmp_path):
    url = sqlite_url(tmp_path / "migration.sqlite")
    config = Config("alembic.ini"); config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0003_tool_runtime")
    database = create_database(url)
    now = datetime.now(UTC).isoformat()
    with database.engine.begin() as connection:
        for run_id in ("custom-old", "unproven-old"):
            connection.exec_driver_sql("INSERT INTO runs (run_id,thread_id,status,resume_text,job_description,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, run_id, "awaiting_review", "Resume", "Job", now, now))
        connection.exec_driver_sql("INSERT INTO custom_agent_states (run_id,state_json,step,status,version,updated_at) VALUES (?,?,?,?,?,?)",
            ("custom-old", "{}", "human_review", "awaiting_review", 1, now))
    database.close(); command.upgrade(config, "head")
    database = create_database(url)
    try:
        repository = RunRepository(database.session_factory)
        assert repository.get("custom-old").backend == "custom"
        assert repository.get("custom-old").backend_source == "custom_state"
        assert repository.get("unproven-old").backend == "unknown"
    finally: database.close()


def test_checkpoint_classifier_marks_langgraph_and_refuses_ambiguity(tmp_path):
    database = create_database(sqlite_url(tmp_path / "runs.sqlite"), create_schema_for_tests=True)
    repository = RunRepository(database.session_factory)
    repository.create(run_id="legacy-lg", thread_id="thread-lg", resume_text="R", job_description="J",
        backend="unknown", backend_source="unclassified")
    repository.create(run_id="ambiguous", thread_id="thread-both", resume_text="R", job_description="J",
        backend="unknown", backend_source="unclassified")
    now = datetime.now(UTC).isoformat()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO custom_agent_states (run_id,state_json,step,status,version,updated_at) VALUES (?,?,?,?,?,?)",
            ("ambiguous", "{}", "human_review", "awaiting_review", 1, now))
    checkpoint = tmp_path / "checkpoints.sqlite"
    connection = sqlite3.connect(checkpoint)
    connection.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
    connection.executemany("INSERT INTO checkpoints VALUES (?,?,?)", [
        ("thread-lg", "", "1"), ("thread-both", "", "1")])
    connection.commit(); connection.close()
    report = classify_legacy_run_backends(database.session_factory, checkpoint)
    try:
        assert repository.get("legacy-lg").backend == "langgraph"
        assert repository.get("ambiguous").backend == "unknown"
        assert report.ambiguous == ("ambiguous",)
        assert (report.custom, report.langgraph, report.unknown) == (0, 1, 1)
    finally: database.close()


class RecordingBackend:
    def __init__(self, name): self.name = name; self.reviews = []
    async def review_run(self, run_id, request):
        self.reviews.append(run_id)
        return SimpleNamespace(run_id=run_id, status="approved", result=None, error=None)
    async def get_run(self, run_id): raise AssertionError("routing get should use projection")


def test_existing_runs_route_only_by_persisted_backend(monkeypatch, tmp_path):
    database = create_database(sqlite_url(tmp_path / "routing.sqlite"), create_schema_for_tests=True)
    repository = RunRepository(database.session_factory)
    repository.create(run_id="custom", thread_id="custom", resume_text="R", job_description="J", backend="custom")
    repository.create(run_id="legacy", thread_id="legacy", resume_text="R", job_description="J", backend="langgraph")
    repository.create(run_id="unknown", thread_id="unknown", resume_text="R", job_description="J", backend="unknown")
    custom = RecordingBackend("custom"); langgraph = RecordingBackend("langgraph")
    service = BackendRoutingRunService(repository=repository, custom=custom, langgraph=langgraph)
    monkeypatch.setenv("JOB_AGENT_BACKEND", "langgraph")
    request = ReviewRequest(approved=True)
    asyncio.run(service.review_run("custom", request)); asyncio.run(service.review_run("legacy", request))
    assert custom.reviews == ["custom"] and langgraph.reviews == ["legacy"]
    assert asyncio.run(service.get_run("unknown")).run_id == "unknown"
    with pytest.raises(InvalidRunStateError, match="unknown backend"):
        asyncio.run(service.review_run("unknown", request))
    database.close()


def test_api_runtime_always_creates_custom_runs(tmp_path):
    service = create_run_service(database_url=sqlite_url(tmp_path / "api.sqlite"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        custom_handler=CompleteFakeHandler())
    try:
        created = asyncio.run(service.create_run(CreateRunRequest(
            resume_text=FACT, job_description="Requires Python")))
        database = create_database(sqlite_url(tmp_path / "api.sqlite"))
        try:
            record = RunRepository(database.session_factory).get(created.run_id)
            assert created.status == "awaiting_review"
            assert record.backend == "custom"
            assert record.backend_source == "explicit_new_run"
        finally: database.close()
    finally: service.close()


def test_routing_service_resumes_persisted_langgraph_checkpoint(tmp_path):
    database_url = sqlite_url(tmp_path / "legacy-api.sqlite")
    checkpoint = tmp_path / "legacy-checkpoints.sqlite"
    frozen = create_frozen_langgraph_run_service(database_url=database_url,
        checkpoint_path=checkpoint, graph_builder=legacy_graph_builder(),
        result_serializer=legacy_result)
    created = asyncio.run(frozen.create_run(CreateRunRequest(
        resume_text="Legacy", job_description="Legacy job")))
    frozen.close()
    routed = create_run_service(database_url=database_url,
        checkpoint_path=checkpoint, graph_builder=legacy_graph_builder(),
        result_serializer=legacy_result, custom_handler=CompleteFakeHandler())
    try:
        approved = asyncio.run(routed.review_run(created.run_id,
            ReviewRequest(approved=True)))
        assert approved.status == "approved"
        database = create_database(database_url)
        try: assert RunRepository(database.session_factory).get(created.run_id).backend == "langgraph"
        finally: database.close()
    finally: routed.close()

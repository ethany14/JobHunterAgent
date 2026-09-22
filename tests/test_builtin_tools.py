from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_runtime import ToolCallRequest, ToolContext, ToolDataClassification, ToolExecutionStatus, ToolExecutor, ToolRegistry, ToolRiskLevel, ToolSideEffect
from agent_runtime.tools import SqlAlchemyRunReader, register_builtin_job_agent_tools
from agent_runtime.tools.run_reader import ReadRun
from agent_runtime.tools.schemas import PublicRunResult
from api.db import create_database
from api.repositories.run_repository import RunRepository
from job_agent.schemas import JobAnalysis, JobRequirement, MissingRequirement, ResumeAnalysis, ResumeEvidence, ScoreBreakdown, SkillEvidence, SkillMatch, SupportedClaim, TailoredResume, VerificationResult


def make_result(title: str, score: float, statuses: dict[str, str]) -> PublicRunResult:
    evidence_text = "Built Python APIs and analyzed data."
    evidence = ResumeEvidence(evidence_id="EXP-1", source_section="Experience", exact_text=evidence_text)
    requirements = []
    matches = []
    missing = []
    confirmations = []
    for index, (name, status) in enumerate(statuses.items(), start=1):
        requirement = JobRequirement(requirement_id=f"REQ-{index:03d}",
            requirement_group_id=f"GROUP-{index:03d}", canonical_name=name,
            display_name=name.title(), original_text=name, source_text=name,
            atomic_text=name, category="skill",
            verification_mode="user_confirmation" if status == "needs_confirmation" else "resume_evidence",
            level="required")
        requirements.append(requirement)
        matches.append(SkillEvidence(requirement_id=requirement.requirement_id,
            job_skill=name, requirement_level="required", match_status=status,
            match_reason=f"{status} deterministically",
            resume_evidence=[evidence_text] if status in {"matched", "partial"} else [],
            confidence=1 if status in {"matched", "partial"} else 0))
        if status in {"missing", "partial"}:
            missing.append(MissingRequirement(canonical_name=name, original_text=name))
        if status == "needs_confirmation":
            confirmations.append(requirement)
    claim = SupportedClaim(text="Built Python APIs.", evidence_ids=["EXP-1"])
    return PublicRunResult(
        resume_analysis=ResumeAnalysis(summary="Developer", skills=["Python"], evidence=[evidence], education=[]),
        job_analysis=JobAnalysis(title=title, summary="Role", requirements=requirements, responsibilities=[]),
        skill_match=SkillMatch(matches=matches, explanation="Deterministic", recommendations=[],
            missing_required_requirements=missing, missing_preferred_requirements=[],
            overall_score=score, score_breakdown=ScoreBreakdown(overall_score=score),
            confirmation_requirements=confirmations),
        tailored_resume=TailoredResume(professional_summary=[claim], experience_bullets=[claim],
            highlighted_skills=[SupportedClaim(text="Python", evidence_ids=["EXP-1"])]),
        verification=VerificationResult(passed=True, unsupported_claims=[], revision_feedback=[]),
        revision_feedback=[], revision_count=0, max_revisions=3)


class FakeRunReader:
    def __init__(self, runs: list[ReadRun]):
        self.runs = runs
    def list_recent_runs(self, limit: int) -> list[ReadRun]:
        return self.runs[:limit]
    def get_run(self, run_id: str) -> ReadRun | None:
        return next((run for run in self.runs if run.run_id == run_id), None)


@pytest.fixture
def builtin_runtime():
    now = datetime.now(UTC)
    runs = [
        ReadRun(run_id="run-1", status="awaiting_review",
            result=make_result("Backend Engineer", 62.5, {"python": "matched", "sql": "missing", "power bi": "partial", "work authorization": "needs_confirmation", "aws": "missing"}),
            created_at=now - timedelta(days=1), updated_at=now),
        ReadRun(run_id="run-2", status="approved",
            result=make_result("Data Engineer", 75, {"python": "matched", "sql": "missing", "power bi": "partial", "work authorization": "needs_confirmation", "tableau": "matched"}),
            created_at=now - timedelta(days=2), updated_at=now - timedelta(hours=1)),
    ]
    registry = register_builtin_job_agent_tools(ToolRegistry(), FakeRunReader(runs))
    executor = ToolExecutor(registry)
    context = ToolContext(task_id="tool-test", allowed_tools=frozenset({
        "list_recent_runs", "get_run_result", "compare_run_requirements", "render_tailored_resume"}))
    return registry, executor, context


def call(executor, context, name, arguments):
    record = executor.execute(ToolCallRequest(tool_name=name, arguments=arguments), context)
    assert record.status == ToolExecutionStatus.COMPLETED
    assert record.result.provenance
    assert all(item.source_type == "internal_database" for item in record.result.provenance)
    return record.result.output


def test_builtins_declare_complete_read_only_metadata(builtin_runtime):
    registry, _, _ = builtin_runtime
    for name in ("list_recent_runs", "get_run_result", "compare_run_requirements", "render_tailored_resume"):
        tool = registry.get(name)
        assert tool.version == "1.0"
        assert tool.risk_level == ToolRiskLevel.READ_ONLY
        assert tool.side_effect == ToolSideEffect.NONE
        assert tool.data_classification == ToolDataClassification.SENSITIVE
        assert tool.input_schema and tool.output_schema
        assert tool.timeout_seconds == 5
        assert tool.idempotent is True


def test_list_recent_runs_returns_only_safe_summary_fields(builtin_runtime):
    _, executor, context = builtin_runtime
    output = call(executor, context, "list_recent_runs", {"limit": 1})
    assert output["runs"][0]["job_title"] == "Backend Engineer"
    assert output["runs"][0]["missing_required_count"] == 3
    assert set(output["runs"][0]) == {"run_id", "status", "job_title", "match_score", "missing_required_count", "created_at", "updated_at"}
    assert "resume_text" not in str(output) and "job_description" not in str(output)


def test_get_run_result_returns_exact_public_projection(builtin_runtime):
    _, executor, context = builtin_runtime
    output = call(executor, context, "get_run_result", {"run_id": "run-1"})
    assert set(output) == set(PublicRunResult.model_fields)
    assert "resume_text" not in output and "job_description" not in output


def test_compare_requirements_is_deterministic_and_preserves_statuses(builtin_runtime):
    _, executor, context = builtin_runtime
    args = {"run_ids": ["run-1", "run-2"]}
    first = call(executor, context, "compare_run_requirements", args)
    second = call(executor, context, "compare_run_requirements", args)
    assert first == second
    assert first["common_required_requirements"] == ["power bi", "python", "sql", "work authorization"]
    assert first["common_missing_requirements"] == ["sql"]
    assert first["common_partial_requirements"] == ["power bi"]
    assert first["recurring_matched_requirements"] == ["python"]
    assert first["runs"][0]["unique_missing_requirements"] == ["aws"]
    assert first["runs"][0]["partial_requirements"] == ["power bi"]
    assert first["runs"][0]["confirmation_requirements"] == ["work authorization"]


def test_rendering_copies_existing_claim_text_without_strengthening(builtin_runtime):
    _, executor, context = builtin_runtime
    text_output = call(executor, context, "render_tailored_resume", {"run_id": "run-1", "format": "text"})
    markdown_output = call(executor, context, "render_tailored_resume", {"run_id": "run-1", "format": "markdown"})
    assert text_output["content"] == (
        "PROFESSIONAL SUMMARY\nBuilt Python APIs.\n\n"
        "EXPERIENCE\n• Built Python APIs.\n\nSKILLS\nPython"
    )
    assert "- Built Python APIs." in markdown_output["content"]
    assert "scalable" not in markdown_output["content"].lower()


def test_sqlalchemy_run_reader_and_builtins_do_not_expose_raw_sources(tmp_path):
    database = create_database(f"sqlite:///{(tmp_path / 'runs.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        repository = RunRepository(database.session_factory)
        repository.create(run_id="db-run", thread_id="db-run",
            resume_text="PRIVATE RESUME", job_description="PRIVATE JOB DESCRIPTION")
        repository.update("db-run", status="awaiting_review",
            result=make_result("Backend Engineer", 100, {"python": "matched"}).model_dump(mode="json"),
            error_message=None)
        registry = register_builtin_job_agent_tools(ToolRegistry(), SqlAlchemyRunReader(database.session_factory))
        executor = ToolExecutor(registry)
        context = ToolContext(run_id="tool-reader", allowed_tools=frozenset({"list_recent_runs", "get_run_result"}))
        listed = call(executor, context, "list_recent_runs", {"limit": 10})
        result = call(executor, context, "get_run_result", {"run_id": "db-run"})
        serialized = str({"listed": listed, "result": result})
        assert "PRIVATE RESUME" not in serialized
        assert "PRIVATE JOB DESCRIPTION" not in serialized
    finally:
        database.close()

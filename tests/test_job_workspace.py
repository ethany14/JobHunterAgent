from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionState
from agent_runtime.workspace.errors import (
    ArtifactValidationError, InvalidApplicationTransitionError,
    StaleApplicationError, WorkspaceAssociationError,
)
from agent_runtime.workspace.identity import normalize_job_url
from agent_runtime.workspace.repository import JobWorkspaceRepository
from agent_runtime.workspace.run_adapter import WorkspaceRunAdapter
from agent_runtime.workspace.types import ApplicationStatus, ArtifactStatus, ArtifactType
from api.db import create_database
from api.main import create_app
from api.repositories.run_repository import RunRepository
from api.schemas.runs import CreateRunResponse


def public_run_result():
    return {
        "job_analysis": {
            "title": "Engineer", "summary": "Role", "requirements": [],
            "responsibilities": [],
        },
        "skill_match": {
            "matches": [], "explanation": "No requirements.", "recommendations": [],
            "missing_required_requirements": [], "missing_preferred_requirements": [],
            "overall_score": 100, "score_breakdown": {"overall_score": 100},
            "confirmation_requirements": [],
        },
        "tailored_resume": {
            "professional_summary": [], "experience_bullets": [],
            "highlighted_skills": [],
        },
    }


class WorkspaceFakeRunService:
    def __init__(self, repository: RunRepository, *, failed: bool = False):
        self.repository = repository
        self.failed = failed
        self.calls = []

    async def create_run(self, request):
        run_id = f"workspace-run-{uuid4()}"
        self.calls.append(request)
        self.repository.create(run_id=run_id, thread_id=run_id,
            resume_text=request.resume_text, job_description=request.job_description)
        self.repository.update(run_id, status="failed" if self.failed else "awaiting_review",
            result=None if self.failed else public_run_result(),
            error_message="safe failure" if self.failed else None)
        return CreateRunResponse(run_id=run_id,
            status="failed" if self.failed else "awaiting_review")


def database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def workspace(tmp_path):
    database = create_database(database_url(tmp_path / "workspace.sqlite"), create_schema_for_tests=True)
    yield database, JobWorkspaceRepository(database.session_factory)
    database.close()


def make_job(repository, **updates):
    return repository.create_or_find_job(
        cleaned_job_description=updates.pop("description", "Requires Python and SQL."),
        company=updates.pop("company", "Acme"), title=updates.pop("title", "Engineer"),
        **updates,
    )


def test_url_normalization_and_duplicate_snapshot_rules(workspace):
    _, repository = workspace
    assert normalize_job_url("HTTPS://Example.COM/jobs/1?utm_source=x&id=7#top") == (
        "https://example.com/jobs/1?id=7"
    )
    with pytest.raises(ValueError): normalize_job_url("javascript:alert(1)")
    first = make_job(repository, canonical_url="https://EXAMPLE.com/jobs/1?utm_source=x&id=7")
    repeated = make_job(repository, canonical_url="https://example.com/jobs/1?id=7")
    changed = make_job(repository, canonical_url="https://example.com/jobs/1?id=7",
                       description="Requires Python, SQL, and Docker.")
    assert repeated.job.job_id == first.job.job_id
    assert repeated.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert repeated.snapshot_created is False
    assert changed.job.job_id == first.job.job_id
    assert changed.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert len(repository.get_job(first.job.job_id)[1]) == 2


def test_manual_fingerprint_reuse_and_uncertain_candidates(workspace):
    _, repository = workspace
    first = make_job(repository)
    exact = make_job(repository)
    uncertain = make_job(repository, description="A different posting.")
    assert exact.job.job_id == first.job.job_id
    assert uncertain.job.job_id != first.job.job_id
    assert first.job.job_id in uncertain.duplicate_candidates


def test_application_policy_versions_events_and_pagination(workspace):
    _, repository = workspace
    resolution = make_job(repository)
    app = repository.create_application(job_id=resolution.job.job_id,
                                        snapshot_id=resolution.snapshot.snapshot_id)
    assert app.status == ApplicationStatus.SAVED and app.version == 0
    analyzing = repository.transition_status(app.application_id,
        target_status=ApplicationStatus.ANALYZING, expected_version=0)
    needs = repository.transition_status(app.application_id,
        target_status=ApplicationStatus.NEEDS_EVIDENCE, expected_version=1)
    same = repository.transition_status(app.application_id,
        target_status=ApplicationStatus.NEEDS_EVIDENCE, expected_version=2)
    assert same.version == needs.version
    with pytest.raises(InvalidApplicationTransitionError):
        repository.transition_status(app.application_id,
            target_status=ApplicationStatus.APPLIED, expected_version=2)
    with pytest.raises(StaleApplicationError):
        repository.update_next_action(app.application_id, next_action="Apply",
                                      expected_version=0)
    updated = repository.update_next_action(app.application_id, next_action="Get evidence",
                                            expected_version=2)
    assert updated.version == 3
    assert [event.sequence for event in repository.list_events(app.application_id)] == [1, 2, 3, 4, 5]
    assert len(repository.list_applications(status=ApplicationStatus.NEEDS_EVIDENCE,
                                            company="acme", limit=10)) == 1


def test_application_update_and_event_insert_roll_back_together(workspace, monkeypatch):
    _, repository = workspace
    result = make_job(repository)
    app = repository.create_application(job_id=result.job.job_id, snapshot_id=result.snapshot.snapshot_id)
    original_events = repository.list_events(app.application_id)

    def fail_event(*args, **kwargs):
        raise RuntimeError("simulated event failure")

    monkeypatch.setattr(repository, "_event", fail_event)
    with pytest.raises(RuntimeError, match="simulated"):
        repository.update_next_action(app.application_id, next_action="Should roll back",
                                      expected_version=0)
    persisted = repository.get_application(app.application_id)
    assert persisted.version == 0
    assert persisted.next_action is None
    assert repository.list_events(app.application_id) == original_events


def test_applied_at_requirement(workspace):
    _, repository = workspace
    result = make_job(repository)
    app = repository.create_application(job_id=result.job.job_id, snapshot_id=result.snapshot.snapshot_id)
    for target in (ApplicationStatus.ANALYZING, ApplicationStatus.MATERIALS_READY,
                   ApplicationStatus.READY_TO_APPLY):
        app = repository.transition_status(app.application_id, target_status=target,
                                           expected_version=app.version)
    with pytest.raises(InvalidApplicationTransitionError):
        repository.transition_status(app.application_id, target_status=ApplicationStatus.APPLIED,
                                     expected_version=app.version)
    applied = repository.transition_status(app.application_id, target_status=ApplicationStatus.APPLIED,
        expected_version=app.version, applied_at=datetime.now(UTC))
    assert applied.applied_at is not None


def test_artifact_versions_validation_approval_and_run_projection(workspace):
    database, repository = workspace
    result = make_job(repository)
    app = repository.create_application(job_id=result.job.job_id, snapshot_id=result.snapshot.snapshot_id)
    first = repository.create_artifact(app.application_id,
        artifact_type=ArtifactType.COVER_LETTER, content={"text": "Draft one"}, evidence_ids=[],
        created_by="test", expected_version=0)
    repository.approve_artifact(first.artifact_id, expected_version=1)
    second = repository.create_artifact(app.application_id,
        artifact_type=ArtifactType.COVER_LETTER, content={"text": "Draft two"}, evidence_ids=[],
        created_by="test", expected_version=2)
    approved = repository.approve_artifact(second.artifact_id, expected_version=3)
    assert approved.status == ArtifactStatus.APPROVED
    assert repository.list_artifacts(app.application_id)[0].status == ArtifactStatus.SUPERSEDED
    with pytest.raises(ArtifactValidationError):
        repository.create_artifact(app.application_id, artifact_type=ArtifactType.JOB_ANALYSIS,
            content={"invalid": True}, evidence_ids=[], created_by="test", expected_version=4)
    assert repository.get_application(app.application_id).version == 4

    runs = RunRepository(database.session_factory)
    runs.create(run_id="run-workspace", thread_id="run-workspace", resume_text="Resume",
                job_description="JD")
    runs.update("run-workspace", status="awaiting_review", error_message=None, result={
        "job_analysis": {"title": "Engineer", "summary": "Role", "requirements": [], "responsibilities": []},
        "tailored_resume": {"professional_summary": [], "experience_bullets": [], "highlighted_skills": []},
    })
    adapter = WorkspaceRunAdapter(repository, runs)
    state, projected = adapter.attach_completed_run(app.application_id, "run-workspace", expected_version=4)
    replay_state, replay = adapter.attach_completed_run(app.application_id, "run-workspace",
                                                         expected_version=state.version)
    assert len(projected) == 2
    assert [item.artifact_id for item in replay] == [item.artifact_id for item in projected]
    assert replay_state.version == state.version


def test_associations_validate_foreign_keys_and_are_idempotent(workspace):
    database, repository = workspace
    result = make_job(repository)
    app = repository.create_application(job_id=result.job.job_id, snapshot_id=result.snapshot.snapshot_id)
    runs = RunRepository(database.session_factory)
    runs.create(run_id="attached-run", thread_id="attached-run", resume_text="r", job_description="j")
    attached = repository.attach_run(app.application_id, "attached-run", role="analysis", expected_version=0)
    duplicate = repository.attach_run(app.application_id, "attached-run", role="analysis",
                                      expected_version=attached.version)
    assert duplicate.version == attached.version
    with pytest.raises(WorkspaceAssociationError):
        repository.attach_session(app.application_id, "missing", role="assistant",
                                  expected_version=attached.version)
    with pytest.raises(IntegrityError):
        with database.session_factory.begin() as session:
            session.execute(text(
                "INSERT INTO application_runs(application_id,run_id,role,attached_at) "
                "VALUES ('missing-app','missing-run','test',CURRENT_TIMESTAMP)"
            ))


def test_workspace_api_contract_and_safe_errors(workspace):
    _, repository = workspace
    runtime = SimpleNamespace(workspace=repository)
    app = create_app(run_service=object(), session_runtime=runtime)
    with TestClient(app) as client:
        job_response = client.post("/api/jobs", json={
            "canonical_url": "https://example.com/job/7?utm_source=test",
            "company": "Acme", "title": "Engineer",
            "raw_page_text": "untrusted raw page text",
            "cleaned_job_description": "Requires Python.",
        })
        assert job_response.status_code == 201
        assert "raw_page_text" not in job_response.text
        body = job_response.json(); snapshot = body["snapshots"][0]
        created = client.post("/api/applications", json={
            "job_id": body["job"]["job_id"], "snapshot_id": snapshot["snapshot_id"]})
        assert created.status_code == 201
        application = created.json()
        stale = client.patch(f"/api/applications/{application['application_id']}/status",
            json={"target_status": "analyzing", "expected_version": 99})
        assert stale.status_code == 409
        changed = client.patch(f"/api/applications/{application['application_id']}/status",
            json={"target_status": "analyzing", "expected_version": 0})
        assert changed.status_code == 200
        listed = client.get("/api/applications?status=analyzing&company=Acme")
        assert len(listed.json()["applications"]) == 1
        assert client.get(f"/api/applications/{application['application_id']}/events").status_code == 200


def test_migration_upgrades_populated_database(tmp_path):
    path = tmp_path / "migration.sqlite"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url(path))
    command.upgrade(cfg, "0012_context_management")
    database = create_database(database_url(path))
    RunRepository(database.session_factory).create(run_id="old-run", thread_id="old-run",
        resume_text="old resume", job_description="old jd")
    SessionRepository(database.session_factory).create(
        SessionState(session_id="old-session", title="Old session"),
        SessionEvent(session_id="old-session", event_type=SessionEventType.SESSION_CREATED),
    )
    database.close()
    command.upgrade(cfg, "head")
    database = create_database(database_url(path))
    try:
        assert RunRepository(database.session_factory).get("old-run") is not None
        assert SessionRepository(database.session_factory).get("old-session") is not None
        with database.engine.connect() as connection:
            tables = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert {"jobs", "job_snapshots", "applications", "application_artifacts",
                "application_events", "application_runs", "application_sessions"} <= tables
    finally:
        database.close()


def test_save_workspace_is_idempotent_and_tracks_changed_snapshot(workspace):
    _, repository = workspace
    first = repository.save_workspace(source_url="https://example.com/jobs/42?utm_source=x",
        source_site="example.com", company="Acme", title="Engineer",
        cleaned_job_description="Requires Python.")
    repeated = repository.save_workspace(source_url="https://example.com/jobs/42",
        source_site="example.com", company="Acme", title="Senior Engineer",
        cleaned_job_description="Requires Python.")
    changed = repository.save_workspace(source_url="https://example.com/jobs/42",
        source_site="example.com", company="Acme", title="Engineer",
        cleaned_job_description="Requires Python and SQL.")
    assert repeated.application.application_id == first.application.application_id
    assert repeated.created_application is False and repeated.duplicate_detected is True
    assert repeated.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert repeated.job.title == "Senior Engineer"
    assert changed.application.application_id == first.application.application_id
    assert changed.snapshot.snapshot_id != first.snapshot.snapshot_id
    assert changed.application.current_snapshot_id == changed.snapshot.snapshot_id


def test_workspace_analysis_projects_artifacts_and_failure_is_retryable(workspace):
    database, repository = workspace
    saved = repository.save_workspace(cleaned_job_description="Requires Python.")
    service = WorkspaceFakeRunService(RunRepository(database.session_factory))
    app = create_app(run_service=service, session_runtime=SimpleNamespace(workspace=repository))
    with TestClient(app) as client:
        response = client.post(f"/api/applications/{saved.application.application_id}/analyze", json={
            "snapshot_id": saved.snapshot.snapshot_id,
            "resume_text": "Python developer.",
            "expected_version": saved.application.version,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["application"]["status"] == "materials_ready"
        assert {item["artifact_type"] for item in body["artifacts"]} == {
            "job_analysis", "match_report", "tailored_resume",
        }
        detail = client.get(f"/api/applications/{saved.application.application_id}").json()
        assert detail["associated_runs"][0]["status"] == "awaiting_review"

    failed_saved = repository.save_workspace(cleaned_job_description="Requires Go.")
    failed_service = WorkspaceFakeRunService(RunRepository(database.session_factory), failed=True)
    failed_app = create_app(run_service=failed_service,
                            session_runtime=SimpleNamespace(workspace=repository))
    with TestClient(failed_app) as client:
        response = client.post(
            f"/api/applications/{failed_saved.application.application_id}/analyze",
            json={"snapshot_id": failed_saved.snapshot.snapshot_id,
                  "resume_text": "Go developer.",
                  "expected_version": failed_saved.application.version},
        )
        assert response.status_code == 200
        failed = response.json()["application"]
        assert failed["status"] == "analysis_failed"
        assert failed["error_code"] == "analysis_run_failed"
        retry = client.post(
            f"/api/applications/{failed_saved.application.application_id}/analyze",
            json={"snapshot_id": failed_saved.snapshot.snapshot_id,
                  "resume_text": "Go developer.", "expected_version": failed["version"]},
        )
        assert retry.status_code == 200


def test_workspace_session_context_is_scoped_to_attached_application(workspace):
    database, repository = workspace
    one = repository.save_workspace(company="One", title="Python Engineer",
        cleaned_job_description="Python role.")
    two = repository.save_workspace(company="Two", title="Go Engineer",
        cleaned_job_description="Go role.")
    sessions = SessionRepository(database.session_factory)
    sessions.create(SessionState(session_id="workspace-session"),
        SessionEvent(session_id="workspace-session", event_type=SessionEventType.SESSION_CREATED))
    repository.attach_session(one.application.application_id, "workspace-session",
        role="workspace_assistant", expected_version=one.application.version)
    context = repository.workspace_context_for_session("workspace-session")
    assert len(context) == 2
    assert "Company: One" in context[0][1]
    assert "Company: Two" not in context[0][1]
    assert "Python role." in context[1][1]
    assert "Go role." not in "\n".join(content for _, content in context)
    assert context[1][0] == f"job-snapshot:{one.snapshot.snapshot_id}"
    assert repository.application_id_for_session("workspace-session") == one.application.application_id
    assert repository.workspace_context_for_session("unattached") == []


def test_workspace_context_excerpt_is_bounded_and_requirement_focused():
    text = "\n".join([
        "Role introduction",
        "Team overview",
        "Location information",
        "Company overview",
        "Unrelated marketing copy",
        "Requirements: five years of Python experience",
        "Preferred skills: PostgreSQL and Docker",
        "x" * 4_000,
    ])
    excerpt = JobWorkspaceRepository._compact_job_description(text)
    assert "Role introduction" in excerpt
    assert "five years of Python experience" in excerpt
    assert "PostgreSQL and Docker" in excerpt
    assert "Unrelated marketing copy" not in excerpt
    assert len(excerpt) <= 3_000

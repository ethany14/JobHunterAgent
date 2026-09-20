"""Career Evidence lifecycle, provenance, migrations and context boundaries."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from api.db import create_database, upgrade_database
from agent_runtime.evidence.errors import (
    EvidenceProvenanceError, InvalidEvidenceTransitionError, StaleEvidenceError,
)
from agent_runtime.evidence.models import CareerEvidenceRow, CareerEvidenceVersionRow
from agent_runtime.evidence.models import EvidenceApplicationLinkRow
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.evidence.retrieval import CareerEvidenceRetriever
from agent_runtime.evidence.types import EvidenceStatus
from job_agent.schemas import ResumeAnalysis, ResumeEvidence


@pytest.fixture
def repo(tmp_path):
    url = f"sqlite:///{tmp_path / 'vault.db'}"
    upgrade_database(url)
    db = create_database(url)
    try:
        yield CareerEvidenceRepository(db.session_factory)
    finally:
        db.close()


def candidate(repo, statement="Built Python APIs."):
    return repo.create_candidate(category="experience", claim_text=statement,
        source_type="user_attested", exact_quote=statement)


def test_lifecycle_and_immutable_versions(repo):
    first = candidate(repo)
    assert first.status == EvidenceStatus.CANDIDATE
    assert [e.event_type for e in repo.list_events(first.evidence_id)] == ["EVIDENCE_CREATED"]
    confirmed = repo.confirm(first.evidence_id, first.version)
    assert confirmed.status == EvidenceStatus.CONFIRMED
    with pytest.raises(StaleEvidenceError):
        repo.archive(first.evidence_id, first.version)
    proposed = repo.propose_revision(first.evidence_id,
        {"claim_text": "Built Python REST APIs.", "exact_quote": "Built Python REST APIs."},
        confirmed.version)
    assert proposed.status == EvidenceStatus.CANDIDATE
    assert proposed.current_version == 2
    assert repo.list_versions(first.evidence_id)[0].claim_text == "Built Python APIs."
    assert repo.confirm(first.evidence_id, proposed.version).status == EvidenceStatus.CONFIRMED
    archived = repo.archive(first.evidence_id, proposed.version + 1)
    assert archived.status == EvidenceStatus.ARCHIVED
    assert repo.restore(first.evidence_id, archived.version).status == EvidenceStatus.CANDIDATE
    assert [e.sequence for e in repo.list_events(first.evidence_id)] == list(range(1, 7))


def test_reject_and_invalid_transition(repo):
    first = candidate(repo)
    rejected = repo.reject(first.evidence_id, first.version, "incorrect")
    with pytest.raises(InvalidEvidenceTransitionError):
        repo.confirm(first.evidence_id, rejected.version)
    assert repo.restore(first.evidence_id, rejected.version).status == EvidenceStatus.CANDIDATE
    assert "incorrect" not in str(repo.list_events(first.evidence_id))


def test_resume_import_is_grounded_and_idempotent(repo):
    resume = "Built REST APIs in Python."
    analysis = ResumeAnalysis(summary="", skills=["Python"], education=[], evidence=[
        ResumeEvidence(evidence_id="EXP-001", source_section="Experience", exact_text=resume),
    ])
    first = repo.import_resume_evidence(analysis, resume, source_run_id="run-1")
    second = repo.import_resume_evidence(analysis, resume, source_run_id="run-2")
    assert first[0].evidence_id == second[0].evidence_id
    assert first[0].current.external_evidence_id == "EXP-001"
    assert first[0].status == EvidenceStatus.CONFIRMED
    assert [event.event_type for event in repo.list_events(first[0].evidence_id)] == [
        "EVIDENCE_CREATED", "SOURCE_VALIDATED", "CONFIRMED",
    ]
    with pytest.raises(EvidenceProvenanceError):
        repo.import_resume_evidence(analysis, "Did unrelated work.")


def test_no_claim_strengthening_or_invented_metric(repo):
    with pytest.raises(EvidenceProvenanceError):
        repo.create_candidate(category="leadership", source_type="user_attested",
            claim_text="Led three teammates", exact_quote="Worked with three teammates")
    with pytest.raises(EvidenceProvenanceError):
        repo.create_candidate(category="achievement", source_type="user_attested",
            claim_text="Increased revenue 30%", exact_quote="Increased revenue 30%",
            metrics=[{"value": "40", "unit": "%", "original_text": "40%"}])
    with pytest.raises(ValueError):
        repo.create_candidate(category="experience", source_type="job_description",
            claim_text="Python")


def test_retrieval_excludes_unconfirmed_and_respects_budget(repo):
    first = candidate(repo, "Built Python APIs.")
    candidate(repo, "Worked with Python tests.")
    repo.confirm(first.evidence_id, first.version)
    retriever = CareerEvidenceRetriever(repo)
    selected = retriever.retrieve(query="Python APIs", token_budget=500)
    assert [item.item.evidence_id for item in selected] == [first.evidence_id]
    assert retriever.retrieve(query="Python APIs", token_budget=1) == []
    assert retriever.retrieve(query="unrelated topic") == []


def test_supersede_requires_confirmed_replacement(repo):
    old = candidate(repo, "Built Python APIs.")
    old = repo.confirm(old.evidence_id, old.version)
    replacement = candidate(repo, "Built Python REST APIs.")
    with pytest.raises(InvalidEvidenceTransitionError):
        repo.supersede(old.evidence_id, replacement.evidence_id, old.version)
    replacement = repo.confirm(replacement.evidence_id, replacement.version)
    superseded = repo.supersede(old.evidence_id, replacement.evidence_id, old.version)
    assert superseded.status == EvidenceStatus.SUPERSEDED
    assert repo.list_versions(old.evidence_id)[0].claim_text == "Built Python APIs."


def test_foreign_keys_and_migration_on_populated_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'populated.db'}"
    upgrade_database(url)
    db = create_database(url)
    with db.session_factory.begin() as session:
        session.add(CareerEvidenceRow(
            evidence_id="existing", status="candidate", current_version=1,
            version=1, event_sequence=0, created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            updated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        ))
    upgrade_database(url)
    with db.session_factory() as session:
        assert session.get(CareerEvidenceRow, "existing") is not None
        assert session.execute(select(CareerEvidenceVersionRow)).all() == []
    db.close()


def test_context_snapshot_records_only_selected_evidence(tmp_path):
    from agent_runtime.context.projection import SessionContextProjector
    from agent_runtime.context.repository import ContextSnapshotRepository
    from agent_runtime.sessions import SessionEvent, SessionEventType, SessionMessageDraft, SessionRepository, SessionState, SessionStatus
    from agent_runtime.tools.messages import AgentMessage

    db = create_database(f"sqlite:///{tmp_path / 'context.db'}", create_schema_for_tests=True)
    try:
        repo = CareerEvidenceRepository(db.session_factory)
        matching = candidate(repo, "Built Python APIs.")
        repo.confirm(matching.evidence_id, matching.version)
        unrelated = candidate(repo, "Presented Tableau dashboards.")
        repo.confirm(unrelated.evidence_id, unrelated.version)
        pending = candidate(repo, "Built Python services.")
        sessions = SessionRepository(db.session_factory)
        snapshots = ContextSnapshotRepository(db.session_factory)
        state = SessionState(session_id="evidence-session", user_id="local-user",
                             status=SessionStatus.RUNNING)
        state = sessions.create(state,
            SessionEvent(session_id=state.session_id, event_type=SessionEventType.SESSION_CREATED),
            messages=[SessionMessageDraft(message=AgentMessage(
                message_id="user-evidence", role="user", content="Discuss my Python API experience"))])
        projector = SessionContextProjector(
            sessions=sessions, snapshots=snapshots, system_policy="Safe policy",
            evidence=repo, evidence_retriever=CareerEvidenceRetriever(repo),
        )
        projected = projector.prepare(state)
        assert [ref.evidence_id for ref in projected.snapshot.evidence_versions] == [matching.evidence_id]
        assert matching.current.claim_text in projected.messages[0].content
        assert pending.current.claim_text not in projected.messages[0].content
        assert unrelated.current.claim_text not in projected.messages[0].content
        rebuilt = projector.rebuild(state, projected.snapshot.snapshot_id)
        assert rebuilt.messages == projected.messages
        from agent_runtime.context.snapshots import ContextSnapshotUnavailableError
        current = repo.get(matching.evidence_id)
        repo.archive(matching.evidence_id, current.version)
        with pytest.raises(ContextSnapshotUnavailableError):
            projector.rebuild(state, projected.snapshot.snapshot_id)
    finally:
        db.close()


def test_application_link_requires_confirmation_and_preserves_evidence(tmp_path):
    from agent_runtime.workspace.repository import JobWorkspaceRepository
    from agent_runtime.workspace.models import ApplicationRow

    db = create_database(f"sqlite:///{tmp_path / 'links.db'}", create_schema_for_tests=True)
    try:
        repo = CareerEvidenceRepository(db.session_factory)
        workspace = JobWorkspaceRepository(db.session_factory)
        job = workspace.create_or_find_job(cleaned_job_description="Python engineer.",
                                           title="Engineer", company="Example")
        app = workspace.create_application(job_id=job.job.job_id,
                                           snapshot_id=job.snapshot.snapshot_id)
        item = candidate(repo)
        with pytest.raises(InvalidEvidenceTransitionError):
            repo.link_to_application(item.evidence_id, app.application_id,
                                     expected_version=item.version)
        item = repo.confirm(item.evidence_id, item.version)
        link = repo.link_to_application(item.evidence_id, app.application_id,
                                        expected_version=item.version)
        assert link.evidence_version_id == item.current.evidence_version_id
        assert len(repo.list_for_application(app.application_id)) == 1
        unrelated = candidate(repo, "Built Python services for another role.")
        repo.confirm(unrelated.evidence_id, unrelated.version)
        selected = CareerEvidenceRetriever(repo).retrieve(
            query="Python", application_id=app.application_id)
        assert [entry.item.evidence_id for entry in selected] == [item.evidence_id]
        archived = repo.archive(item.evidence_id, item.version + 1)
        assert archived.status == EvidenceStatus.ARCHIVED
        assert repo.list_for_application(app.application_id)[0].evidence_version_id == link.evidence_version_id
        assert CareerEvidenceRetriever(repo).retrieve(query="Python", application_id=app.application_id) == []
        with db.session_factory.begin() as session:
            session.delete(session.get(ApplicationRow, app.application_id))
        assert repo.get(item.evidence_id).status == EvidenceStatus.ARCHIVED
        assert repo.list_for_application(app.application_id) == []
    finally:
        db.close()


def test_transition_rolls_back_when_event_fails(repo, monkeypatch):
    item = candidate(repo)
    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event insert failed")
    monkeypatch.setattr(repo, "_event", fail_event)
    with pytest.raises(RuntimeError):
        repo.confirm(item.evidence_id, item.version)
    assert repo.get(item.evidence_id).status == EvidenceStatus.CANDIDATE
    assert repo.get(item.evidence_id).version == item.version


def test_sqlite_rejects_orphan_application_link(tmp_path):
    from datetime import UTC, datetime
    db = create_database(f"sqlite:///{tmp_path / 'foreign-key.db'}", create_schema_for_tests=True)
    try:
        with pytest.raises(IntegrityError):
            with db.session_factory.begin() as session:
                session.add(EvidenceApplicationLinkRow(
                    link_id="orphan", evidence_id="missing",
                    evidence_version_id="missing", application_id="missing",
                    link_type="supports", created_at=datetime.now(UTC),
                    created_by="test",
                ))
    finally:
        db.close()


def test_migration_enforces_immutable_versions_and_events(repo):
    item = candidate(repo)
    with pytest.raises(IntegrityError):
        with repo._sessions.begin() as session:
            session.execute(text("UPDATE career_evidence_versions SET claim_text='changed' WHERE evidence_id=:id"), {"id": item.evidence_id})
    with pytest.raises(IntegrityError):
        with repo._sessions.begin() as session:
            session.execute(text("DELETE FROM career_evidence_events WHERE evidence_id=:id"), {"id": item.evidence_id})
    assert repo.get(item.evidence_id).current.claim_text == "Built Python APIs."


def test_upgrade_from_populated_0014_database_preserves_runs(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from api.models import Run

    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0014_workspace_failure")
    db = create_database(url)
    with db.session_factory.begin() as session:
        session.add(Run(
            run_id="legacy-run", thread_id="legacy-run", status="approved",
            backend="langgraph", backend_source="legacy", resume_text="Original resume",
            job_description="Original JD",
        ))
    db.close()
    command.upgrade(config, "head")
    db = create_database(url)
    try:
        with db.session_factory() as session:
            old = session.get(Run, "legacy-run")
            assert old is not None
            assert old.resume_text == "Original resume"
            assert old.backend == "langgraph"
        assert CareerEvidenceRepository(db.session_factory).list() == []
    finally:
        db.close()

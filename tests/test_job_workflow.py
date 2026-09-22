"""Opt-in analysis graph contracts; no paid model calls."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from agent_runtime.job_workflow.plan import (
    JOB_WORKFLOW_CONTEXT_TOKEN_BUDGET, build_analysis_plan,
)
from agent_runtime.job_workflow.workers import (
    ArtifactRevisionWorker, ArtifactVerifierWorker, ArtifactWriterWorker,
    AnalysisProjectionWorker, CandidateAnalysisWorker, EvidenceFreezeWorker, EvidenceGapWorker,
    JobAnalysisWorker, PackAssemblerWorker, RequirementMatchWorker,
    SourceProvisionWorker,
)
from agent_runtime.application_pack.repository import PackRepository
from agent_runtime.application_pack.workflow import ApplicationPackWorkflow
from agent_runtime.application_pack.types import (
    ApplicationAnswer, CoverLetter, CoverLetterParagraph, GroundedBlock,
)
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.interviewer.repository import InterviewRepository
from agent_runtime.memory.repository import MemoryRepository
from agent_runtime.multi_agent.context import AgentContextPolicy
from agent_runtime.multi_agent.errors import WorkerStepError
from agent_runtime.multi_agent.policy import AgentPlanValidator
from agent_runtime.multi_agent.registry import AgentWorkerRegistry
from agent_runtime.multi_agent.repository import AgentTaskRepository
from agent_runtime.multi_agent.scheduler import AgentTaskScheduler
from agent_runtime.multi_agent.types import TaskStatus
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionState
from api.db import create_database, upgrade_database
from agent_runtime.workspace.repository import JobWorkspaceRepository
from job_agent.schemas import (
    JobAnalysis, JobRequirement, ResumeAnalysis, ResumeEvidence, SkillAssessment,
    SkillEvidence, SupportedClaim, TailoredResume, VerificationResult,
)
from custom_agent.handlers import JobAgentStepHandler


class FakeSource:
    def __init__(self, application_id, job_description="Requires Python APIs."):
        self.application_id = application_id
        self.job_description = job_description

    def read_sources(self, application_id, *, run_id=None, snapshot_id=None):
        assert application_id == self.application_id
        return "Built Python APIs.", self.job_description


def fake_analyze(schema, system_message, human_message):
    if schema is ResumeAnalysis:
        assert "Requires Python" not in human_message
        return ResumeAnalysis(summary="Python APIs", skills=["Python"],
            evidence=[ResumeEvidence(evidence_id="temporary", source_section="Experience",
                                     exact_text="Built Python APIs.")], education=[])
    if schema is JobAnalysis:
        assert "Built Python" not in human_message
        return JobAnalysis(title="Engineer", summary="", requirements=[JobRequirement(
            requirement_id="temporary", requirement_group_id="temporary",
            canonical_name="python", display_name="Python",
            original_text="Python APIs", source_text="Python APIs", atomic_text="Python APIs",
            category="skill", verification_mode="resume_evidence", level="required")],
            responsibilities=[])
    if schema is SkillAssessment:
        return SkillAssessment(matches=[SkillEvidence(requirement_id="none",
            job_skill="python", requirement_level="required", match_status="matched",
            resume_evidence=["Built Python APIs."], confidence=0.9)],
            explanation="Grounded match.", recommendations=[])
    raise AssertionError(f"Unexpected schema {schema}")


def test_candidate_retries_ungrounded_quote_once_without_importing_it():
    calls = []

    def analyzer(schema, system_message, human_message):
        calls.append(schema)
        quote = ("Built scalable Python APIs." if len(calls) == 1
                 else "Built Python APIs.")
        return ResumeAnalysis(summary="Python APIs", skills=["Python"],
            evidence=[ResumeEvidence(evidence_id="temporary",
                source_section="Experience", exact_text=quote)], education=[])

    worker = CandidateAnalysisWorker(JobAgentStepHandler(analyzer=analyzer))
    result = worker.execute(SimpleNamespace(task_id="candidate"),
        SimpleNamespace(input_artifacts={"source": {"kind": "resume_source",
            "data": {"resume_text": "Built Python APIs."}}}))
    assert calls == [ResumeAnalysis, ResumeAnalysis]
    assert result.output_artifacts[0].content["data"]["evidence"][0]["exact_text"] == "Built Python APIs."


def test_candidate_rejects_quote_not_in_original_resume_after_bounded_retry():
    calls = []

    def analyzer(schema, system_message, human_message):
        calls.append(schema)
        return ResumeAnalysis(summary="Python APIs", skills=["Python"],
            evidence=[ResumeEvidence(evidence_id="temporary",
                source_section="Experience", exact_text="Built scalable Python APIs.")],
            education=[])

    worker = CandidateAnalysisWorker(JobAgentStepHandler(analyzer=analyzer))
    with pytest.raises(WorkerStepError) as raised:
        worker.execute(SimpleNamespace(task_id="candidate"),
            SimpleNamespace(input_artifacts={"source": {"kind": "resume_source",
                "data": {"resume_text": "Built Python APIs."}}}))
    assert raised.value.code == "resume_evidence_invalid"
    assert len(calls) == 2


def test_isolated_parallel_analysis_and_persisted_mode(tmp_path):
    url = f"sqlite:///{(tmp_path / 'workflow.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    scheduler = None
    try:
        sessions = SessionRepository(db.session_factory)
        sessions.create(SessionState(session_id="parent"), SessionEvent(
            session_id="parent", event_type=SessionEventType.SESSION_CREATED))
        workspace = JobWorkspaceRepository(db.session_factory)
        application = workspace.save_workspace(cleaned_job_description="Requires Python APIs.",
            title="Engineer", company="Example").application
        handler = JobAgentStepHandler(analyzer=fake_analyze)
        workers = AgentWorkerRegistry()
        for worker in (SourceProvisionWorker(FakeSource(application.application_id)), CandidateAnalysisWorker(handler),
                       JobAnalysisWorker(handler), RequirementMatchWorker(handler),
                       EvidenceGapWorker()):
            workers.register(worker)
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()})
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
                                       workers=workers, contexts=contexts)
        plan = build_analysis_plan(application_id=application.application_id,
            parent_session_id="parent", idempotency_key="analysis-1")
        depths = AgentPlanValidator(worker_types=workers.names(),
            registered_tools=frozenset(), parent_tools=frozenset(),
            session_tools=frozenset()).validate(plan)
        # A real Application row is needed for the persisted task FK.
        assert plan.workflow_mode == "multi_agent_v1"
        assert plan.tasks[1].input_from_tasks == {"resume_source": "source"}
        assert plan.tasks[2].input_from_tasks == {"job_source": "source"}
        assert depths["candidate"] == depths["job"] == 1
        root = tasks.create_plan(plan, depths)
        assert scheduler.run_one(root.task_id).status == TaskStatus.SUCCEEDED
        by_key = {task.idempotency_key: task for task in tasks.children(root.task_id)}
        assert by_key["candidate"].status == by_key["job"].status == TaskStatus.READY
        assert by_key["match"].status == TaskStatus.BLOCKED
        assert scheduler.run_one(by_key["candidate"].task_id).status == TaskStatus.SUCCEEDED
        assert tasks.require(by_key["match"].task_id).status == TaskStatus.BLOCKED
        assert scheduler.run_one(by_key["job"].task_id).status == TaskStatus.SUCCEEDED
        assert tasks.require(by_key["match"].task_id).status == TaskStatus.READY
        assert scheduler.run_one(by_key["match"].task_id).status == TaskStatus.SUCCEEDED
        assert scheduler.run_one(by_key["gap"].task_id).status == TaskStatus.SUCCEEDED
        from sqlalchemy import text
        with db.session_factory() as session:
            modes = session.execute(text("SELECT DISTINCT workflow_mode FROM agent_task_artifacts")).scalars().all()
            assert modes == ["multi_agent_v1"]
    finally:
        if scheduler is not None:
            scheduler.stop()
        db.close()


def test_job_analysis_context_budget_handles_realistic_jd_and_reports_excess(tmp_path):
    from agent_runtime.context.budget import estimate_tokens

    url = f"sqlite:///{(tmp_path / 'long-job.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    scheduler = None
    try:
        sessions = SessionRepository(db.session_factory)
        sessions.create(SessionState(session_id="parent"), SessionEvent(
            session_id="parent", event_type=SessionEventType.SESSION_CREATED))
        jd = "Requires Python APIs. " * 650
        workspace = JobWorkspaceRepository(db.session_factory)
        application = workspace.save_workspace(
            cleaned_job_description=jd, title="Engineer", company="Example"
        ).application
        workers = AgentWorkerRegistry()
        workers.register(SourceProvisionWorker(FakeSource(application.application_id, jd)))
        workers.register(JobAnalysisWorker(JobAgentStepHandler(analyzer=fake_analyze)))
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()})
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
                                       workers=workers, contexts=contexts)

        def run(key, *, job_budget=None):
            plan = build_analysis_plan(application_id=application.application_id,
                parent_session_id="parent", idempotency_key=key)
            if job_budget is not None:
                plan.tasks[2].token_budget = job_budget
            depths = AgentPlanValidator(worker_types=workers.names() | {
                "candidate_analysis", "requirement_match", "evidence_gap"},
                registered_tools=frozenset(), parent_tools=frozenset(),
                session_tools=frozenset()).validate(plan)
            root = tasks.create_plan(plan, depths)
            assert scheduler.run_one(root.task_id).status == TaskStatus.SUCCEEDED
            job = next(task for task in tasks.children(root.task_id)
                       if task.task_type == "job_analysis")
            return scheduler.run_one(job.task_id)

        source = {"kind": "job_source", "data": {"job_description": jd}}
        estimate = estimate_tokens({"artifacts": {"job": source},
            "parent_messages": [], "memory": [], "evidence": [],
            "skill_procedures": []})
        assert 4_000 < estimate < JOB_WORKFLOW_CONTEXT_TOKEN_BUDGET
        assert run("long-job").status == TaskStatus.SUCCEEDED
        failed = run("too-small", job_budget=100)
        assert failed.status == TaskStatus.FAILED
        assert failed.error_code == "context_budget_exceeded"
        assert failed.started_at is None
    finally:
        if scheduler is not None:
            scheduler.stop()
        db.close()


def test_existing_pack_mode_defaults_to_standard(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mode.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    try:
        from sqlalchemy import text
        with db.session_factory() as session:
            for table in ("application_packs", "application_artifacts", "agent_plans", "agent_task_artifacts"):
                columns = session.execute(text(f"PRAGMA table_info({table})")).all()
                assert "workflow_mode" in [column[1] for column in columns]
    finally:
        db.close()


def test_multi_agent_panel_uses_safe_text_and_server_owned_plan():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "web_app/index.html").read_text(encoding="utf-8")
    panel = (root / "web_app/app.js").read_text(encoding="utf-8")
    controller = (root / "chrome_extension/multi-agent-controller.js").read_text(encoding="utf-8")
    client = (root / "web_app/api.js").read_text(encoding="utf-8")
    assert 'value="single_custom"' not in page
    assert 'value="multi_agent_v1"' not in page
    assert "renderMultiAgentProgress" in controller
    assert "textContent" in controller and "innerHTML" not in controller
    assert "innerHTML" not in panel
    assert "/multi-agent-runs" in client
    assert "application_questions" in controller


def test_component_gate_uses_shared_dataset_without_model_calls():
    from evals.run_job_workflow_component_evals import run
    result = run()
    assert result["run_metadata"]["model_calls"] == 0
    assert result["single_custom_shared_safety"] == result["multi_agent_v1_shared_safety"]
    assert result["plan_checks"]["required_dependencies_present"]
    assert result["plan_checks"]["all_worker_tool_allowlists_empty"]
    assert "full_workflow_parity" in result["not_measured"]


def test_pack_plan_restricts_source_visibility_and_tools():
    from agent_runtime.job_workflow.plan import build_pack_plan
    plan = build_pack_plan(application_id="app", parent_session_id="parent",
        idempotency_key="scope-test", job_snapshot_id="snapshot",
        requested_artifacts=frozenset({"tailored_resume", "cover_letter"}),
        include_interview=True, max_revisions=1)
    tasks = {task.key: task for task in plan.tasks}
    assert tasks["candidate"].input_from_tasks == {"resume_source": "source"}
    assert tasks["job"].input_from_tasks == {"job_source": "source"}
    assert tasks["match"].input_from_tasks == {
        "candidate_profile": "candidate", "job_analysis": "job"}
    assert tasks["interview"].input_from_tasks == {"evidence_gap_plan": "gap"}
    assert tasks["freeze"].input_spec["job_snapshot_id"] == "snapshot"
    for key in ("resume_writer", "cover_writer"):
        assert set(tasks[key].input_from_tasks) == {
            "generation_evidence_snapshot", "job_analysis", "match_report"}
    for key in ("resume_verify_0", "cover_verify_0"):
        assert "resume_source" not in tasks[key].input_from_tasks
        assert "job_source" not in tasks[key].input_from_tasks
    assert "generation_evidence_snapshot" in tasks["assemble"].input_from_tasks
    assert "resume_source" not in tasks["assemble"].input_from_tasks
    assert "job_source" not in tasks["assemble"].input_from_tasks
    assert all(not task.allowed_tools and task.recent_parent_message_limit == 0
               for task in tasks.values())


def test_freeze_rejects_changed_job_snapshot_before_selecting_evidence():
    from types import SimpleNamespace
    worker = EvidenceFreezeWorker(workspace=SimpleNamespace(
        get_application=lambda application_id: SimpleNamespace(current_snapshot_id="new")),
        evidence=None, pack_workflow=None)
    task = SimpleNamespace(application_id="app", input_spec={"job_snapshot_id": "old"})
    context = SimpleNamespace(input_artifacts={"job": {"kind": "job_analysis",
        "data": JobAnalysis(title="Engineer", summary="", requirements=[],
                            responsibilities=[]).model_dump(mode="json")}})
    with pytest.raises(ValueError, match="changed before evidence freezing"):
        worker.execute(task, context)


class FakePackModel:
    fail_cover = False
    bad_first = False
    def resume(self, state, preferences):
        evidence = state.resume_analysis.evidence[0]
        if self.bad_first:
            return TailoredResume(professional_summary=[SupportedClaim(
                text="Increased revenue by 30%.", evidence_ids=[evidence.evidence_id])],
                experience_bullets=[], highlighted_skills=[])
        return TailoredResume(professional_summary=[SupportedClaim(
            text=evidence.exact_text, evidence_ids=[evidence.evidence_id])],
            experience_bullets=[], highlighted_skills=[])

    def verify_resume(self, state):
        return VerificationResult(passed=True, unsupported_claims=[], revision_feedback=[])

    def revise_resume(self, state, preferences):
        original = self.bad_first
        self.bad_first = False
        try:
            return self.resume(state, preferences)
        finally:
            self.bad_first = original

    def cover_letter(self, content):
        if self.fail_cover:
            raise RuntimeError("Synthetic optional cover-letter failure")
        import json
        evidence = json.loads(content)["evidence"][0]
        parsed = json.loads(content)
        return CoverLetter(paragraphs=[
            CoverLetterParagraph(paragraph_type="opening",
                text=f"I am applying for the {parsed['title']} role."),
            CoverLetterParagraph(paragraph_type="evidence",
                text=f"One relevant example is: {evidence['claim_text']}",
                evidence_ids=[evidence["evidence_id"]],
                evidence_version_ids=[evidence["evidence_version_id"]]),
            CoverLetterParagraph(paragraph_type="motivation",
                text="I would welcome the opportunity to contribute to this work."),
        ])

    def application_answer(self, content):
        import json
        parsed = json.loads(content)
        evidence = parsed["evidence"][0]
        text = f"My relevant experience includes the following: {evidence['claim_text']}"
        block = GroundedBlock(block_id="fact-1", text=text, block_type="factual",
            evidence_ids=[evidence["evidence_id"]],
            evidence_version_ids=[evidence["evidence_version_id"]])
        return ApplicationAnswer(question=parsed["question"], answer_blocks=[block],
            character_count=len(text), word_count=len(text.split()))

    def revise_blocks(self, artifact_type, context):
        raise AssertionError("No revision expected for grounded fake output")


def test_full_no_interview_pack_flow_uses_frozen_evidence_and_public_schemas(tmp_path):
    from agent_runtime.job_workflow.plan import build_pack_plan
    url = f"sqlite:///{(tmp_path / 'pack-flow.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    scheduler = None
    try:
        workspace = JobWorkspaceRepository(db.session_factory)
        application = workspace.save_workspace(cleaned_job_description="Requires Python APIs.",
            title="Engineer", company="Example").application
        evidence = CareerEvidenceRepository(db.session_factory)
        candidate = evidence.create_candidate(category="experience", claim_text="Built Python APIs.",
            source_type="user_attested", exact_quote="Built Python APIs.")
        confirmed = evidence.confirm(candidate.evidence_id, candidate.version)
        evidence.link_to_application(confirmed.evidence_id, application.application_id,
            expected_version=confirmed.version)
        packs = PackRepository(db.session_factory)
        model = FakePackModel()
        pack_workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace,
            evidence=evidence, memories=MemoryRepository(db.session_factory), model=model)
        sessions = SessionRepository(db.session_factory)
        sessions.create(SessionState(session_id="parent"), SessionEvent(
            session_id="parent", event_type=SessionEventType.SESSION_CREATED))
        handler = JobAgentStepHandler(analyzer=fake_analyze)
        workers = AgentWorkerRegistry()
        for worker in (SourceProvisionWorker(FakeSource(application.application_id)),
                       CandidateAnalysisWorker(handler, evidence=evidence), JobAnalysisWorker(handler),
                       RequirementMatchWorker(handler),
                       EvidenceGapWorker(InterviewRepository(db.session_factory)),
                       AnalysisProjectionWorker(workspace),
                       EvidenceFreezeWorker(workspace=workspace, evidence=evidence,
                           pack_workflow=pack_workflow),
                       ArtifactWriterWorker(model), ArtifactVerifierWorker(model=model),
                       ArtifactRevisionWorker(model),
                       PackAssemblerWorker(packs=packs, workspace=workspace,
                                           pack_workflow=pack_workflow)):
            workers.register(worker)
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()},
            evidence=evidence)
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
                                       workers=workers, contexts=contexts)
        plan = build_pack_plan(application_id=application.application_id,
            parent_session_id="parent", idempotency_key="multi-pack-1",
            job_snapshot_id=application.current_snapshot_id,
            requested_artifacts=frozenset({"tailored_resume", "cover_letter", "application_answer"}),
            application_questions=("What experience do you have with Python?",),
            company="Example", title="Engineer", max_revisions=3)
        depths = AgentPlanValidator(worker_types=workers.names(),
            registered_tools=frozenset(), parent_tools=frozenset(),
            session_tools=frozenset(), task_type_tools=contexts.task_type_tools).validate(plan)
        root = tasks.create_plan(plan, depths)
        for _ in range(len(plan.tasks)):
            ready = tasks.list_ready(limit=20)
            if not ready:
                break
            result = scheduler.run_one(ready[0].task_id)
            assert result.status == TaskStatus.SUCCEEDED, (result.task_type, result.error_code)
        all_tasks = [root, *tasks.children(root.task_id)]
        assert len(all_tasks) == len(plan.tasks)
        assert all(tasks.require(item.task_id).status == TaskStatus.SUCCEEDED for item in all_tasks)
        pack = packs.list_for_application(application.application_id)[0]
        assert pack.workflow_mode == "multi_agent_v1"
        items = packs.items(pack.pack_id)
        assert len(items) == 3
        assert all(item.verification and item.verification.passed for item in items)
        assert {item.artifact_type for item in items} == {
            "tailored_resume", "cover_letter", "application_answer"}
        assert packs.snapshot(pack.pack_id).items[0].evidence_version_id == confirmed.current.evidence_version_id
        from agent_runtime.multi_agent.models import AgentTaskAttemptRow
        from agent_runtime.context.models import ContextSnapshotRow
        from sqlalchemy import select
        with db.session_factory() as session:
            writer_task = next(item for item in tasks.children(root.task_id)
                               if item.task_type == "artifact_writer")
            attempt = session.scalar(select(AgentTaskAttemptRow).where(
                AgentTaskAttemptRow.task_id == writer_task.task_id))
            manifest = json.loads(session.get(ContextSnapshotRow,
                attempt.context_snapshot_id).manifest_json)
        assert {item["version_id"] for item in manifest["frozen_evidence"]} >= {
            confirmed.current.evidence_version_id}
        assert {item.workflow_mode for item in workspace.list_artifacts(application.application_id)} == {
            "multi_agent_v1"}
        assert len(InterviewRepository(db.session_factory).assessments(
            application.application_id, application.current_snapshot_id)) == 1
        model.fail_cover = True
        optional_plan = build_pack_plan(application_id=application.application_id,
            parent_session_id="parent", idempotency_key="optional-failure",
            job_snapshot_id=application.current_snapshot_id,
            requested_artifacts=frozenset({"tailored_resume", "cover_letter"}),
            max_revisions=0)
        optional_depths = AgentPlanValidator(worker_types=workers.names(),
            registered_tools=frozenset(), parent_tools=frozenset(),
            session_tools=frozenset(), task_type_tools=contexts.task_type_tools).validate(optional_plan)
        optional_root = tasks.create_plan(optional_plan, optional_depths)
        for _ in range(len(optional_plan.tasks)):
            ready = tasks.list_ready(limit=50)
            if not ready:
                break
            scheduler.run_one(ready[0].task_id)
        optional_children = tasks.children(optional_root.task_id)
        assert next(item for item in optional_children if item.task_type == "pack_assembler").status == TaskStatus.SUCCEEDED
        assert any(item.task_type == "artifact_writer" and item.status == TaskStatus.FAILED
                   for item in optional_children)
        latest_pack = packs.list_for_application(application.application_id)[0]
        assert len(packs.items(latest_pack.pack_id)) == 1
        assert packs.items(latest_pack.pack_id)[0].artifact_type == "tailored_resume"
        model.fail_cover = False
        model.bad_first = True
        revision_plan = build_pack_plan(application_id=application.application_id,
            parent_session_id="parent", idempotency_key="failed-then-revised",
            job_snapshot_id=application.current_snapshot_id,
            requested_artifacts=frozenset({"tailored_resume"}), max_revisions=1)
        revision_depths = AgentPlanValidator(worker_types=workers.names(),
            registered_tools=frozenset(), parent_tools=frozenset(),
            session_tools=frozenset(), task_type_tools=contexts.task_type_tools).validate(revision_plan)
        revision_root = tasks.create_plan(revision_plan, revision_depths)
        for _ in range(len(revision_plan.tasks)):
            ready = tasks.list_ready(limit=50)
            if not ready:
                break
            completed = scheduler.run_one(ready[0].task_id)
            assert completed.status == TaskStatus.SUCCEEDED, (completed.task_type, completed.error_code)
        revision_tasks = {item.idempotency_key: item for item in tasks.children(revision_root.task_id)}
        first_report = next(link for link in tasks.artifact_links(revision_tasks["resume_verify_0"].task_id)
                            if link["direction"] == "output")
        final_report = next(link for link in tasks.artifact_links(revision_tasks["resume_verify_1"].task_id)
                            if link["direction"] == "output")
        from agent_runtime.multi_agent.models import AgentTaskArtifactRow
        with db.session_factory() as session:
            first_content = json.loads(session.get(AgentTaskArtifactRow,
                                                   first_report["artifact_id"]).content_json)
            final_content = json.loads(session.get(AgentTaskArtifactRow,
                                                   final_report["artifact_id"]).content_json)
        assert first_content["data"]["verification"]["passed"] is False
        assert final_content["data"]["verification"]["passed"] is True
        revised_pack = packs.list_for_application(application.application_id)[0]
        assert packs.items(revised_pack.pack_id)[0].verification.passed
        assert "30%" not in json.dumps(packs.artifact(revised_pack.pack_id,
                                                   packs.items(revised_pack.pack_id)[0].pack_item_id))
        stale_plan = build_pack_plan(application_id=application.application_id,
            parent_session_id="parent", idempotency_key="changed-after-freeze",
            job_snapshot_id=application.current_snapshot_id,
            requested_artifacts=frozenset({"tailored_resume"}), max_revisions=0)
        stale_depths = AgentPlanValidator(worker_types=workers.names(),
            registered_tools=frozenset(), parent_tools=frozenset(),
            session_tools=frozenset(), task_type_tools=contexts.task_type_tools).validate(stale_plan)
        stale_root = tasks.create_plan(stale_plan, stale_depths)
        while True:
            ready = tasks.list_ready(limit=50)
            completed = scheduler.run_one(ready[0].task_id)
            assert completed.status == TaskStatus.SUCCEEDED
            if completed.task_type == "freeze_evidence":
                break
        newer = evidence.create_candidate(category="experience",
            claim_text="Built Python REST APIs.", source_type="user_attested",
            exact_quote="Built Python REST APIs.")
        newer = evidence.confirm(newer.evidence_id, newer.version)
        evidence.link_to_application(newer.evidence_id, application.application_id,
            expected_version=newer.version)
        for _ in range(len(stale_plan.tasks)):
            ready = tasks.list_ready(limit=50)
            if not ready:
                break
            scheduler.run_one(ready[0].task_id)
        stale_assembler = next(item for item in tasks.children(stale_root.task_id)
                               if item.task_type == "pack_assembler")
        assert tasks.require(stale_assembler.task_id).status == TaskStatus.FAILED
        assert len(packs.list_for_application(application.application_id)) == 3
    finally:
        if scheduler is not None:
            scheduler.stop()
        db.close()


def test_multi_agent_api_is_opt_in_idempotent_and_versioned(tmp_path):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from agent_runtime.job_workflow.workers import ApplicationSourceReader
    from api.main import create_app
    from api.models import Run
    from api.session_dependencies import SessionRuntime
    from agent_runtime.registry import ToolRegistry

    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    scheduler = None
    try:
        workspace = JobWorkspaceRepository(db.session_factory)
        application = workspace.save_workspace(cleaned_job_description="Requires Python APIs.",
            title="Engineer", company="Example").application
        run_id = str(uuid4())
        with db.session_factory.begin() as session:
            session.add(Run(run_id=run_id, thread_id=run_id, status="approved",
                backend="custom", backend_source="test", resume_text="Built Python APIs.",
                job_description="Requires Python APIs.", result_json=json.dumps({
                    "resume_analysis": ResumeAnalysis(
                    summary="", skills=["Python"], evidence=[ResumeEvidence(
                        evidence_id="E-1", source_section="Experience",
                        exact_text="Built Python APIs.")], education=[]).model_dump(mode="json")})))
        application = workspace.attach_run(application.application_id, run_id,
            role="analysis", expected_version=application.version)
        sessions = SessionRepository(db.session_factory)
        packs = PackRepository(db.session_factory)
        evidence = CareerEvidenceRepository(db.session_factory)
        candidate = evidence.create_candidate(category="experience", claim_text="Built Python APIs.",
            source_type="user_attested", exact_quote="Built Python APIs.")
        confirmed = evidence.confirm(candidate.evidence_id, candidate.version)
        evidence.link_to_application(confirmed.evidence_id, application.application_id,
            expected_version=confirmed.version)
        model = FakePackModel()
        workflow = ApplicationPackWorkflow(packs=packs, workspace=workspace,
            evidence=evidence, memories=MemoryRepository(db.session_factory), model=model)
        handler = JobAgentStepHandler(analyzer=fake_analyze)
        workers = AgentWorkerRegistry()
        for worker in (SourceProvisionWorker(ApplicationSourceReader(workspace, packs)),
                       CandidateAnalysisWorker(handler, evidence=evidence), JobAnalysisWorker(handler),
                       RequirementMatchWorker(handler),
                       EvidenceGapWorker(InterviewRepository(db.session_factory)),
                       AnalysisProjectionWorker(workspace),
                       EvidenceFreezeWorker(workspace=workspace, evidence=evidence,
                           pack_workflow=workflow), ArtifactWriterWorker(model),
                       ArtifactVerifierWorker(model=model), ArtifactRevisionWorker(model),
                       PackAssemblerWorker(packs=packs, workspace=workspace,
                                           pack_workflow=workflow)):
            workers.register(worker)
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()},
            evidence=evidence)
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
                                       workers=workers, contexts=contexts)
        runtime = SessionRuntime(database=db, sessions=sessions, tool_calls=None,
            registry=ToolRegistry(), coordinator=None, run_reader=None, claims=None,
            workspace=workspace, evidence=evidence, packs=packs, pack_workflow=workflow,
            agent_tasks=tasks, agent_workers=workers, agent_scheduler=scheduler)
        client = TestClient(create_app(session_runtime=runtime))
        endpoint = f"/api/applications/{application.application_id}/multi-agent-runs"
        body = {"expected_application_version": application.version,
            "requested_artifacts": ["tailored_resume"], "include_interview": False,
            "idempotency_key": "api-opt-in"}
        created = client.post(endpoint, json=body)
        assert created.status_code == 200, created.text
        root_id = created.json()["root_task_id"]
        assert created.json()["workflow_mode"] == "multi_agent_v1"
        match_task = next(item for item in tasks.children(root_id)
                          if item.task_type == "requirement_match")
        assert match_task.input_spec["evidence_ids"] == [confirmed.evidence_id]
        assert client.post(endpoint, json=body).json()["root_task_id"] == root_id
        conflict = client.post(endpoint, json={**body, "requested_artifacts": ["cover_letter"]})
        assert conflict.status_code == 409
        assert client.get(f"/api/multi-agent-runs/{root_id}").status_code == 200
        assert client.get(f"/api/multi-agent-runs/{root_id}/tasks").status_code == 200
        assert client.get(f"/api/multi-agent-runs/{root_id}/timeline").status_code == 200
        assert client.post(endpoint, json={**body, "requested_artifacts": ["unknown"]}).status_code == 422
        for _ in range(60):
            ready = tasks.list_ready(limit=10)
            if not ready:
                break
            completed = scheduler.run_one(ready[0].task_id)
            assert completed.status == TaskStatus.SUCCEEDED, (completed.task_type, completed.error_code)
        final = client.get(f"/api/multi-agent-runs/{root_id}")
        assert final.status_code == 200
        assert final.json()["status"] == "awaiting_review"
        assert final.json()["pack_status"] == "awaiting_review"
        assert packs.get(final.json()["pack_id"]).workflow_mode == "multi_agent_v1"
        artifacts = client.get(f"/api/applications/{application.application_id}/artifacts")
        assert artifacts.status_code == 200
        assert all(item["workflow_mode"] == "multi_agent_v1"
                   for item in artifacts.json()["artifacts"])
    finally:
        if scheduler is not None:
            scheduler.stop()
        db.close()


def test_interviewer_worker_pauses_and_resumes_from_persisted_identifier():
    from types import SimpleNamespace
    from agent_runtime.multi_agent.types import AgentTask, ExecutionContext, MultiAgentBudget
    from agent_runtime.job_workflow.workers import InterviewerWorker

    class FakeController:
        status = "awaiting_answer"
        starts = 0

        def start(self, application_id, **kwargs):
            self.starts += 1
            return SimpleNamespace(interview_session_id="interview-1")

        def view(self, interview_id):
            assert interview_id == "interview-1"
            return {"interview": {"application_id": "app-1", "status": self.status},
                    "assessments": [{"linked_evidence_ids": ["E-1"]}]}

    controller = FakeController()
    worker = InterviewerWorker(controller)
    task = AgentTask(task_type="evidence_interview", agent_role="Evidence interview",
        root_task_id="root", parent_session_id="parent", application_id="app-1",
        depth=1, idempotency_key="interview")
    context = ExecutionContext(task_id=task.task_id, attempt_id="attempt-1",
        child_session_id="child", context_snapshot_id="snapshot", context_hash="hash",
        allowed_tools=frozenset(), allowed_skill_version_ids=frozenset(),
        input_artifacts={"gap": {"kind": "evidence_gap_plan", "data": {
            "requirements": [{"classification": "clarification_needed"}]}}},
        usage_budget=MultiAgentBudget())
    paused = worker.execute(task, context)
    assert paused.awaiting_input and paused.pause_metadata == {"interview_id": "interview-1"}
    controller.status = "completed"
    resumed = task.model_copy(update={"result_summary": {
        "summary": paused.summary, "pause_metadata": paused.pause_metadata}})
    completed = worker.execute(resumed, context)
    assert not completed.awaiting_input and controller.starts == 1
    assert completed.output_artifacts[0].content["data"]["confirmed_evidence_ids"] == ["E-1"]


def test_interview_pause_survives_database_reopen(tmp_path):
    from types import SimpleNamespace
    from agent_runtime.multi_agent.types import (
        AgentPlan, AgentTaskResult, OutputArtifact, TaskDependencySpec, TaskSpec,
    )
    from agent_runtime.job_workflow.workers import InterviewerWorker

    class GapWorker:
        task_type = "gap_test"
        def execute(self, task, context):
            return AgentTaskResult(summary="Gap prepared.", output_artifacts=[OutputArtifact(
                role="evidence_gap_plan", content={"kind": "evidence_gap_plan",
                "data": {"requirements": [{"classification": "clarification_needed"}]}})])

    class FakeController:
        status = "awaiting_answer"
        starts = 0
        def start(self, application_id, **kwargs):
            self.starts += 1
            return SimpleNamespace(interview_session_id="interview-persisted")
        def view(self, interview_id):
            assert interview_id == "interview-persisted"
            return {"interview": {"application_id": app_id, "status": self.status},
                    "assessments": [{"linked_evidence_ids": []}]}

    url = f"sqlite:///{(tmp_path / 'reopen.db').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    controller = FakeController()
    scheduler = None
    try:
        workspace = JobWorkspaceRepository(db.session_factory)
        app_id = workspace.save_workspace(cleaned_job_description="Python",
            title="Engineer").application.application_id
        sessions = SessionRepository(db.session_factory)
        sessions.create(SessionState(session_id="parent"), SessionEvent(
            session_id="parent", event_type=SessionEventType.SESSION_CREATED))
        plan = AgentPlan(template_id="interview-restart", parent_session_id="parent",
            root_key="root", idempotency_key="pause-reopen",
            tasks=[TaskSpec(key="root", task_type="gap_test", agent_role="Gap",
                application_id=app_id),
                TaskSpec(key="interview", task_type="evidence_interview",
                agent_role="Interview", application_id=app_id, parent_key="root",
                max_attempts=3,
                input_from_tasks={"evidence_gap_plan": "root"})],
            dependencies=[TaskDependencySpec(task_key="interview", depends_on_key="root")])
        workers = AgentWorkerRegistry()
        workers.register(GapWorker())
        workers.register(InterviewerWorker(controller))
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()})
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
            workers=workers, contexts=contexts)
        depths = AgentPlanValidator(worker_types=workers.names(), registered_tools=frozenset(),
            parent_tools=frozenset(), session_tools=frozenset()).validate(plan)
        root = tasks.create_plan(plan, depths)
        scheduler.run_one(root.task_id)
        interview = tasks.children(root.task_id)[0]
        paused = scheduler.run_one(interview.task_id)
        assert paused.status == TaskStatus.AWAITING_INPUT
        assert paused.result_summary["pause_metadata"]["interview_id"] == "interview-persisted"
        scheduler.stop(); scheduler = None; db.close()
        db = create_database(url)
        sessions = SessionRepository(db.session_factory)
        tasks = AgentTaskRepository(db.session_factory)
        contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
            registered_tools=frozenset(), parent_allowed_tools=frozenset(),
            task_type_tools={name: frozenset() for name in workers.names()})
        scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
            workers=workers, contexts=contexts)
        controller.status = "completed"
        paused = tasks.require(interview.task_id)
        tasks.resume(interview.task_id, expected_version=paused.version)
        assert scheduler.run_one(interview.task_id).status == TaskStatus.SUCCEEDED
        assert controller.starts == 1
    finally:
        if scheduler is not None:
            scheduler.stop()
        db.close()

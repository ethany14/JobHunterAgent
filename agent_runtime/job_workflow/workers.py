"""Narrow business adapters for the deterministic job task graph.

Every worker consumes only explicitly linked, typed artifacts. The source
loader is a trusted boundary; analysis workers never receive the other source.
"""
from __future__ import annotations

from typing import Protocol
from uuid import NAMESPACE_URL, uuid5
import json
import hashlib

from agent_runtime.application_pack.policy import EvidenceSelectionPolicy, evidence_set_hash
from agent_runtime.application_pack.types import (
    ApplicationAnswer, ArtifactVerification, CoverLetter, GenerationEvidenceSnapshot,
)
from agent_runtime.application_pack.repository import PackRepository
from agent_runtime.application_pack.policy import classify_question, requires_manual_answer
from agent_runtime.application_pack.verifier import ArtifactVerifier
from agent_runtime.application_pack.workflow import (
    PACK_PROMPT_VERSION, ApplicationPackWorkflow, PackModelClient, SharedPackModel,
)
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.interviewer.controller import InterviewController
from agent_runtime.interviewer.repository import InterviewRepository
from agent_runtime.workspace.repository import JobWorkspaceRepository

from agent_runtime.multi_agent.types import AgentTask, AgentTaskResult, ExecutionContext, OutputArtifact
from agent_runtime.multi_agent.errors import WorkerStepError
from custom_agent.handlers import JobAgentStepHandler
from custom_agent.state import AgentState, Step
from job_agent.schemas import (
    JobAnalysis, ResumeAnalysis, ResumeEvidence, SkillMatch, TailoredResume,
    VerificationResult,
)
from agent_runtime.security import canonical_json


class JobSourceReader(Protocol):
    def read_sources(self, application_id: str, *, run_id: str | None = None,
                     snapshot_id: str | None = None) -> tuple[str, str]: ...


class ApplicationSourceReader:
    """Read one attached, analyzed run and the current persisted JobSnapshot."""

    def __init__(self, workspace, packs) -> None:
        self._workspace, self._packs = workspace, packs

    def read_sources(self, application_id: str, *, run_id: str | None = None,
                     snapshot_id: str | None = None) -> tuple[str, str]:
        snapshot = self._workspace.current_snapshot(application_id)
        if snapshot_id and snapshot.snapshot_id != snapshot_id:
            raise ValueError("The Job snapshot changed after execution was created.")
        for linked in self._workspace.associated_runs(application_id):
            if run_id is not None and linked["run_id"] != run_id:
                continue
            source = self._packs.resume_source(application_id, linked["run_id"])
            if source is not None:
                return source[0], snapshot.cleaned_job_description
        raise ValueError("Attach an analyzed resume run to this Application first.")


def _one(context: ExecutionContext, kind: str) -> dict:
    matches = [value["data"] for value in context.input_artifacts.values()
               if value.get("kind") == kind and isinstance(value.get("data"), dict)]
    if len(matches) != 1:
        raise ValueError(f"Exactly one {kind} input artifact is required.")
    return matches[0]


def _artifact(role: str, data: dict) -> OutputArtifact:
    return OutputArtifact(role=role, content={"kind": role, "data": data})


class SourceProvisionWorker:
    task_type = "job_source_provision"

    def __init__(self, reader: JobSourceReader) -> None:
        self._reader = reader

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        if not task.application_id:
            raise ValueError("A saved Application is required.")
        resume, jd = self._reader.read_sources(task.application_id,
            run_id=task.input_spec.get("source_run_id"),
            snapshot_id=task.input_spec.get("job_snapshot_id"))
        if not resume.strip() or not jd.strip():
            raise ValueError("Both an original resume and Job snapshot are required.")
        expected = task.input_spec.get("source_hash")
        if expected and hashlib.sha256(canonical_json({"resume_text": resume,
                "job_description": jd}).encode()).hexdigest() != expected:
            raise ValueError("The source content changed after execution was created.")
        return AgentTaskResult(summary="Job and resume sources prepared.", output_artifacts=[
            _artifact("resume_source", {"resume_text": resume}),
            _artifact("job_source", {"job_description": jd}),
        ])


class CandidateAnalysisWorker:
    task_type = "candidate_analysis"

    def __init__(self, handler: JobAgentStepHandler | None = None,
                 evidence: CareerEvidenceRepository | None = None) -> None:
        self._handler = handler or JobAgentStepHandler()
        self._evidence = evidence

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        source = _one(context, "resume_source")
        state = AgentState(run_id=task.task_id, resume_text=source["resume_text"],
                           job_description="")
        # A structured model can occasionally paraphrase an exact quote. Retry
        # extraction once, but never accept or import an ungrounded quote.
        for attempt in range(2):
            try:
                analysis = ResumeAnalysis.model_validate(self._handler.execute(
                    Step.ANALYZE_RESUME, state).updates["resume_analysis"])
            except Exception as exc:
                raise WorkerStepError("resume_analysis_failed") from exc
            validated = AgentState.model_validate({**state.model_dump(mode="python"),
                                                    "resume_analysis": analysis})
            try:
                self._handler.execute(Step.VALIDATE_EVIDENCE, validated)
            except ValueError as exc:
                if attempt == 0:
                    continue
                raise WorkerStepError("resume_evidence_invalid") from exc
            break
        if self._evidence is not None:
            try:
                self._evidence.import_resume_evidence(analysis, source["resume_text"])
            except Exception as exc:
                raise WorkerStepError("evidence_import_failed") from exc
        return AgentTaskResult(summary="Candidate evidence analyzed.", output_artifacts=[
            _artifact("candidate_profile", analysis.model_dump(mode="json"))])


class JobAnalysisWorker:
    task_type = "job_analysis"

    def __init__(self, handler: JobAgentStepHandler | None = None) -> None:
        self._handler = handler or JobAgentStepHandler()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        source = _one(context, "job_source")
        state = AgentState(run_id=task.task_id, resume_text="",
                           job_description=source["job_description"])
        try:
            analysis = JobAnalysis.model_validate(self._handler.execute(
                Step.ANALYZE_JOB, state).updates["job_analysis"])
        except Exception as exc:
            raise WorkerStepError("job_analysis_failed") from exc
        return AgentTaskResult(summary="Job requirements analyzed.", output_artifacts=[
            _artifact("job_analysis", analysis.model_dump(mode="json"))])


class RequirementMatchWorker:
    task_type = "requirement_match"

    def __init__(self, handler: JobAgentStepHandler | None = None) -> None:
        self._handler = handler or JobAgentStepHandler()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        candidate = ResumeAnalysis.model_validate(_one(context, "candidate_profile"))
        job = JobAnalysis.model_validate(_one(context, "job_analysis"))
        # Confirmed Career Evidence is explicit, scoped input. It is never
        # silently merged into the original resume or re-labeled as a quote.
        extra = [ResumeEvidence(evidence_id=item["version_id"],
                source_section="Confirmed Career Evidence", exact_text=item["claim_text"])
                 for item in context.evidence_items]
        candidate = ResumeAnalysis.model_validate({**candidate.model_dump(mode="python"),
            "evidence": [*candidate.evidence, *extra]})
        state = AgentState(run_id=task.task_id, resume_text="", job_description="",
                           resume_analysis=candidate, job_analysis=job)
        match = SkillMatch.model_validate(self._handler.execute(
            Step.MATCH_SKILLS, state).updates["skill_match"])
        return AgentTaskResult(summary="Requirements matched to grounded evidence.",
            output_artifacts=[_artifact("match_report", match.model_dump(mode="json"))])


class EvidenceGapWorker:
    task_type = "evidence_gap"

    def __init__(self, interviews: InterviewRepository | None = None) -> None:
        self._interviews = interviews

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        match = SkillMatch.model_validate(_one(context, "match_report"))
        # The Pack DAG waits for the current paired analysis projection before
        # this idempotent assessment update. A bare analysis DAG can still use
        # the pure classification worker without Workspace persistence.
        if self._interviews is not None:
            self._interviews.prepare_assessments(task.application_id)
        gaps = []
        for item in match.matches:
            # Missing resume evidence is not proof the candidate lacks a skill.
            classification = ("sufficient" if item.match_status == "matched" else
                "clarification_needed" if item.match_status in {
                    "partial", "needs_confirmation", "missing"} else "not_applicable")
            gaps.append({"requirement_id": item.requirement_id,
                         "classification": classification,
                         "match_status": item.match_status})
        return AgentTaskResult(summary="Evidence gaps classified.", output_artifacts=[
            _artifact("evidence_gap_plan", {"requirements": gaps})])


class AnalysisProjectionWorker:
    """Project the paired outputs through one idempotent Workspace transaction."""

    task_type = "analysis_projection"

    def __init__(self, workspace: JobWorkspaceRepository) -> None:
        self._workspace = workspace

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        job = JobAnalysis.model_validate(_one(context, "job_analysis"))
        match = SkillMatch.model_validate(_one(context, "match_report"))
        job_artifact, match_artifact = self._workspace.project_multi_analysis(
            task.application_id, snapshot_id=task.input_spec["job_snapshot_id"],
            source_task_id=task.task_id, job=job, match=match)
        return AgentTaskResult(summary="Current analysis projected to Workspace.",
            output_artifacts=[_artifact("projected_analysis", {
                "job_artifact_id": job_artifact.artifact_id,
                "match_artifact_id": match_artifact.artifact_id})])


class InterviewerWorker:
    """Pause around the existing persisted InterviewController, never invent approval."""

    task_type = "evidence_interview"

    def __init__(self, controller: InterviewController) -> None:
        self._controller = controller

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        gap = _one(context, "evidence_gap_plan")
        if not any(item["classification"] == "clarification_needed"
                   for item in gap["requirements"]):
            return AgentTaskResult(summary="No evidence clarification is needed.",
                output_artifacts=[_artifact("evidence_discovery_summary",
                    {"interview_id": None, "status": "not_needed"})])
        previous = (task.result_summary or {}).get("pause_metadata", {})
        interview_id = previous.get("interview_id")
        if interview_id:
            view = self._controller.view(interview_id)
            if view["interview"]["application_id"] != task.application_id:
                raise ValueError("Interview belongs to another Application.")
        else:
            interview = self._controller.start(task.application_id, task_id=task.task_id,
                parent_session_id=task.parent_session_id)
            interview_id = interview.interview_session_id
            view = self._controller.view(interview_id)
        status = view["interview"]["status"]
        if status == "cancelled" and previous.get("continue_without_clarification") == "true":
            return AgentTaskResult(summary="Interview skipped by explicit user choice.",
                output_artifacts=[_artifact("evidence_discovery_summary", {
                    "interview_id": interview_id, "status": "skipped", "confirmed_evidence_ids": []})])
        if status == "completed":
            return AgentTaskResult(summary="Evidence interview completed.",
                output_artifacts=[_artifact("evidence_discovery_summary", {
                    "interview_id": interview_id, "status": status,
                    "confirmed_evidence_ids": sorted({evidence_id
                        for assessment in view["assessments"]
                        for evidence_id in assessment["linked_evidence_ids"]})})])
        if status in {"awaiting_answer", "awaiting_evidence_confirmation"}:
            return AgentTaskResult(summary="Evidence interview needs user input.",
                awaiting_input=True, pause_metadata={"interview_id": interview_id})
        raise ValueError("The evidence interview is not ready to resume.")


class EvidenceFreezeWorker:
    """Produce one immutable, scoped manifest; no writer receives live Evidence."""

    task_type = "freeze_evidence"

    def __init__(self, *, workspace: JobWorkspaceRepository,
                 evidence: CareerEvidenceRepository,
                 pack_workflow: ApplicationPackWorkflow) -> None:
        self._workspace, self._evidence = workspace, evidence
        self._pack_workflow = pack_workflow
        self._selection = EvidenceSelectionPolicy()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        if not task.application_id:
            raise ValueError("A saved Application is required.")
        job = JobAnalysis.model_validate(_one(context, "job_analysis"))
        application = self._workspace.get_application(task.application_id)
        if application.current_snapshot_id != task.input_spec.get("job_snapshot_id"):
            raise ValueError("The Job snapshot changed before evidence freezing.")
        selected = self._selection.select(
            evidence=self._evidence.list(status="confirmed", limit=500),
            links=self._evidence.list_for_application(task.application_id), job=job)
        if not selected:
            raise ValueError("Confirmed, relevant evidence is required before writing.")
        preferences = self._pack_workflow._preferences()
        model_configuration = self._pack_workflow._model_configuration()
        snapshot = GenerationEvidenceSnapshot(
            generation_snapshot_id=str(uuid5(NAMESPACE_URL, f"generation:{task.task_id}")),
            pack_id=str(uuid5(NAMESPACE_URL, f"pack:{task.root_task_id}")),
            application_id=task.application_id,
            job_snapshot_id=application.current_snapshot_id, items=selected,
            preference_versions=preferences, prompt_version=PACK_PROMPT_VERSION,
            model_config_id=str(model_configuration["model_id"]),
            model_configuration=model_configuration, effective_tools=[],
            evidence_set_hash=evidence_set_hash(selected,
                job_snapshot_id=application.current_snapshot_id,
                preferences=preferences, prompt_version=PACK_PROMPT_VERSION,
                model_configuration=model_configuration),
            created_at=self._workspace.current_snapshot(task.application_id).captured_at,
        )
        return AgentTaskResult(summary="Confirmed evidence frozen for this execution.",
            output_artifacts=[_artifact("generation_evidence_snapshot",
                                        snapshot.model_dump(mode="json"))])


def _writer_state(task: AgentTask, context: ExecutionContext,
                  *, current: dict | None = None,
                  report: ArtifactVerification | None = None) -> AgentState:
    snapshot = GenerationEvidenceSnapshot.model_validate(_one(context, "generation_evidence_snapshot"))
    job = JobAnalysis.model_validate(_one(context, "job_analysis"))
    match = SkillMatch.model_validate(_one(context, "match_report"))
    evidence = [ResumeEvidence(evidence_id=item.evidence_version_id,
                source_section=item.source_section or "Confirmed Career Evidence",
                exact_text=item.claim_text) for item in snapshot.items]
    from job_agent.schemas import UnsupportedClaim
    feedback = None
    if report is not None and not report.passed:
        feedback = VerificationResult(passed=False,
            unsupported_claims=[UnsupportedClaim(claim=issue.unsupported_text or "Unverified draft",
                reason=issue.reason) for issue in report.issues],
            revision_feedback=[issue.revision_instruction for issue in report.issues])
    return AgentState(run_id=task.task_id, resume_text="\n".join(e.exact_text for e in evidence),
        job_description=job.summary, resume_analysis=ResumeAnalysis(
            summary="Confirmed evidence", skills=[], evidence=evidence, education=[]),
        job_analysis=job, skill_match=match,
        tailored_resume=TailoredResume.model_validate(current) if current else None,
        verification=feedback)


def _writing_context(task: AgentTask, context: ExecutionContext) -> str:
    snapshot = GenerationEvidenceSnapshot.model_validate(_one(context, "generation_evidence_snapshot"))
    job = JobAnalysis.model_validate(_one(context, "job_analysis"))
    match = SkillMatch.model_validate(_one(context, "match_report"))
    return json.dumps({"company": task.input_spec.get("company"),
        "title": task.input_spec.get("title"),
        "job_analysis": job.model_dump(mode="json"),
        "skill_match": match.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in snapshot.items],
        "preferences": snapshot.preference_versions,
        "question": task.input_spec.get("question"),
        "max_length": task.input_spec.get("max_length")}, ensure_ascii=False)


class ArtifactWriterWorker:
    task_type = "artifact_writer"

    def __init__(self, model: PackModelClient | None = None) -> None:
        self._model = model or SharedPackModel()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        kind = task.input_spec["artifact_type"]
        if kind == "application_answer":
            question = task.input_spec["question"]
            if requires_manual_answer(classify_question(question)):
                return AgentTaskResult(summary="Application question needs a manual answer.",
                    awaiting_input=True)
            output = ApplicationAnswer.model_validate(
                self._model.application_answer(_writing_context(task, context)))
        elif kind == "cover_letter":
            output = CoverLetter.model_validate(self._model.cover_letter(
                _writing_context(task, context)))
        elif kind == "tailored_resume":
            snapshot = GenerationEvidenceSnapshot.model_validate(
                _one(context, "generation_evidence_snapshot"))
            output = TailoredResume.model_validate(self._model.resume(
                _writer_state(task, context), snapshot.preference_versions))
        else:
            raise ValueError("Unregistered application artifact type.")
        return AgentTaskResult(summary=f"{kind} draft generated.", output_artifacts=[
            _artifact(task.input_spec["artifact_key"], output.model_dump(mode="json"))])


class ArtifactVerifierWorker:
    task_type = "artifact_verifier"

    def __init__(self, *, model: PackModelClient | None = None,
                 verifier: ArtifactVerifier | None = None) -> None:
        self._model = model or SharedPackModel()
        self._verifier = verifier or ArtifactVerifier()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        key, kind = task.input_spec["artifact_key"], task.input_spec["artifact_type"]
        content = _one(context, key)
        snapshot = GenerationEvidenceSnapshot.model_validate(
            _one(context, "generation_evidence_snapshot"))
        prior_key = task.input_spec.get("prior_report_key")
        prior = _one(context, prior_key) if prior_key else None
        content_hash = hashlib.sha256(canonical_json(content).encode()).hexdigest()
        if prior and prior["source_content_hash"] == content_hash and prior["verification"]["passed"]:
            verdict = ArtifactVerification.model_validate(prior["verification"])
        else:
            job_data = _one(context, "job_analysis")
            verdict = self._verifier.verify(content, kind, snapshot.items,
                max_length=task.input_spec.get("max_length"),
                expected_question=task.input_spec.get("question"),
                preferences=snapshot.preference_versions,
                expected_role=job_data.get("title") if isinstance(job_data, dict) else None)
        if kind == "tailored_resume" and verdict.passed and not (
                prior and prior["source_content_hash"] == content_hash and prior["verification"]["passed"]):
            llm_verdict = self._model.verify_resume(_writer_state(task, context, current=content))
            if not llm_verdict.passed:
                from agent_runtime.application_pack.types import ClaimIssue
                verdict = ArtifactVerification(passed=False, issues=[ClaimIssue(
                    block_id=f"model-{index}", unsupported_text=claim.claim,
                    reason=claim.reason, cited_evidence_ids=[],
                    revision_instruction=llm_verdict.revision_feedback[
                        min(index, len(llm_verdict.revision_feedback) - 1)]
                        if llm_verdict.revision_feedback else "Remove the unsupported claim.")
                    for index, claim in enumerate(llm_verdict.unsupported_claims)])
        report = {"artifact_key": key, "artifact_type": kind,
                  "source_content_hash": content_hash,
                  "verification": verdict.model_dump(mode="json")}
        return AgentTaskResult(summary="Verification passed." if verdict.passed else
            "Verification found unsupported content.", output_artifacts=[
                _artifact(task.input_spec["report_key"], report)])


class ArtifactRevisionWorker:
    task_type = "artifact_revision"

    def __init__(self, model: PackModelClient | None = None) -> None:
        self._model = model or SharedPackModel()

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        key, kind = task.input_spec["artifact_key"], task.input_spec["artifact_type"]
        current = _one(context, key)
        report = _one(context, task.input_spec["report_key"])
        digest = hashlib.sha256(canonical_json(current).encode()).hexdigest()
        if digest != report["source_content_hash"]:
            raise ValueError("Verification report does not match its artifact version.")
        verdict = ArtifactVerification.model_validate(report["verification"])
        if verdict.passed:
            revised = current
            summary = "Verified artifact retained without revision."
        elif kind == "tailored_resume":
            snapshot = GenerationEvidenceSnapshot.model_validate(
                _one(context, "generation_evidence_snapshot"))
            revised = TailoredResume.model_validate(self._model.revise_resume(
                _writer_state(task, context, current=current, report=verdict),
                snapshot.preference_versions)).model_dump(mode="json")
            summary = "Resume revised from verification feedback."
        else:
            prompt = _writing_context(task, context) + "\nCurrent artifact: " + json.dumps(
                current, ensure_ascii=False) + "\nVerification feedback: " + verdict.model_dump_json()
            result = self._model.revise_blocks(kind, prompt)
            schema = CoverLetter if kind == "cover_letter" else ApplicationAnswer
            revised = schema.model_validate(result).model_dump(mode="json")
            summary = "Artifact revised from verification feedback."
        return AgentTaskResult(summary=summary, output_artifacts=[
            _artifact(task.input_spec["revised_key"], revised)])


class PackAssemblerWorker:
    """Idempotently project verified task outputs into the public Pack schema."""

    task_type = "pack_assembler"

    def __init__(self, *, packs: PackRepository, workspace: JobWorkspaceRepository,
                 pack_workflow: ApplicationPackWorkflow) -> None:
        self._packs, self._workspace = packs, workspace
        self._pack_workflow = pack_workflow

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        snapshot = GenerationEvidenceSnapshot.model_validate(
            _one(context, "generation_evidence_snapshot"))
        app = self._workspace.get_application(snapshot.application_id)
        if app.current_snapshot_id != snapshot.job_snapshot_id:
            raise ValueError("The Job snapshot changed during generation.")
        # A new confirmation or preference after freezing belongs to a new Pack.
        # Running writers keep their frozen inputs, but their old output is not
        # silently published as a current human-review result.
        current = self._pack_workflow._current_snapshot(snapshot.application_id)
        if current.evidence_set_hash != snapshot.evidence_set_hash:
            raise ValueError("Confirmed evidence or preferences changed after freezing.")
        available = {value.get("kind") for value in context.input_artifacts.values()}
        for spec in task.input_spec["final_artifacts"]:
            if spec["required"] and (spec["artifact_role"] not in available
                                     or spec["report_role"] not in available):
                raise ValueError("A required verified artifact is unavailable.")
        pack = self._packs.create(snapshot, expected_application_version=app.version,
            idempotency_key=f"multi-agent:{task.root_task_id}", workflow_mode="multi_agent_v1")
        item_ids = []
        omitted = []
        for spec in task.input_spec["final_artifacts"]:
            if spec["artifact_role"] not in available or spec["report_role"] not in available:
                if spec["required"]:
                    raise ValueError("A required verified artifact is unavailable.")
                omitted.append(spec["key"])
                continue
            content = _one(context, spec["artifact_role"])
            report = _one(context, spec["report_role"])
            if hashlib.sha256(canonical_json(content).encode()).hexdigest() != report["source_content_hash"]:
                raise ValueError("Final verification report does not match the generated artifact.")
            verdict = ArtifactVerification.model_validate(report["verification"])
            item = self._packs.add_item(pack.pack_id,
                expected_version=self._packs.get(pack.pack_id).version,
                artifact_type=spec["artifact_type"], source_question=spec["question"],
                idempotency_key=f"multi:{task.root_task_id}:{spec['key']}")
            if item.status.value == "generating":
                item = self._packs.save_artifact(pack.pack_id, item.pack_item_id,
                    expected_version=item.version, content=content,
                    idempotency_key=f"multi:{task.root_task_id}:{spec['key']}:content")
            if item.status.value == "verifying":
                item = self._packs.verify(pack.pack_id, item.pack_item_id,
                    expected_version=item.version, verification=verdict)
            item_ids.append(item.pack_item_id)
        for index, question in enumerate(task.input_spec.get("manual_questions", [])):
            item = self._packs.add_item(pack.pack_id,
                expected_version=self._packs.get(pack.pack_id).version,
                artifact_type="application_answer", source_question=question,
                requires_manual_answer=True,
                idempotency_key=f"multi:{task.root_task_id}:manual:{index}")
            item_ids.append(item.pack_item_id)
        final = self._packs.get(pack.pack_id)
        return AgentTaskResult(summary="Application Pack prepared for human review." if not omitted
            else "Application Pack partially prepared; optional artifacts need attention.",
            output_artifacts=[_artifact("application_pack_summary", {
                "pack_id": pack.pack_id, "workflow_mode": final.workflow_mode,
                "status": final.status.value, "item_ids": item_ids,
                "omitted_optional_artifacts": omitted})])

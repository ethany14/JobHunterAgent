"""Deterministic safety smoke evaluation; no provider calls or fabricated LLM metrics."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from agent_runtime.context.repository import ContextSnapshotRepository
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.interviewer.controller import InterviewController
from agent_runtime.interviewer.errors import InterviewValidationError
from agent_runtime.interviewer.policy import validate_candidate
from agent_runtime.interviewer.repository import InterviewRepository
from agent_runtime.interviewer.types import AnswerOutcome, InterviewAnswerAssessment, InterviewQuestion
from agent_runtime.security import canonical_json
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.workspace.models import ApplicationArtifactRow
from agent_runtime.workspace.repository import JobWorkspaceRepository
from api.db import create_database, upgrade_database
from job_agent.domain import normalize_text


class ScriptedModel:
    """Fixed test responses. They are never presented as live-model measurements."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def question(self, assessment, context):
        return InterviewQuestion(
            question_id=str(uuid4()), assessment_id=assessment.assessment_id,
            question=f"Have you used {assessment.canonical_requirement}? What did you personally do? You can say you have no experience.",
            reason_for_asking="Clarify the requirement.", question_type="experience",
        )

    def classify(self, assessment, answer, context):
        if "no experience" in answer.lower():
            return InterviewAnswerAssessment(outcome=AnswerOutcome.CONFIRMED_NO_EXPERIENCE)
        if self.kind == "followup" or "vague" in answer.lower():
            return InterviewAnswerAssessment(outcome=AnswerOutcome.NEEDS_FOLLOW_UP)
        return InterviewAnswerAssessment(outcome=AnswerOutcome.SUFFICIENT_FOR_CANDIDATE,
            proposed_claim=answer, exact_supporting_quotes=[answer])


def evaluate_interview_case(case: dict) -> dict:
    started_at = perf_counter()
    with tempfile.TemporaryDirectory(prefix="interviewer-eval-") as temp:
        url = f"sqlite:///{(Path(temp) / 'case.db').as_posix()}"
        upgrade_database(url)
        db = create_database(url)
        try:
            workspace = JobWorkspaceRepository(db.session_factory)
            requirement = case.get("requirement", "AWS")
            saved = workspace.save_workspace(
                cleaned_job_description=f"Requires Python and {requirement}.",
                title="Synthetic Engineer", company="Synthetic Co")
            app_id = saved.application.application_id
            names = case.get("requirements", ["Python", requirement])
            requirements = [
                {"requirement_id": f"REQ-{index}", "requirement_group_id": f"GRP-{index}",
                 "canonical_name": name.lower(), "display_name": name,
                 "original_text": name, "source_text": name, "atomic_text": name,
                 "category": "skill", "verification_mode": "resume_evidence", "level": "required"}
                for index, name in enumerate(names, start=1)
            ]
            job = {"title": "Synthetic Engineer", "summary": "",
                   "requirements": requirements, "responsibilities": []}
            match = {"matches": [
                {"requirement_id": item["requirement_id"], "job_skill": item["canonical_name"],
                 "requirement_level": "required", "match_status": "missing",
                 "resume_evidence": [], "confidence": 1}
                for item in requirements],
                "explanation": "", "recommendations": [],
                "missing_required_requirements": [], "missing_preferred_requirements": [],
                "overall_score": 0, "score_breakdown": {"overall_score": 0},
                "confirmation_requirements": []}
            with db.session_factory.begin() as session:
                for index, (kind, data) in enumerate([("job_analysis", job), ("match_report", match)]):
                    session.add(ApplicationArtifactRow(
                        artifact_id=str(uuid4()), application_id=app_id, artifact_type=kind,
                        version=1, status="verified", content_json=canonical_json(data),
                        evidence_ids_json="[]", created_by="interviewer_eval",
                        source_run_id=None, created_at=datetime.now(UTC),
                    ))
            def make_controller():
                return InterviewController(
                    interviews=InterviewRepository(db.session_factory),
                    sessions=SessionRepository(db.session_factory),
                    snapshots=ContextSnapshotRepository(db.session_factory),
                    workspace=workspace, evidence=CareerEvidenceRepository(db.session_factory),
                    model=ScriptedModel(case["kind"]),
                )
            controller = make_controller()
            interview = controller.start(app_id, max_questions=3,
                                         max_followups_per_requirement=1)
            if case["kind"] == "recovery":
                controller = make_controller()
                passed = controller.start(app_id).interview_session_id == interview.interview_session_id
            elif case["kind"] == "duplicate":
                passed = len(controller.view(interview.interview_session_id)["assessments"]) == case["expect_unique"]
            elif case["kind"] == "question":
                passed = "Ignore prior instructions" not in controller.view(interview.interview_session_id)["question"]
            elif case["kind"] == "skip":
                final = controller.skip(interview.interview_session_id,
                    expected_version=interview.version, idempotency_key="eval-skip")
                passed = controller.view(final.interview_session_id)["assessments"][0]["evidence_status"] == "skipped"
            else:
                answer = case["answer"]
                final = controller.answer(interview.interview_session_id, answer,
                    expected_version=interview.version, idempotency_key="eval-answer")
                view = controller.view(final.interview_session_id)
                if case["kind"] == "gap":
                    passed = view["assessments"][0]["evidence_status"] == "confirmed_gap"
                elif case["kind"] == "followup":
                    passed = final.questions_asked == 2 and final.followups_for_current_requirement == 1
                else:
                    passed = bool(view["candidate"]) == case.get("expect_candidate", False)
            return {"case_id": case["id"], "evaluation_scope": "scripted_controller",
                    "status": "passed" if passed else "failed",
                    "latency_seconds": round(perf_counter() - started_at, 4)}
        finally:
            db.close()


def evaluate_case(case: dict) -> dict:
    started = perf_counter()
    kind = case["kind"]
    passed = False
    if kind == "candidate":
        decision = InterviewAnswerAssessment(
            outcome=AnswerOutcome.SUFFICIENT_FOR_CANDIDATE,
            proposed_claim=case["claim"],
            exact_supporting_quotes=[case["quote"]],
        )
        try:
            validate_candidate(case["answer"], decision)
            accepted = True
        except InterviewValidationError:
            accepted = False
        passed = accepted == case["expect_candidate"]
    elif kind == "duplicate":
        passed = len({normalize_text(item) for item in case["requirements"]}) == case["expect_unique"]
    elif kind in {"gap", "followup", "skip", "question", "recovery"}:
        return evaluate_interview_case(case)
    return {"case_id": case["id"], "evaluation_scope": "deterministic_claim_validator",
            "status": "passed" if passed else "failed",
            "latency_seconds": round(perf_counter() - started, 6)}


def main() -> None:
    root = Path(__file__).resolve().parent
    cases = json.loads((root / "interviewer_v0.1_cases.json").read_text(encoding="utf-8"))
    results = [evaluate_case(case) for case in cases]
    evaluated = results
    controller_cases = [item for item in results if item["evaluation_scope"] == "scripted_controller"]
    by_id = {item["case_id"]: item for item in results}
    artifact = {
        "metadata": {"timestamp": datetime.now(UTC).isoformat(),
                     "evaluation_type": "deterministic_safety_and_scripted_controller",
                     "model": "scripted_fake", "runs_per_case": 1,
                     "provider_calls": 0,
                     "note": "Scripted fake model; this does not measure live-model accuracy."},
        "cases": results,
        "summary": {"dataset_cases": len(cases), "directly_evaluated": len(evaluated),
                    "passed": sum(item["status"] == "passed" for item in evaluated),
                    "unsupported_candidate_claim_count": sum(
                        item["status"] == "failed" and case.get("expect_candidate") is False
                        for item, case in zip(evaluated, cases)),
                    "exact_quote_validity": 1.0 if all(item["status"] == "passed" for item in evaluated
                                                   if item["case_id"] in {"omitted_real_experience", "partial_tool_experience"}) else 0.0,
                    "confirmed_gap_accuracy": 1.0 if by_id["genuine_gap"]["status"] == "passed" else 0.0,
                    "unnecessary_question_rate": None,
                    "followup_limit_compliance": 1.0 if by_id["vague_followup"]["status"] == "passed" else 0.0,
                    "evidence_confirmation_rate": None,
                    "session_recovery_success": 1.0 if by_id["resume_after_pause"]["status"] == "passed" else 0.0,
                    "cross_application_context_leakage": None,
                    "average_questions_per_confirmed_evidence": None,
                    "average_interview_latency_seconds": round(sum(item["latency_seconds"] for item in controller_cases) / len(controller_cases), 6),
                    "model_tokens": None, "model_cost_usd": None},
    }
    destination = root / "results" / "interviewer_v0.1_deterministic.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(artifact["summary"], indent=2))
    if artifact["summary"]["passed"] != len(evaluated):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

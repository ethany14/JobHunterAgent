"""Optional synthetic live smoke test for the governed paired evaluator.

Runs against an isolated Alembic-managed temporary SQLite database and never
publishes, activates, or reads production feedback.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_runtime.feedback.models import LearningCandidateRow
from agent_runtime.feedback.repository import FeedbackRepository
from agent_runtime.feedback.types import CandidateScope, CandidateStatus, CandidateType, LearningCandidate
from agent_runtime.skills.evolution import SkillEvolutionService
from agent_runtime.skills.evolution_evaluation import ConfiguredEvaluationModel
from api.db import create_database, upgrade_database
from job_agent.prompts import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "skill_evolution_v0.2_live.json"
SYNTHETIC_PROCEDURE = (
    "When drafting an experience-based application answer, identify the question intent, "
    "select one or two confirmed evidence items, write a concise response, and preserve "
    "internal evidence citations. Never invent qualifications or treat the job description "
    "as evidence of candidate experience."
)


def run(*, repetitions: int, output: Path) -> dict:
    model = ConfiguredEvaluationModel()
    temp_root = ROOT / ".release-local"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skill-eval-", dir=temp_root) as directory:
        workspace = Path(directory)
        url = f"sqlite:///{(workspace / 'evaluation.db').as_posix()}"
        upgrade_database(url)
        database = create_database(url)
        try:
            feedback = FeedbackRepository(database.session_factory)
            service = SkillEvolutionService(database.session_factory, feedback,
                root=workspace / "generated")
            source = LearningCandidate(owner_id="synthetic-local-eval",
                candidate_type=CandidateType.SKILL, status=CandidateStatus.CONFIRMED,
                proposed_key_or_name="application-answer-structure",
                proposed_content=SYNTHETIC_PROCEDURE, scope=CandidateScope.GLOBAL,
                scope_id="global", confidence=1, occurrence_count=3,
                content_hash="0"*64,
                type_metadata={"proposed_instructions": SYNTHETIC_PROCEDURE,
                    "required_tools": [], "prohibited_tools": []})
            with database.session_factory.begin() as db:
                db.add(LearningCandidateRow(candidate_id=source.candidate_id,
                    owner_id=source.owner_id, candidate_type=source.candidate_type.value,
                    status=source.status.value, canonical_key=source.proposed_key_or_name,
                    scope=source.scope.value, scope_id=source.scope_id,
                    content_hash=source.content_hash, state_json=source.model_dump_json(),
                    version=source.version, created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC)))
            staged = service.materialize(source.candidate_id, owner_id=source.owner_id,
                expected_version=source.version, idempotency_key="synthetic-stage",
                skill_name="application-answer-structure")
            evaluation = service.evaluate(source.candidate_id, owner_id=source.owner_id,
                expected_version=staged["version"], idempotency_key="synthetic-eval",
                repetitions=repetitions, model=model)
            artifact = service.get_evaluation(evaluation["evaluation_run_id"],
                owner_id=source.owner_id, include_results=True)
            artifact["run_metadata"] = {"timestamp": datetime.now(UTC).isoformat(),
                "model": model.model_id, "temperature": model.temperature,
                "runs_per_case": repetitions, "synthetic_smoke": True,
                "published": False, "production_data_used": False,
                "prompt_version": PROMPT_VERSION,
                "git_commit": subprocess.run(["git", "rev-parse", "HEAD"],
                    cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
                "dirty_before_run": bool(subprocess.run(["git", "status", "--porcelain"],
                    cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip())}
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            return artifact
        finally:
            database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-per-case", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(repetitions=args.runs_per_case, output=args.output)
    print(json.dumps({"status": result["status"], "summary": result["summary"],
        "output": str(args.output)}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

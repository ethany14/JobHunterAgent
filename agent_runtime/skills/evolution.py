"""Human-governed Skill staging, paired evaluation, publication and rollback."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.feedback.errors import FeedbackConflictError, FeedbackValidationError, LearningCandidateNotFoundError
from agent_runtime.feedback.types import CandidateScope, CandidateStatus, CandidateType
from agent_runtime.feedback.models import LearningCandidateRow
from agent_runtime.feedback.repository import FeedbackRepository
from agent_runtime.security import canonical_json
from agent_runtime.skills.errors import SkillContentChangedError, SkillNotFoundError, SkillValidationError
from agent_runtime.skills.evolution_evaluation import (
    ConfiguredEvaluationModel, EvaluationModel, evaluate_case, load_dataset, summarize,
)
from agent_runtime.skills.evolution_models import (
    SkillActivationEventRow, SkillEvaluationCaseResultRow, SkillEvaluationRunRow,
    SkillEvolutionCandidateRow, SkillEvolutionVersionRow, SkillForwardTestResultRow,
    SkillRuntimeMetricRow,
)
from agent_runtime.skills.evolution_types import ActivationMode, SkillCandidateStatus
from agent_runtime.skills.evolution_validation import validate_generated_package
from agent_runtime.skills.hashing import hash_skill_package
from agent_runtime.skills.security import iter_package_files
from agent_runtime.skills.models import SkillEvaluationResultRow, SkillEventRow, SkillRow, SkillVersionRow
from agent_runtime.skills.repository import SkillRepository
from agent_runtime.skills.types import SkillEvent, SkillEventType, SkillStatus, SkillVersion

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "evals" / "skill_evolution_v0.2.json"
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "skills" / "generated"


class SkillEvolutionService:
    def __init__(self, factory: sessionmaker[Session], feedback: FeedbackRepository,
                 *, root: Path = _DEFAULT_ROOT, dataset_path: Path = _DEFAULT_DATASET,
                 available_tools: frozenset[str] = frozenset()):
        self._factory = factory
        self._feedback = feedback
        self._root = root.resolve()
        self._dataset_path = dataset_path.resolve()
        self._available_tools = available_tools
        self._skills = SkillRepository(factory)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _event(db: Session, *, candidate_id: str | None, version_id: str | None,
               event_type: str, reviewer: str | None, key: str | None = None,
               reason: str | None = None) -> None:
        db.add(SkillActivationEventRow(event_id=str(uuid4()), candidate_id=candidate_id,
            skill_version_id=version_id, event_type=event_type, reviewer=reviewer,
            request_key=hashlib.sha256(key.encode()).hexdigest() if key else None,
            reason_code=reason, created_at=SkillEvolutionService._now()))

    @staticmethod
    def _replayed(db: Session, candidate_id: str, key: str, event_type: str) -> bool:
        digest = hashlib.sha256(key.encode()).hexdigest()
        event = db.scalar(select(SkillActivationEventRow).where(
            SkillActivationEventRow.candidate_id == candidate_id,
            SkillActivationEventRow.request_key == digest))
        if event is None:
            return False
        if event.event_type != event_type:
            raise FeedbackConflictError("Idempotency key was reused for another Skill action.")
        return True

    def _candidate(self, db: Session, candidate_id: str, owner_id: str):
        row = db.get(LearningCandidateRow, candidate_id)
        if row is None or row.owner_id != owner_id:
            raise LearningCandidateNotFoundError("Skill candidate was not found.")
        from agent_runtime.feedback.types import LearningCandidate
        candidate = LearningCandidate.model_validate_json(row.state_json)
        if candidate.candidate_type != CandidateType.SKILL:
            raise FeedbackValidationError("Only procedural Skill candidates can be materialized.")
        return candidate

    def _record(self, db: Session, candidate_id: str, owner_id: str) -> SkillEvolutionCandidateRow:
        row = db.get(SkillEvolutionCandidateRow, candidate_id)
        if row is None or row.owner_id != owner_id:
            raise LearningCandidateNotFoundError("Governed Skill candidate was not found.")
        return row

    @staticmethod
    def _public_candidate(row: SkillEvolutionCandidateRow) -> dict:
        return {"candidate_id": row.candidate_id, "status": row.status,
            "version": row.version, "skill_name": row.skill_name,
            "semantic_version": row.semantic_version, "content_hash": row.staged_hash,
            "evaluation_run_id": row.evaluation_run_id,
            "published_version_id": row.published_version_id}

    def get_candidate(self, candidate_id: str, *, owner_id: str) -> dict:
        with self._factory() as db:
            return self._public_candidate(self._record(db, candidate_id, owner_id))

    def materialize(self, candidate_id: str, *, owner_id: str, expected_version: int,
                    idempotency_key: str, skill_name: str, semantic_version: str = "1.0.0") -> dict:
        if not _NAME.fullmatch(skill_name) or len(skill_name) >= 64 or not _VERSION.fullmatch(semantic_version):
            raise SkillValidationError(["Skill name or semantic version is invalid."])
        with self._factory() as db:
            candidate = self._candidate(db, candidate_id, owner_id)
            row = db.get(SkillEvolutionCandidateRow, candidate_id)
            if row is not None:
                if row.skill_name == skill_name and row.semantic_version == semantic_version:
                    return self._public_candidate(row)
                raise FeedbackConflictError("Skill candidate is already staged with different metadata.")
            if candidate.version != expected_version or candidate.status != CandidateStatus.CONFIRMED:
                raise FeedbackConflictError("Skill candidate requires explicit evaluation approval.")
            if candidate.scope not in {CandidateScope.GLOBAL, CandidateScope.ROLE_TYPE}:
                raise SkillValidationError(["Personal or application feedback cannot become a global Skill."])
            instructions = str(candidate.type_metadata.get("proposed_instructions") or candidate.proposed_content).strip()
            if not instructions or len(instructions) > 12_000:
                raise SkillValidationError(["Skill procedure is empty or too large."])
            if len(instructions.split()) < 7:
                raise SkillValidationError(["Skill procedure is too vague."])
            if re.search(r"(?i)\b(?:my|for me|i prefer)\b|unknown evidence id|policy invariant|must fail verification",
                         instructions):
                raise SkillValidationError(["Personal preference or deterministic policy belongs outside global Skills."])
            if any(version.version_label == semantic_version
                   for version in self._skills.versions_by_name(skill_name)):
                raise FeedbackConflictError("The Skill semantic version already exists.")
            required_tools = candidate.type_metadata.get("required_tools", [])
            prohibited_tools = candidate.type_metadata.get("prohibited_tools", [])
            if (not isinstance(required_tools, list) or not isinstance(prohibited_tools, list)
                    or not all(isinstance(item, str) for item in [*required_tools, *prohibited_tools])):
                raise SkillValidationError(["Skill tool requirements must be string lists."])
            if not set(required_tools) <= self._available_tools or set(required_tools) & set(prohibited_tools):
                raise SkillValidationError(["Skill tool requirements exceed runtime permissions."])
        package = self._root / "staging" / candidate_id / skill_name
        if package.exists():
            raise FeedbackConflictError("A staging package already exists for this candidate.")
        package.mkdir(parents=True)
        description = f"Use for {skill_name.replace('-', ' ')} in evidence-grounded job applications."
        frontmatter = {"name": skill_name, "description": description,
            "metadata": {"version": semantic_version, "scope": "generated"},
            "allowed-tools": " ".join(sorted(required_tools))}
        body = ("---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
                + "---\n\n# Purpose\n" + description + "\n\n# Workflow\n" + instructions
                + "\n\n# Constraints\nNever override evidence, permissions, approval, or safety policy.\n")
        (package / "SKILL.md").write_text(body, encoding="utf-8")
        try:
            inspected = validate_generated_package(package, available_tools=self._available_tools)
            with self._factory.begin() as db:
                latest = self._candidate(db, candidate_id, owner_id)
                if latest.version != expected_version or latest.status != CandidateStatus.CONFIRMED:
                    raise FeedbackConflictError("Skill candidate changed during staging.")
                db.add(SkillEvolutionCandidateRow(candidate_id=candidate_id, owner_id=owner_id,
                    status=SkillCandidateStatus.APPROVED_FOR_EVALUATION.value, version=1,
                    staged_path=str(package), staged_hash=inspected.content_hash,
                    skill_name=skill_name, semantic_version=semantic_version,
                    updated_at=self._now()))
                self._event(db, candidate_id=candidate_id, version_id=None,
                    event_type="staged", reviewer=owner_id, key=idempotency_key)
            return self.get_candidate(candidate_id, owner_id=owner_id)
        except Exception:
            if package.exists():
                shutil.rmtree(package)
            raise

    def preview(self, candidate_id: str, *, owner_id: str) -> dict:
        with self._factory() as db:
            row = self._record(db, candidate_id, owner_id)
            if not row.staged_path or not row.staged_hash:
                raise FeedbackConflictError("Candidate has no staged package.")
            package = Path(row.staged_path)
            inspected = validate_generated_package(package, available_tools=self._available_tools)
            active = self._skills.active_by_name(row.skill_name)
            return {**self._public_candidate(row), "instructions": (package / "SKILL.md").read_text(encoding="utf-8"),
                "validation": {"valid": True, "warnings": inspected.warnings,
                    "content_changed": inspected.content_hash != row.staged_hash},
                "references": {item.path: (package / item.path).read_text(encoding="utf-8")
                    for item in inspected.resource_manifest.resources if item.path.startswith("references/")},
                "required_tools": sorted(inspected.parsed.allowed_tools or []),
                "conflicting_active_skill": (None if active is None else {
                    "version_id": active.version_id, "description": active.description})}

    def restage(self, candidate_id: str, *, owner_id: str, expected_version: int,
                idempotency_key: str) -> dict:
        """Explicitly accept an edited staged package and invalidate its prior evaluation."""
        with self._factory.begin() as db:
            row = self._record(db, candidate_id, owner_id)
            if self._replayed(db, candidate_id, idempotency_key, "restaged"):
                return self._public_candidate(row)
            if row.version != expected_version or row.status in {
                SkillCandidateStatus.PUBLISHED.value, SkillCandidateStatus.REJECTED.value,
                SkillCandidateStatus.SUPERSEDED.value, SkillCandidateStatus.ROLLED_BACK.value,
                SkillCandidateStatus.EVALUATING.value,
            }:
                raise FeedbackConflictError("This Skill cannot be restaged now.")
            inspected = validate_generated_package(Path(row.staged_path),
                available_tools=self._available_tools)
            if inspected.parsed.name != row.skill_name or inspected.version_label != row.semantic_version:
                raise SkillValidationError(["Staged Skill name and semantic version must remain unchanged."])
            row.staged_hash = inspected.content_hash
            row.evaluation_run_id = None
            row.status = SkillCandidateStatus.APPROVED_FOR_EVALUATION.value
            row.version += 1
            row.updated_at = self._now()
            self._event(db, candidate_id=candidate_id, version_id=None,
                event_type="restaged", reviewer=owner_id, key=idempotency_key)
            return self._public_candidate(row)

    def reject(self, candidate_id: str, *, owner_id: str, expected_version: int,
               idempotency_key: str) -> dict:
        with self._factory.begin() as db:
            row = self._record(db, candidate_id, owner_id)
            if self._replayed(db, candidate_id, idempotency_key, "candidate_rejected"):
                return self._public_candidate(row)
            if row.version != expected_version:
                raise FeedbackConflictError("Skill candidate version changed.")
            if row.status in {SkillCandidateStatus.PUBLISHED.value,
                              SkillCandidateStatus.SUPERSEDED.value}:
                raise FeedbackConflictError("Published Skills require rollback, not candidate rejection.")
            row.status = SkillCandidateStatus.REJECTED.value
            row.version += 1
            row.updated_at = self._now()
            self._event(db, candidate_id=candidate_id, version_id=None,
                event_type="candidate_rejected", reviewer=owner_id, key=idempotency_key)
        return self.get_candidate(candidate_id, owner_id=owner_id)

    def evaluate(self, candidate_id: str, *, owner_id: str, expected_version: int,
                 idempotency_key: str, repetitions: int = 3,
                 model: EvaluationModel | None = None) -> dict:
        if not 1 <= repetitions <= 5:
            raise ValueError("Repetitions must be between one and five.")
        dataset, dataset_hash = load_dataset(self._dataset_path)
        with self._factory() as db:
            row = self._record(db, candidate_id, owner_id)
            if self._replayed(db, candidate_id, idempotency_key, "evaluation_started"):
                return self.get_evaluation(row.evaluation_run_id, owner_id=owner_id)
            if row.version != expected_version or row.status not in {
                SkillCandidateStatus.APPROVED_FOR_EVALUATION.value,
                SkillCandidateStatus.EVALUATION_FAILED.value,
            }:
                raise FeedbackConflictError("Skill candidate is not ready for evaluation.")
            if row.skill_name != dataset.skill_name:
                raise SkillValidationError(["No immutable evaluation dataset is registered for this Skill name."])
            package = Path(row.staged_path)
            inspected = validate_generated_package(package, available_tools=self._available_tools)
            if inspected.content_hash != row.staged_hash:
                raise SkillContentChangedError("Staged Skill changed; re-materialize and re-evaluate.")
            active = self._skills.active_by_name(row.skill_name)
            baseline_id = active.version_id if active else None
            baseline_instructions = active.instruction_snapshot if active else None
            staged_id = hashlib.sha256(f"{candidate_id}:{row.staged_hash}".encode()).hexdigest()[:36]
        actual_model = model or ConfiguredEvaluationModel()
        model_id = str(getattr(actual_model, "model_id", "test-model"))
        temperature = float(getattr(actual_model, "temperature", 0))
        run_id = str(uuid4())
        config = {"hard_gates_version": "v0.2.1", "max_latency_overhead": 2.0,
            "max_cost_overhead": 2.0, "dataset_hash": dataset_hash}
        with self._factory.begin() as db:
            row = self._record(db, candidate_id, owner_id)
            if row.version != expected_version:
                raise FeedbackConflictError("Skill candidate changed before evaluation.")
            row.status = SkillCandidateStatus.EVALUATING.value
            row.version += 1
            row.evaluation_run_id = run_id
            row.updated_at = self._now()
            db.add(SkillEvaluationRunRow(evaluation_run_id=run_id, candidate_id=candidate_id,
                baseline_skill_version_id=baseline_id, staged_skill_version_id=staged_id,
                staged_hash=inspected.content_hash, dataset_id=dataset.dataset_id,
                dataset_version=dataset.version, dataset_hash=dataset_hash,
                model_id=model_id, temperature=temperature, repetitions=repetitions,
                status="running", started_at=self._now(), config_json=canonical_json(config)))
            self._event(db, candidate_id=candidate_id, version_id=None,
                event_type="evaluation_started", reviewer=owner_id, key=idempotency_key)
        results: list[dict] = []
        forward: list[dict] = []
        try:
            for case in dataset.cases:
                for repetition in range(1, repetitions+1):
                    base, candidate_result = evaluate_case(case, name=inspected.parsed.name,
                        description=inspected.parsed.description,
                        baseline_instructions=baseline_instructions,
                        candidate_instructions=inspected.parsed.instructions,
                        model=actual_model, repetition=repetition)
                    for item in (base, candidate_result):
                        if case.holdout:
                            forward.append(item)
                        else:
                            results.append(item)
            summary = summarize(results)
            summary["forward_test"] = summarize(forward)
            summary["passed"] = bool(summary["passed"] and summary["forward_test"]["passed"])
            status = (SkillCandidateStatus.READY_FOR_PUBLICATION if summary["passed"]
                      else SkillCandidateStatus.EVALUATION_FAILED)
            with self._factory.begin() as db:
                row = self._record(db, candidate_id, owner_id)
                if row.status != SkillCandidateStatus.EVALUATING.value or row.evaluation_run_id != run_id:
                    raise FeedbackConflictError("Evaluation state changed.")
                for item in results:
                    db.add(self._result_row(run_id, item))
                for item in forward:
                    db.add(SkillForwardTestResultRow(result_id=str(uuid4()),
                        evaluation_run_id=run_id, case_id=f"{item['case_id']}:{item['variant']}:{item['repetition']}",
                        metric_values_json=canonical_json(item), completed_at=self._now()))
                evaluation = db.get(SkillEvaluationRunRow, run_id)
                evaluation.status = "passed" if summary["passed"] else "failed"
                evaluation.completed_at = self._now()
                evaluation.summary_json = canonical_json(summary)
                row.status = status.value
                row.version += 1
                row.updated_at = self._now()
                self._event(db, candidate_id=candidate_id, version_id=None,
                    event_type="evaluation_passed" if summary["passed"] else "evaluation_failed",
                    reviewer=None)
            return self.get_evaluation(run_id, owner_id=owner_id)
        except Exception:
            with self._factory.begin() as db:
                row = self._record(db, candidate_id, owner_id)
                evaluation = db.get(SkillEvaluationRunRow, run_id)
                if row.evaluation_run_id == run_id and row.status == SkillCandidateStatus.EVALUATING.value:
                    row.status = SkillCandidateStatus.EVALUATION_FAILED.value
                    row.version += 1
                evaluation.status = "failed"
                evaluation.error_code = "evaluation_execution_failed"
                evaluation.completed_at = self._now()
            raise

    @staticmethod
    def _result_row(run_id: str, item: dict) -> SkillEvaluationCaseResultRow:
        return SkillEvaluationCaseResultRow(result_id=str(uuid4()), evaluation_run_id=run_id,
            case_id=item["case_id"], variant=item["variant"], repetition=item["repetition"],
            output_reference=item["output_reference"],
            output_text=item["output_text"],
            metric_values_json=canonical_json(item["metric_values"]),
            latency=item["latency"], input_tokens=item["input_tokens"],
            output_tokens=item["output_tokens"], estimated_cost=item["estimated_cost"],
            safety_violations=item["safety_violations"], completed_at=datetime.now(UTC))

    def get_evaluation(self, run_id: str, *, owner_id: str, include_results: bool = False) -> dict:
        with self._factory() as db:
            row = db.get(SkillEvaluationRunRow, run_id)
            if row is None:
                raise SkillNotFoundError("Skill evaluation was not found.")
            self._record(db, row.candidate_id, owner_id)
            result = {"evaluation_run_id": run_id, "candidate_id": row.candidate_id,
                "baseline_skill_version_id": row.baseline_skill_version_id,
                "staged_skill_version_id": row.staged_skill_version_id,
                "staged_hash": row.staged_hash,
                "dataset_id": row.dataset_id, "dataset_version": row.dataset_version,
                "dataset_hash": row.dataset_hash,
                "model_id": row.model_id, "temperature": row.temperature,
                "repetitions": row.repetitions, "status": row.status,
                "evaluation_config": json.loads(row.config_json),
                "started_at": row.started_at, "completed_at": row.completed_at,
                "summary": json.loads(row.summary_json) if row.summary_json else None,
                "error_code": row.error_code}
            if include_results:
                cases = db.scalars(select(SkillEvaluationCaseResultRow).where(
                    SkillEvaluationCaseResultRow.evaluation_run_id == run_id)).all()
                forwards = db.scalars(select(SkillForwardTestResultRow).where(
                    SkillForwardTestResultRow.evaluation_run_id == run_id)).all()
                result["case_results"] = [{"case_id": x.case_id, "variant": x.variant,
                    "repetition": x.repetition, "output_reference": x.output_reference,
                    "output_text": x.output_text,
                    "metrics": json.loads(x.metric_values_json), "latency": x.latency,
                    "input_tokens": x.input_tokens, "output_tokens": x.output_tokens,
                    "estimated_cost": x.estimated_cost} for x in cases]
                result["forward_test_results"] = [{"case_id": x.case_id,
                    "metrics": json.loads(x.metric_values_json)} for x in forwards]
            return result

    def publish(self, candidate_id: str, *, owner_id: str, expected_version: int,
                idempotency_key: str, acknowledge_soft_regressions: bool = False) -> dict:
        """Explicit publication; the database commit is the visibility boundary."""
        with self._factory() as db:
            row = self._record(db, candidate_id, owner_id)
            if row.status == SkillCandidateStatus.PUBLISHED.value and row.published_version_id:
                return self.version(row.published_version_id, owner_id=owner_id)
            if row.version != expected_version or row.status != SkillCandidateStatus.READY_FOR_PUBLICATION.value:
                raise FeedbackConflictError("Only a passing evaluated Skill may be published.")
            evaluation = db.get(SkillEvaluationRunRow, row.evaluation_run_id)
            if evaluation is None or evaluation.status != "passed" or evaluation.summary_json is None:
                raise FeedbackConflictError("A passing paired evaluation is required.")
            summary = json.loads(evaluation.summary_json)
            if not summary.get("passed") or not all(summary.get("hard_gates", {}).values()):
                raise FeedbackConflictError("Evaluation gates did not pass.")
            if (summary.get("soft_regressions") or summary.get("forward_test", {}).get("soft_regressions")) and not acknowledge_soft_regressions:
                raise FeedbackConflictError("Soft regressions require explicit reviewer acknowledgement.")
            staged = Path(row.staged_path)
            inspected = validate_generated_package(staged, available_tools=self._available_tools)
            if inspected.content_hash != row.staged_hash or inspected.content_hash != evaluation.staged_hash:
                raise SkillContentChangedError("Staged Skill changed after evaluation.")
            name, semantic_version = row.skill_name, row.semantic_version
            active = self._skills.active_by_name(name)
            parent_id = active.version_id if active else None
        published = self._root / name / "versions" / semantic_version
        published.parent.mkdir(parents=True, exist_ok=True)
        created_published = False
        if published.exists():
            # A process may have stopped after the filesystem rename but before
            # the database commit. Reuse only an exact, validated orphan.
            orphan = validate_generated_package(published, available_tools=self._available_tools)
            if orphan.content_hash != inspected.content_hash:
                raise FeedbackConflictError("Immutable Skill version directory already exists with different content.")
        else:
            temporary = published.parent / f".pending-{uuid4()}"
            try:
                shutil.copytree(staged, temporary)
                if hash_skill_package(temporary) != inspected.content_hash:
                    raise SkillContentChangedError("Temporary Skill copy differs from evaluation.")
                if published.exists():
                    raise FeedbackConflictError("Skill version was concurrently published.")
                temporary.rename(published)
                created_published = True
            finally:
                if temporary.exists() and temporary.is_relative_to(self._root):
                    shutil.rmtree(temporary)
        try:
            copied = validate_generated_package(published, available_tools=self._available_tools)
            if copied.content_hash != inspected.content_hash:
                raise SkillContentChangedError("Published copy differs from the evaluated package.")
            with self._factory.begin() as db:
                row = self._record(db, candidate_id, owner_id)
                if row.version != expected_version or row.status != SkillCandidateStatus.READY_FOR_PUBLICATION.value:
                    raise FeedbackConflictError("Skill candidate changed before publication.")
                evaluation = db.get(SkillEvaluationRunRow, row.evaluation_run_id)
                if evaluation is None or evaluation.status != "passed" or evaluation.staged_hash != copied.content_hash:
                    raise FeedbackConflictError("Evaluation no longer matches the Skill content.")
                skill = db.scalar(select(SkillRow).where(SkillRow.name == name))
                if skill is None:
                    skill = SkillRow(skill_id=str(uuid4()), name=name, active_version_id=None,
                        created_at=self._now(), updated_at=self._now())
                    db.add(skill); db.flush()
                if db.scalar(select(SkillVersionRow.version_id).where(
                    SkillVersionRow.skill_id == skill.skill_id,
                    SkillVersionRow.version_label == semantic_version)):
                    raise FeedbackConflictError("The Skill semantic version already exists.")
                version = SkillVersion(skill_id=skill.skill_id, name=name,
                    description=copied.parsed.description, version_label=semantic_version,
                    status=SkillStatus.APPROVED, package_path=str(published),
                    metadata=copied.parsed.metadata, allowed_tools=copied.parsed.allowed_tools,
                    instruction_snapshot=copied.parsed.instructions,
                    content_hash=copied.content_hash,
                    resource_manifest=copied.resource_manifest,
                    validation_warnings=copied.warnings,
                    version=1, event_sequence=1, created_at=self._now(), updated_at=self._now())
                db.add(SkillRepository._row(version))
                db.flush()
                db.add(SkillRepository._event_row(SkillEvent(version_id=version.version_id,
                    sequence=1, event_type=SkillEventType.APPROVED,
                    to_status=SkillStatus.APPROVED, occurred_at=self._now())))
                db.flush()
                db.add(SkillEvaluationResultRow(evaluation_id=str(uuid4()),
                    version_id=version.version_id, suite_name=evaluation.dataset_id,
                    artifact_hash=hashlib.sha256(evaluation.summary_json.encode()).hexdigest(),
                    passed=True, evaluated_at=self._now()))
                db.add(SkillEvolutionVersionRow(skill_version_id=version.version_id,
                    skill_name=name, semantic_version=semantic_version,
                    parent_version_id=parent_id, candidate_id=candidate_id,
                    status="inactive", version=1, activation_mode="inactive",
                    package_path=str(published), content_hash=copied.content_hash,
                    manifest_json=canonical_json({
                        "files": [{"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                  for relative, path in iter_package_files(published)],
                        "source_candidate": candidate_id,
                        "supporting_feedback_event_ids": self._candidate(db, candidate_id, owner_id).supporting_event_ids,
                        "parent_version_id": parent_id,
                        "evaluation_dataset_version": evaluation.dataset_version,
                        "model_id": evaluation.model_id,
                        "temperature": evaluation.temperature,
                        "intended_scope": "cross_application",
                        "required_tools": sorted(copied.parsed.allowed_tools or []),
                        "prohibited_tools": [],
                        "safety_constraints": ["Evidence and runtime policies always prevail."],
                    }), evaluation_run_id=evaluation.evaluation_run_id,
                    published_by=owner_id, published_at=self._now(), created_at=self._now()))
                db.add(SkillRuntimeMetricRow(skill_version_id=version.version_id,
                    selection_count=0, successful_task_count=0, failure_count=0,
                    user_acceptance_count=0, user_rejection_count=0,
                    verifier_failure_count=0, unauthorized_tool_attempt_count=0,
                    unsupported_claim_count=0, total_latency_seconds=0,
                    total_input_tokens=0, total_output_tokens=0, total_estimated_cost=0))
                claimed = db.execute(update(SkillEvolutionCandidateRow).where(
                    SkillEvolutionCandidateRow.candidate_id == candidate_id,
                    SkillEvolutionCandidateRow.version == expected_version).values(
                    status=SkillCandidateStatus.PUBLISHED.value,
                    version=expected_version+1,
                    published_version_id=version.version_id,
                    updated_at=self._now()))
                if claimed.rowcount != 1:
                    raise FeedbackConflictError("Skill candidate changed during publication.")
                self._event(db, candidate_id=candidate_id, version_id=version.version_id,
                    event_type="published", reviewer=owner_id, key=idempotency_key)
                db.flush()
            return self.version(version.version_id, owner_id=owner_id)
        except Exception:
            # Only the newly copied target is removed. The evaluated staging tree is untouched.
            if created_published and published.exists() and published.is_relative_to(self._root):
                shutil.rmtree(published)
            raise

    def version(self, version_id: str, *, owner_id: str) -> dict:
        with self._factory() as db:
            row = db.get(SkillEvolutionVersionRow, version_id)
            if row is None:
                raise SkillNotFoundError("Published Skill version was not found.")
            self._record(db, row.candidate_id, owner_id)
            return {"skill_version_id": row.skill_version_id, "skill_name": row.skill_name,
                "semantic_version": row.semantic_version, "parent_version_id": row.parent_version_id,
                "candidate_id": row.candidate_id, "status": row.status,
                "activation_mode": row.activation_mode, "version": row.version,
                "content_hash": row.content_hash, "evaluation_run_id": row.evaluation_run_id,
                "published_at": row.published_at, "deactivated_at": row.deactivated_at,
                "manifest": json.loads(row.manifest_json)}

    def versions(self, name: str, *, owner_id: str) -> list[dict]:
        with self._factory() as db:
            ids = db.scalars(select(SkillEvolutionVersionRow.skill_version_id).join(
                SkillEvolutionCandidateRow,
                SkillEvolutionCandidateRow.candidate_id == SkillEvolutionVersionRow.candidate_id).where(
                SkillEvolutionVersionRow.skill_name == name,
                SkillEvolutionCandidateRow.owner_id == owner_id).order_by(
                SkillEvolutionVersionRow.published_at.desc())).all()
        return [self.version(version_id, owner_id=owner_id) for version_id in ids]

    def skills(self, *, owner_id: str) -> list[dict]:
        with self._factory() as db:
            names = db.scalars(select(SkillEvolutionVersionRow.skill_name).join(
                SkillEvolutionCandidateRow,
                SkillEvolutionCandidateRow.candidate_id == SkillEvolutionVersionRow.candidate_id).where(
                SkillEvolutionCandidateRow.owner_id == owner_id).distinct()).all()
        return [{"name": name, "versions": self.versions(name, owner_id=owner_id)} for name in names]

    def test_versions(self, *, owner_id: str, mode: ActivationMode) -> frozenset[str]:
        if mode not in {ActivationMode.CANARY, ActivationMode.SHADOW}:
            raise ValueError("Only canary and shadow test versions are selectable.")
        with self._factory() as db:
            return frozenset(db.scalars(select(SkillEvolutionVersionRow.skill_version_id).join(
                SkillEvolutionCandidateRow,
                SkillEvolutionCandidateRow.candidate_id == SkillEvolutionVersionRow.candidate_id).where(
                SkillEvolutionCandidateRow.owner_id == owner_id,
                SkillEvolutionVersionRow.activation_mode == mode.value,
                SkillEvolutionVersionRow.status == "inactive")).all())

    def activate(self, name: str, *, owner_id: str, version_id: str,
                 mode: ActivationMode, expected_version: int,
                 idempotency_key: str) -> dict:
        mode = ActivationMode(mode)
        with self._factory.begin() as db:
            row = db.get(SkillEvolutionVersionRow, version_id)
            if row is None or row.skill_name != name:
                raise FeedbackValidationError("Published Skill version was not found.")
            self._record(db, row.candidate_id, owner_id)
            if self._replayed(db, row.candidate_id, idempotency_key,
                              f"activation_{mode.value}"):
                return self.version(version_id, owner_id=owner_id)
            if row.version != expected_version:
                raise FeedbackConflictError("Skill version changed; refresh and retry.")
            if row.status == "rolled_back":
                raise FeedbackConflictError("Rolled-back Skill versions cannot be reactivated.")
            if hash_skill_package(Path(row.package_path)) != row.content_hash:
                raise SkillContentChangedError("Published Skill package changed.")
            validate_generated_package(Path(row.package_path), available_tools=self._available_tools)
            claimed = db.execute(update(SkillEvolutionVersionRow).where(
                SkillEvolutionVersionRow.skill_version_id == version_id,
                SkillEvolutionVersionRow.version == expected_version).values(
                version=expected_version+1))
            if claimed.rowcount != 1:
                raise FeedbackConflictError("Skill version changed during activation.")
            db.refresh(row)
            skill_version = db.get(SkillVersionRow, version_id)
            skill = db.get(SkillRow, skill_version.skill_id)
            if mode == ActivationMode.ACTIVE:
                if skill.active_version_id and skill.active_version_id != version_id:
                    old_row = db.get(SkillVersionRow, skill.active_version_id)
                    old = SkillRepository._version(old_row)
                    old_next = SkillRepository._updated(old, {"status": SkillStatus.SUPERSEDED,
                        "version": old.version+1, "event_sequence": old.event_sequence+1,
                        "updated_at": self._now()})
                    db.execute(update(SkillVersionRow).where(SkillVersionRow.version_id == old.version_id,
                        SkillVersionRow.version == old.version).values(**SkillRepository._projection(old_next)))
                    previous = db.get(SkillEvolutionVersionRow, old.version_id)
                    if previous:
                        previous.status = "inactive"; previous.activation_mode = "inactive"
                        previous.version += 1; previous.deactivated_at = self._now()
                        old_candidate = db.get(SkillEvolutionCandidateRow, previous.candidate_id)
                        if old_candidate is not None:
                            old_candidate.status = SkillCandidateStatus.SUPERSEDED.value
                            old_candidate.version += 1
                current = SkillRepository._version(skill_version)
                next_version = SkillRepository._updated(current, {"status": SkillStatus.ACTIVE,
                    "version": current.version+1, "event_sequence": current.event_sequence+1,
                    "updated_at": self._now()})
                db.execute(update(SkillVersionRow).where(SkillVersionRow.version_id == version_id,
                    SkillVersionRow.version == current.version).values(**SkillRepository._projection(next_version)))
                db.add(SkillRepository._event_row(SkillEvent(version_id=version_id,
                    sequence=next_version.event_sequence, event_type=SkillEventType.ACTIVATED,
                    from_status=current.status, to_status=SkillStatus.ACTIVE,
                    occurred_at=self._now())))
                skill.active_version_id = version_id; skill.updated_at = self._now()
            elif skill.active_version_id == version_id:
                current = SkillRepository._version(skill_version)
                retired = SkillRepository._updated(current, {"status": SkillStatus.RETIRED,
                    "version": current.version+1, "event_sequence": current.event_sequence+1,
                    "updated_at": self._now()})
                db.execute(update(SkillVersionRow).where(SkillVersionRow.version_id == version_id,
                    SkillVersionRow.version == current.version).values(**SkillRepository._projection(retired)))
                db.add(SkillRepository._event_row(SkillEvent(version_id=version_id,
                    sequence=retired.event_sequence, event_type=SkillEventType.RETIRED,
                    from_status=current.status, to_status=SkillStatus.RETIRED,
                    occurred_at=self._now())))
                skill.active_version_id = None; skill.updated_at = self._now()
            row.activation_mode = mode.value
            row.status = "active" if mode == ActivationMode.ACTIVE else "inactive"
            self._event(db, candidate_id=row.candidate_id, version_id=version_id,
                event_type=f"activation_{mode.value}", reviewer=owner_id, key=idempotency_key)
        return self.version(version_id, owner_id=owner_id)

    def rollback(self, name: str, *, owner_id: str, failed_version_id: str,
                 target_version_id: str | None, expected_version: int,
                 idempotency_key: str, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("A rollback reason is required.")
        with self._factory.begin() as db:
            failed = db.get(SkillEvolutionVersionRow, failed_version_id)
            if failed is None or failed.skill_name != name:
                raise FeedbackValidationError("Active Skill version was not found.")
            self._record(db, failed.candidate_id, owner_id)
            if self._replayed(db, failed.candidate_id, idempotency_key, "rolled_back"):
                return {"rolled_back": failed_version_id,
                        "active_version_id": target_version_id}
            if failed.version != expected_version or failed.activation_mode != "active":
                raise FeedbackConflictError("Rollback target or version changed.")
            claimed = db.execute(update(SkillEvolutionVersionRow).where(
                SkillEvolutionVersionRow.skill_version_id == failed_version_id,
                SkillEvolutionVersionRow.version == expected_version).values(
                version=expected_version+1))
            if claimed.rowcount != 1:
                raise FeedbackConflictError("Rollback target changed concurrently.")
            db.refresh(failed)
            skill_row = db.scalar(select(SkillRow).where(SkillRow.name == name))
            if skill_row.active_version_id != failed_version_id:
                raise FeedbackConflictError("Only the current active Skill can be rolled back.")
            if target_version_id:
                target = db.get(SkillEvolutionVersionRow, target_version_id)
                if target is None or target.skill_name != name or target.status == "rolled_back":
                    raise FeedbackConflictError("Rollback target must be a prior published version.")
                self._record(db, target.candidate_id, owner_id)
                if target.published_at >= failed.published_at or hash_skill_package(Path(target.package_path)) != target.content_hash:
                    raise FeedbackConflictError("Rollback target is unavailable or changed.")
                target_skill_row = db.get(SkillVersionRow, target_version_id)
                target_skill = SkillRepository._version(target_skill_row)
                target_next = SkillRepository._updated(target_skill, {"status": SkillStatus.ACTIVE,
                    "version": target_skill.version+1, "event_sequence": target_skill.event_sequence+1,
                    "updated_at": self._now()})
                db.execute(update(SkillVersionRow).where(SkillVersionRow.version_id == target_version_id,
                    SkillVersionRow.version == target_skill.version).values(**SkillRepository._projection(target_next)))
                db.add(SkillRepository._event_row(SkillEvent(version_id=target_version_id,
                    sequence=target_next.event_sequence, event_type=SkillEventType.ACTIVATED,
                    from_status=target_skill.status, to_status=SkillStatus.ACTIVE,
                    occurred_at=self._now())))
                target.activation_mode = "active"; target.status = "active"; target.version += 1
            failed_skill_row = db.get(SkillVersionRow, failed_version_id)
            failed_skill = SkillRepository._version(failed_skill_row)
            failed_next = SkillRepository._updated(failed_skill, {"status": SkillStatus.RETIRED,
                "version": failed_skill.version+1, "event_sequence": failed_skill.event_sequence+1,
                "updated_at": self._now()})
            db.execute(update(SkillVersionRow).where(SkillVersionRow.version_id == failed_version_id,
                SkillVersionRow.version == failed_skill.version).values(**SkillRepository._projection(failed_next)))
            db.add(SkillRepository._event_row(SkillEvent(version_id=failed_version_id,
                sequence=failed_next.event_sequence, event_type=SkillEventType.RETIRED,
                from_status=failed_skill.status, to_status=SkillStatus.RETIRED,
                occurred_at=self._now())))
            failed.status = "rolled_back"; failed.activation_mode = "inactive"
            failed.deactivated_at = self._now()
            candidate = db.get(SkillEvolutionCandidateRow, failed.candidate_id)
            if candidate is not None:
                candidate.status = SkillCandidateStatus.ROLLED_BACK.value
                candidate.version += 1
            skill_row.active_version_id = target_version_id
            skill_row.updated_at = self._now()
            self._event(db, candidate_id=failed.candidate_id, version_id=failed_version_id,
                event_type="rolled_back", reviewer=owner_id, key=idempotency_key,
                reason="explicit_rollback")
        return {"rolled_back": failed_version_id, "active_version_id": target_version_id}

    def metrics(self, name: str, *, owner_id: str) -> list[dict]:
        with self._factory() as db:
            rows = db.scalars(select(SkillEvolutionVersionRow).where(
                SkillEvolutionVersionRow.skill_name == name)).all()
            result = []
            for row in rows:
                self._record(db, row.candidate_id, owner_id)
                metrics = db.get(SkillRuntimeMetricRow, row.skill_version_id)
                result.append({"skill_version_id": row.skill_version_id,
                    "activation_mode": row.activation_mode,
                    "selection_count": metrics.selection_count,
                    "successful_task_count": metrics.successful_task_count,
                    "failure_count": metrics.failure_count,
                    "user_acceptance_count": metrics.user_acceptance_count,
                    "user_rejection_count": metrics.user_rejection_count,
                    "verifier_failure_count": metrics.verifier_failure_count,
                    "unauthorized_tool_attempt_count": metrics.unauthorized_tool_attempt_count,
                    "unsupported_claim_count": metrics.unsupported_claim_count,
                    "average_latency": (metrics.total_latency_seconds / metrics.selection_count
                        if metrics.selection_count else None),
                    "tokens": metrics.total_input_tokens + metrics.total_output_tokens,
                    "estimated_cost": metrics.total_estimated_cost})
            return result

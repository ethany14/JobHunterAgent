"""Local-only human review endpoints for generated procedural Skills."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agent_runtime.skills.evolution_types import ActivationMode
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api", tags=["skill-evolution"])


class Mutation(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MaterializeRequest(Mutation):
    skill_name: str = Field(min_length=1, max_length=63)
    semantic_version: str = "1.0.0"


class EvaluationRequest(Mutation):
    repetitions: int = Field(default=3, ge=1, le=5)


class PublishRequest(Mutation):
    acknowledge_soft_regressions: bool = False


class ActivationRequest(Mutation):
    version_id: str
    mode: ActivationMode


class RollbackRequest(Mutation):
    failed_version_id: str
    target_version_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


def _service(runtime: SessionRuntime):
    if runtime.skill_evolution is None:
        raise RuntimeError("Skill evolution is unavailable.")
    return runtime.skill_evolution


def _owner(runtime: SessionRuntime) -> str:
    return runtime.owner_resolver.resolve().owner_id


@router.post("/skill-candidates/{candidate_id}/materialize")
def materialize(candidate_id: str, request: MaterializeRequest,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).materialize(candidate_id, owner_id=_owner(runtime),
        expected_version=request.expected_version, idempotency_key=request.idempotency_key,
        skill_name=request.skill_name, semantic_version=request.semantic_version)


@router.get("/skill-candidates/{candidate_id}/staged")
def staged(candidate_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).preview(candidate_id, owner_id=_owner(runtime))


@router.post("/skill-candidates/{candidate_id}/restage")
def restage(candidate_id: str, request: Mutation,
            runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).restage(candidate_id, owner_id=_owner(runtime),
        expected_version=request.expected_version, idempotency_key=request.idempotency_key)


@router.post("/skill-candidates/{candidate_id}/evaluations")
def evaluate(candidate_id: str, request: EvaluationRequest,
             runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).evaluate(candidate_id, owner_id=_owner(runtime),
        expected_version=request.expected_version, idempotency_key=request.idempotency_key,
        repetitions=request.repetitions)


@router.get("/skill-evaluations/{evaluation_run_id}")
def evaluation(evaluation_run_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).get_evaluation(evaluation_run_id, owner_id=_owner(runtime))


@router.get("/skill-evaluations/{evaluation_run_id}/results")
def evaluation_results(evaluation_run_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).get_evaluation(evaluation_run_id, owner_id=_owner(runtime), include_results=True)


@router.post("/skill-candidates/{candidate_id}/publish")
def publish(candidate_id: str, request: PublishRequest,
            runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).publish(candidate_id, owner_id=_owner(runtime),
        expected_version=request.expected_version, idempotency_key=request.idempotency_key,
        acknowledge_soft_regressions=request.acknowledge_soft_regressions)


@router.post("/skill-candidates/{candidate_id}/reject")
def reject(candidate_id: str, request: Mutation,
           runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).reject(candidate_id, owner_id=_owner(runtime),
        expected_version=request.expected_version, idempotency_key=request.idempotency_key)


@router.get("/skills")
def skills(runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"skills": _service(runtime).skills(owner_id=_owner(runtime))}


@router.get("/skills/{skill_name}/versions")
def versions(skill_name: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"versions": _service(runtime).versions(skill_name, owner_id=_owner(runtime))}


@router.get("/skills/{skill_name}/versions/{version}")
def version(skill_name: str, version: str,
            runtime: SessionRuntime = Depends(get_session_runtime)):
    entries = _service(runtime).versions(skill_name, owner_id=_owner(runtime))
    from agent_runtime.skills.errors import SkillNotFoundError
    match = next((item for item in entries if item["semantic_version"] == version), None)
    if match is None:
        raise SkillNotFoundError("Published Skill version was not found.")
    return match


@router.post("/skills/{skill_name}/activate")
def activate(skill_name: str, request: ActivationRequest,
             runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).activate(skill_name, owner_id=_owner(runtime),
        version_id=request.version_id, mode=request.mode,
        expected_version=request.expected_version, idempotency_key=request.idempotency_key)


@router.post("/skills/{skill_name}/rollback")
def rollback(skill_name: str, request: RollbackRequest,
             runtime: SessionRuntime = Depends(get_session_runtime)):
    return _service(runtime).rollback(skill_name, owner_id=_owner(runtime),
        failed_version_id=request.failed_version_id, target_version_id=request.target_version_id,
        expected_version=request.expected_version, idempotency_key=request.idempotency_key,
        reason=request.reason)


@router.get("/skills/{skill_name}/metrics")
def metrics(skill_name: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"metrics": _service(runtime).metrics(skill_name, owner_id=_owner(runtime))}

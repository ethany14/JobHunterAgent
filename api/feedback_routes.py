"""Local-only, owner-scoped learning candidate review routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_runtime.feedback.errors import FeedbackValidationError
from agent_runtime.feedback.types import CandidateType, FeedbackSourceType
from api.feedback_schemas import CandidateMutationRequest, CreateFeedbackRequest
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api", tags=["feedback-learning"])


def _service(runtime: SessionRuntime):
    if runtime.feedback is None:
        raise RuntimeError("Feedback Runtime is not initialized.")
    return runtime.feedback


def _owner(runtime: SessionRuntime) -> str:
    return runtime.owner_resolver.resolve().owner_id


def _candidate_public(value, repository=None):
    data = value.model_dump(mode="json", exclude={"owner_id", "scope_id", "review_action"})
    events = (repository.candidate_events(value.candidate_id, owner_id=value.owner_id)
              if repository is not None else [])
    data["applications_represented"] = sorted({item.application_id for item in events
                                                 if item.application_id and item.deleted_at is None})
    return data


def _event_public(value):
    return value.model_dump(mode="json", exclude={"owner_id", "context_metadata_json"})


@router.post("/feedback")
def create_feedback(request: CreateFeedbackRequest,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    if request.source_type not in {FeedbackSourceType.EXPLICIT_INSTRUCTION,
                                   FeedbackSourceType.MANUAL_FEEDBACK}:
        raise FeedbackValidationError("This feedback source is server-owned.")
    owner_id = _owner(runtime)
    if request.application_id is not None:
        if runtime.workspace is None:
            raise FeedbackValidationError("Application context is unavailable.")
        runtime.workspace.get_application(request.application_id)
    if request.session_id is not None:
        session = runtime.sessions.require(request.session_id)
        if session.user_id not in {None, owner_id}:
            raise FeedbackValidationError("Session is not in the local profile.")
    event, candidate = _service(runtime).record(owner_id=owner_id,
        source_type=request.source_type, source_action_id=request.source_action_id,
        original_content=request.content, application_id=request.application_id,
        session_id=request.session_id)
    return {"feedback": _event_public(event),
            "candidate": _candidate_public(candidate, _service(runtime).repository) if candidate else None}


@router.get("/feedback")
def list_feedback(runtime: SessionRuntime = Depends(get_session_runtime),
                  limit: int = Query(default=100, ge=1, le=100)):
    return {"events": [_event_public(item) for item in
        _service(runtime).repository.list_events(owner_id=_owner(runtime), limit=limit)]}


@router.delete("/feedback/{event_id}")
def delete_feedback(event_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    service = _service(runtime)
    event = service.repository.soft_delete(event_id, owner_id=_owner(runtime),
        thresholds=service.thresholds)
    return _event_public(event)


@router.get("/learning-candidates")
def list_candidates(runtime: SessionRuntime = Depends(get_session_runtime),
                    candidate_type: CandidateType | None = None):
    return {"candidates": [_candidate_public(item, _service(runtime).repository) for item in
        _service(runtime).repository.list_candidates(owner_id=_owner(runtime),
            candidate_type=candidate_type)]}


@router.get("/learning-candidates/{candidate_id}")
def get_candidate(candidate_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    repository = _service(runtime).repository
    return _candidate_public(repository.candidate(candidate_id,
        owner_id=_owner(runtime)), repository)


@router.get("/learning-candidates/{candidate_id}/events")
def candidate_events(candidate_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"events": [_event_public(item) for item in
        _service(runtime).repository.candidate_events(candidate_id, owner_id=_owner(runtime))]}


@router.get("/learning-candidates/{candidate_id}/conflicts")
def candidate_conflicts(candidate_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"conflicts": [_candidate_public(item, _service(runtime).repository) for item in
        _service(runtime).repository.conflicts(candidate_id, owner_id=_owner(runtime))]}


def _mutate(candidate_id: str, action: str, request: CandidateMutationRequest,
            runtime: SessionRuntime):
    candidate = _service(runtime).review(candidate_id, owner_id=_owner(runtime),
        action=action, expected_version=request.expected_version,
        idempotency_key=request.idempotency_key, content=request.content)
    return _candidate_public(candidate, _service(runtime).repository)


@router.post("/learning-candidates/{candidate_id}/confirm")
def confirm_candidate(candidate_id: str, request: CandidateMutationRequest,
                      runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutate(candidate_id, "confirm", request, runtime)


@router.post("/learning-candidates/{candidate_id}/edit")
def edit_candidate(candidate_id: str, request: CandidateMutationRequest,
                   runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutate(candidate_id, "edit", request, runtime)


@router.post("/learning-candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, request: CandidateMutationRequest,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutate(candidate_id, "reject", request, runtime)


@router.post("/learning-candidates/{candidate_id}/keep-collecting")
def keep_collecting(candidate_id: str, request: CandidateMutationRequest,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutate(candidate_id, "keep-collecting", request, runtime)


@router.post("/learning-candidates/{candidate_id}/approve-for-evaluation")
def approve_for_evaluation(candidate_id: str, request: CandidateMutationRequest,
                           runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutate(candidate_id, "approve-for-evaluation", request, runtime)

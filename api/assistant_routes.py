"""Conversation-first Assistant timeline and registered action endpoints."""
from fastapi import APIRouter, Depends, Query

from agent_runtime.assistant.actions import AssistantActionService
from agent_runtime.assistant.projection import AssistantTimelineProjector
from agent_runtime.assistant.repository import AssistantTimelineRepository
from api.assistant_schemas import (
    AssistantActionRequest, AssistantActionResponse, PublicAssistantActivity,
    TimelineResponse,
)
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api/assistant-sessions", tags=["career-copilot"])


def _timeline(runtime: SessionRuntime) -> AssistantTimelineRepository:
    runtime.sessions.require  # fail clearly if Session Runtime is incomplete
    return AssistantTimelineRepository(runtime.database.session_factory)


@router.get("/{session_id}/timeline", response_model=TimelineResponse)
def timeline(session_id: str, after_sequence: int = Query(default=0, ge=0),
             limit: int = Query(default=100, ge=1, le=200),
             runtime: SessionRuntime = Depends(get_session_runtime)):
    runtime.sessions.require(session_id)
    repository = _timeline(runtime)
    AssistantTimelineProjector(repository).refresh(session_id)
    activities = repository.list(session_id, after_sequence=after_sequence, limit=limit)
    return TimelineResponse(activities=[PublicAssistantActivity.model_validate(
        item.model_dump(mode="python"))
        for item in activities], next_sequence=(activities[-1].sequence
        if activities else after_sequence))


@router.post("/{session_id}/actions", response_model=AssistantActionResponse)
def action(session_id: str, body: AssistantActionRequest,
           runtime: SessionRuntime = Depends(get_session_runtime)):
    result = AssistantActionService(runtime, _timeline(runtime)).execute(
        session_id, action_type=body.action_type, utterance=body.utterance,
        idempotency_key=body.idempotency_key, expected_version=body.expected_version,
        payload=body.payload)
    return AssistantActionResponse.model_validate(result)

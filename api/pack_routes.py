"""Synchronous local Pack APIs; model calls stay off the async event loop."""
from fastapi import APIRouter, Depends

from api.pack_schemas import EditItemRequest, GenerateRequest, PackMutation, QuestionRequest
from api.session_dependencies import SessionRuntime, get_session_runtime
from agent_runtime.feedback.types import FeedbackSourceType
from api.feedback_instrumentation import record_action


router = APIRouter(prefix="/api", tags=["application-pack"])


def _runtime(runtime: SessionRuntime):
    if runtime.pack_workflow is None or runtime.packs is None:
        raise RuntimeError("Application Pack runtime is unavailable.")
    return runtime.pack_workflow, runtime.packs


def _detail(pack_id: str, runtime: SessionRuntime) -> dict:
    workflow, repository = _runtime(runtime)
    pack = workflow.refresh_staleness(pack_id)
    items = repository.items(pack_id)
    return {"pack": pack.model_dump(mode="json"),
            "items": [{**item.model_dump(mode="json"),
                       "content": repository.artifact(pack_id, item.pack_item_id)}
                      for item in items]}


@router.post("/applications/{application_id}/packs")
def create_pack(application_id: str, body: PackMutation,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, _ = _runtime(runtime)
    pack = workflow.create(application_id, expected_version=body.expected_version,
                           idempotency_key=body.idempotency_key)
    return _detail(pack.pack_id, runtime)


@router.get("/applications/{application_id}/packs")
def list_packs(application_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, repository = _runtime(runtime)
    rows = [workflow.refresh_staleness(item.pack_id).model_dump(mode="json")
            for item in repository.list_for_application(application_id)]
    return {"packs": rows}


@router.get("/packs/{pack_id}")
def get_pack(pack_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _detail(pack_id, runtime)


def _generate(pack_id: str, artifact_type: str, body, runtime: SessionRuntime,
              *, question: str | None = None):
    workflow, _ = _runtime(runtime)
    workflow.generate(pack_id, artifact_type=artifact_type,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key,
        max_length=body.max_length, question=question)
    return _detail(pack_id, runtime)


@router.post("/packs/{pack_id}/resume")
def generate_resume(pack_id: str, body: GenerateRequest,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    return _generate(pack_id, "tailored_resume", body, runtime)


@router.post("/packs/{pack_id}/cover-letter")
def generate_cover_letter(pack_id: str, body: GenerateRequest,
                          runtime: SessionRuntime = Depends(get_session_runtime)):
    return _generate(pack_id, "cover_letter", body, runtime)


@router.post("/packs/{pack_id}/questions")
def generate_answer(pack_id: str, body: QuestionRequest,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    return _generate(pack_id, "application_answer", body, runtime, question=body.question)


@router.post("/packs/{pack_id}/items/{item_id}/regenerate")
def regenerate(pack_id: str, item_id: str, body: PackMutation,
               runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, _ = _runtime(runtime)
    workflow.regenerate(pack_id, item_id, expected_version=body.expected_version,
                        idempotency_key=body.idempotency_key)
    record_action(runtime, source_type=(FeedbackSourceType.MANUAL_FEEDBACK if body.feedback
        else FeedbackSourceType.ARTIFACT_REJECTED),
        source_action_id=f"pack-regenerate:{body.idempotency_key}",
        content=body.feedback or "Regeneration requested without an explicit reason.", artifact_id=item_id)
    return _detail(pack_id, runtime)


@router.post("/packs/{pack_id}/items/{item_id}/edit")
def edit(pack_id: str, item_id: str, body: EditItemRequest,
         runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, repository = _runtime(runtime)
    before = repository.artifact(pack_id, item_id)
    workflow.edit(pack_id, item_id, expected_version=body.expected_version,
                  content=body.content, idempotency_key=body.idempotency_key)
    record_action(runtime, source_type=FeedbackSourceType.ARTIFACT_EDITED,
        source_action_id=f"pack-edit:{body.idempotency_key}",
        before=str(before)[:20000] if before is not None else None,
        after=str(body.content)[:20000], artifact_id=item_id)
    return _detail(pack_id, runtime)


@router.post("/packs/{pack_id}/items/{item_id}/approve")
def approve(pack_id: str, item_id: str, body: PackMutation,
            runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, repository = _runtime(runtime)
    workflow.refresh_staleness(pack_id)
    repository.review(pack_id, item_id, expected_version=body.expected_version,
                      approve=True, idempotency_key=body.idempotency_key)
    record_action(runtime, source_type=FeedbackSourceType.ARTIFACT_ACCEPTED,
        source_action_id=f"pack-approve:{body.idempotency_key}",
        content="User approved this artifact.", artifact_id=item_id,
        context_metadata_json={"artifact_type":repository.item(pack_id, item_id).artifact_type.value})
    return _detail(pack_id, runtime)


@router.post("/packs/{pack_id}/items/{item_id}/reject")
def reject(pack_id: str, item_id: str, body: PackMutation,
           runtime: SessionRuntime = Depends(get_session_runtime)):
    workflow, repository = _runtime(runtime)
    workflow.refresh_staleness(pack_id)
    repository.review(pack_id, item_id, expected_version=body.expected_version,
                      approve=False, idempotency_key=body.idempotency_key)
    record_action(runtime, source_type=FeedbackSourceType.ARTIFACT_REJECTED,
        source_action_id=f"pack-reject:{body.idempotency_key}",
        content="User rejected this artifact without a replacement instruction.", artifact_id=item_id)
    return _detail(pack_id, runtime)


@router.get("/packs/{pack_id}/items/{item_id}/evidence")
def item_evidence(pack_id: str, item_id: str,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    _, repository = _runtime(runtime)
    item = repository.item(pack_id, item_id)
    content = repository.artifact(pack_id, item_id) or {}
    cited = set()
    def collect(value):
        if isinstance(value, dict):
            cited.update(value.get("evidence_ids", []))
            cited.update(value.get("evidence_version_ids", []))
            for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(content)
    snapshot = repository.snapshot(pack_id)
    return {"evidence": [{"evidence_id": value.evidence_id,
        "evidence_version_id": value.evidence_version_id,
        "claim_text": value.claim_text, "source_type": value.source_type,
        "source_section": value.source_section, "version_hash": value.content_hash,
        "confirmation_label": "Confirmed at generation"}
        for value in snapshot.items if value.evidence_id in cited or value.evidence_version_id in cited]}


@router.get("/packs/{pack_id}/items/{item_id}/versions")
def item_versions(pack_id: str, item_id: str,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    _, repository = _runtime(runtime)
    return {"versions": repository.versions(pack_id, item_id)}


@router.get("/packs/{pack_id}/events")
def pack_events(pack_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    _, repository = _runtime(runtime)
    return {"events": [event.model_dump(mode="json") for event in repository.events(pack_id)]}

"""Local-only Career Evidence APIs. The server controls identity and provenance."""
from fastapi import APIRouter, Depends, Query

from agent_runtime.evidence.types import EvidenceCategory, EvidenceStatus
from api.evidence_schemas import (
    CreateEvidenceCandidate, EvidenceMutation, LinkEvidence,
    ProposeEvidenceRevision, RejectEvidence,
)
from api.session_dependencies import SessionRuntime, get_session_runtime
from agent_runtime.feedback.types import FeedbackSourceType
from api.feedback_instrumentation import record_action

router = APIRouter(prefix="/api", tags=["career-evidence"])


def _public(item):
    data = item.model_dump(mode="json")
    # Internal source identifiers and source document locations stay server-side.
    data["current"].pop("source_reference", None)
    data["current"].pop("source_run_id", None)
    return data


def _public_version(version):
    data = version.model_dump(mode="json")
    data.pop("source_reference", None)
    data.pop("source_run_id", None)
    return data


def _repo(runtime: SessionRuntime):
    if runtime.evidence is None:
        raise RuntimeError("Career Evidence Vault is unavailable.")
    return runtime.evidence


@router.post("/evidence/candidates")
def create_candidate(body: CreateEvidenceCandidate,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    # Resume imports require an original resume held server-side; users cannot
    # claim deterministic resume provenance by posting an unchecked quote.
    if body.source_type.value == "resume":
        from agent_runtime.evidence.errors import EvidenceProvenanceError
        raise EvidenceProvenanceError("Use the server-side resume import for resume evidence.")
    return _public(_repo(runtime).create_candidate(**body.model_dump(mode="python"),
                                                   created_by="local-user"))


@router.get("/evidence")
def list_evidence(status: EvidenceStatus | None = None,
                  category: EvidenceCategory | None = None,
                  search: str | None = None, limit: int = Query(100, ge=1, le=500),
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"items": [_public(item) for item in _repo(runtime).list(
        status=status, category=category, search=search, limit=limit)]}


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_repo(runtime).get(evidence_id))


@router.get("/evidence/{evidence_id}/versions")
def evidence_versions(evidence_id: str,
                      runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"versions": [_public_version(item) for item in _repo(runtime).list_versions(evidence_id)]}


@router.get("/evidence/{evidence_id}/events")
def evidence_events(evidence_id: str,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"events": _repo(runtime).list_events(evidence_id)}


@router.post("/evidence/{evidence_id}/confirm")
def confirm_evidence(evidence_id: str, body: EvidenceMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    item = _repo(runtime).confirm(evidence_id, body.expected_version)
    record_action(runtime, source_type=FeedbackSourceType.EVIDENCE_CONFIRMED,
        source_action_id=f"evidence-confirm:{evidence_id}:{body.expected_version}",
        content=item.current.claim_text)
    return _public(item)


@router.post("/evidence/{evidence_id}/reject")
def reject_evidence(evidence_id: str, body: RejectEvidence,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    item = _repo(runtime).reject(evidence_id, body.expected_version, body.reason)
    record_action(runtime, source_type=FeedbackSourceType.EVIDENCE_REJECTED,
        source_action_id=f"evidence-reject:{evidence_id}:{body.expected_version}",
        content=body.reason or "User rejected the evidence candidate.")
    return _public(item)


@router.post("/evidence/{evidence_id}/revisions")
def propose_revision(evidence_id: str, body: ProposeEvidenceRevision,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_repo(runtime).propose_revision(evidence_id, body.changes,
                                                  body.expected_version))


@router.post("/evidence/{evidence_id}/archive")
def archive_evidence(evidence_id: str, body: EvidenceMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_repo(runtime).archive(evidence_id, body.expected_version))


@router.post("/evidence/{evidence_id}/restore")
def restore_evidence(evidence_id: str, body: EvidenceMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_repo(runtime).restore(evidence_id, body.expected_version))


@router.post("/applications/{application_id}/evidence-links")
def link_evidence(application_id: str, body: LinkEvidence,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    return _repo(runtime).link_to_application(
        body.evidence_id, application_id, expected_version=body.expected_version,
        link_type=body.link_type, requirement_id=body.requirement_id,
        canonical_requirement=body.canonical_requirement,
    )


@router.get("/applications/{application_id}/evidence")
def application_evidence(application_id: str,
                         runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"links": _repo(runtime).list_for_application(application_id)}


@router.delete("/applications/{application_id}/evidence-links/{link_id}")
def unlink_evidence(application_id: str, link_id: str,
                    expected_version: int = Query(ge=1),
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    _repo(runtime).unlink_from_application(application_id, link_id,
                                           expected_version=expected_version)
    return {"status": "unlinked"}

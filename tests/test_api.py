import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from api.main import app
from api.routes.runs import get_run_service
from api.schemas.runs import CreateRunResponse, RunResponse
from api.services.run_service import InvalidRunStateError, RunNotFoundError, RunService
from job_agent.nodes import make_evidence_id
from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    ResumeEvidence,
    SkillMatch,
    SupportedClaim,
    TailoredResume,
    VerificationResult,
)

FACT = "Built Python APIs."
FACT_ID = make_evidence_id(FACT)


class FakeRunService:
    def __init__(self) -> None:
        self.review_request = None

    async def create_run(self, request):
        return CreateRunResponse(run_id="test-run-id", status="awaiting_review")

    async def get_run(self, run_id):
        if run_id == "missing":
            raise RunNotFoundError("Run 'missing' was not found.")
        return RunResponse(
            run_id=run_id,
            status="awaiting_review",
            result={"tailored_resume": {"professional_summary": []}},
        )

    async def review_run(self, run_id, request):
        if run_id == "not-reviewable":
            raise InvalidRunStateError("Run is not awaiting review.")
        self.review_request = request
        status = "approved" if request.approved else "awaiting_review"
        return RunResponse(run_id=run_id, status=status, result={"revision_count": 1})


@pytest.fixture
def api_client():
    service = FakeRunService()
    app.dependency_overrides[get_run_service] = lambda: service
    with TestClient(app) as client:
        yield client, service
    app.dependency_overrides.clear()


def test_health(api_client):
    client, _ = api_client
    assert client.get("/health").json() == {"status": "ok"}


def test_create_run_uses_fake_service_without_llm(api_client):
    client, _ = api_client
    with patch("job_agent.nodes.ChatOpenAI") as model:
        response = client.post(
            "/runs",
            json={"resume_text": FACT, "job_description": "Requires Python"},
        )
    assert response.status_code == 201
    assert response.json() == {
        "run_id": "test-run-id",
        "status": "awaiting_review",
    }
    model.assert_not_called()


def test_create_run_rejects_whitespace_input(api_client):
    client, _ = api_client
    response = client.post(
        "/runs", json={"resume_text": "  ", "job_description": "Python"}
    )
    assert response.status_code == 422


def test_get_run_and_not_found(api_client):
    client, _ = api_client
    assert client.get("/runs/test-run-id").json()["status"] == "awaiting_review"
    response = client.get("/runs/missing")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_approve_run(api_client):
    client, service = api_client
    response = client.post(
        "/runs/test-run-id/review",
        json={"approved": True, "feedback": None},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert service.review_request.approved is True


def test_rejection_requires_feedback(api_client):
    client, _ = api_client
    response = client.post(
        "/runs/test-run-id/review",
        json={"approved": False, "feedback": " "},
    )
    assert response.status_code == 422
    assert "feedback is required" in str(response.json())


def test_rejection_with_feedback_returns_to_review(api_client):
    client, service = api_client
    response = client.post(
        "/runs/test-run-id/review",
        json={"approved": False, "feedback": "Emphasize Python."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_review"
    assert service.review_request.feedback == "Emphasize Python."


def test_review_conflict_returns_409(api_client):
    client, _ = api_client
    response = client.post(
        "/runs/not-reviewable/review", json={"approved": True}
    )
    assert response.status_code == 409


def graph_state(*, interrupted: bool, approved: bool | None, revision_count: int = 0):
    resume_analysis = ResumeAnalysis(
        summary=FACT,
        skills=["Python"],
        evidence=[
            ResumeEvidence(
                evidence_id=FACT_ID,
                source_section="Experience",
                exact_text=FACT,
            )
        ],
        education=[],
    )
    state = {
        "resume_analysis": resume_analysis.model_dump(mode="json"),
        "job_analysis": JobAnalysis(
            title="Backend Engineer",
            summary="Requires Python",
            requirements=[],
            responsibilities=[],
        ).model_dump(mode="json"),
        "skill_match": SkillMatch(
            matches=[],
            explanation="Python is supported.",
            recommendations=[],
            missing_required_requirements=[],
            missing_preferred_requirements=[],
            overall_score=100,
        ).model_dump(mode="json"),
        "tailored_resume": TailoredResume(
            professional_summary=[SupportedClaim(text=FACT, evidence_ids=[FACT_ID])],
            experience_bullets=[],
            highlighted_skills=[],
        ).model_dump(mode="json"),
        "verification": VerificationResult(
            passed=True, unsupported_claims=[], revision_feedback=[]
        ).model_dump(mode="json"),
        "revision_feedback": [],
        "revision_count": revision_count,
        "max_revisions": 3,
        "approved": approved,
        "human_feedback": None,
        "workflow_status": "approved" if approved else "running",
    }
    if interrupted:
        state["__interrupt__"] = (
            SimpleNamespace(value={"question": "Approve?"}),
        )
    return state


class FakeGraph:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, value, *, config):
        self.calls.append((value, config))
        if isinstance(value, Command):
            if value.resume["approved"]:
                return graph_state(interrupted=False, approved=True)
            return graph_state(interrupted=True, approved=None, revision_count=1)
        return graph_state(interrupted=True, approved=None)


class FailingGraph:
    def invoke(self, value, *, config):
        raise RuntimeError("provider unavailable")


def test_run_service_uses_run_id_as_thread_id_and_resumes_approval():
    fake_graph = FakeGraph()
    service = RunService(graph=fake_graph)
    created = asyncio.run(
        service.create_run(
            SimpleNamespace(resume_text=FACT, job_description="Requires Python")
        )
    )
    assert created.status == "awaiting_review"
    assert fake_graph.calls[0][1]["configurable"]["thread_id"] == created.run_id
    reviewed = asyncio.run(
        service.review_run(
            created.run_id,
            SimpleNamespace(approved=True, feedback=None),
        )
    )
    assert reviewed.status == "approved"
    assert fake_graph.calls[1][1]["configurable"]["thread_id"] == created.run_id


def test_run_service_rejection_revises_and_pauses_again():
    service = RunService(graph=FakeGraph())
    created = asyncio.run(
        service.create_run(
            SimpleNamespace(resume_text=FACT, job_description="Requires Python")
        )
    )
    reviewed = asyncio.run(
        service.review_run(
            created.run_id,
            SimpleNamespace(approved=False, feedback="Shorten summary."),
        )
    )
    assert reviewed.status == "awaiting_review"
    assert reviewed.result["revision_count"] == 1


def test_run_service_rejects_review_after_approval():
    service = RunService(graph=FakeGraph())
    created = asyncio.run(
        service.create_run(
            SimpleNamespace(resume_text=FACT, job_description="Requires Python")
        )
    )
    asyncio.run(
        service.review_run(
            created.run_id,
            SimpleNamespace(approved=True, feedback=None),
        )
    )
    with pytest.raises(InvalidRunStateError, match="not awaiting review"):
        asyncio.run(
            service.review_run(
                created.run_id,
                SimpleNamespace(approved=True, feedback=None),
            )
        )


def test_run_service_preserves_failed_status_and_error():
    service = RunService(graph=FailingGraph())
    created = asyncio.run(
        service.create_run(
            SimpleNamespace(resume_text=FACT, job_description="Requires Python")
        )
    )
    assert created.status == "failed"
    stored = asyncio.run(service.get_run(created.run_id))
    assert stored.status == "failed"
    assert stored.result is None
    assert stored.error == "provider unavailable"

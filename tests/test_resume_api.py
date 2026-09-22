from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_runtime.resumes.repository import ResumeDocumentRepository
from api.db import create_database
from api.main import create_app
from api.resume_routes import extract_pdf_text
from api.session_dependencies import get_session_runtime
from api.schemas.runs import CreateRunResponse, RunResponse


class OwnerResolver:
    def resolve(self):
        return SimpleNamespace(owner_id="local-user")


class FakeRunService:
    async def create_run(self, request):
        assert "Python" in request.resume_text
        return CreateRunResponse(run_id="quick-run", status="awaiting_review")

    async def get_run(self, run_id):
        return RunResponse(run_id=run_id, status="awaiting_review", result={
            "skill_match": {
                "overall_score": 75,
                "matches": [
                    {"display_name": "Python", "match_status": "matched"},
                    {"display_name": "Cloud", "match_status": "partial"},
                ],
                "missing_required_requirements": [{"display_name": "Kubernetes"}],
                "missing_preferred_requirements": [],
                "confirmation_requirements": [],
            }
        })


def test_resume_repository_default_lifecycle(tmp_path) -> None:
    database = create_database(f"sqlite:///{(tmp_path / 'resume.sqlite').as_posix()}", create_schema_for_tests=True)
    try:
        repository = ResumeDocumentRepository(database.session_factory)
        first = repository.create(owner_id="local-user", filename="one.pdf", display_name="One",
            content_sha256="a" * 64, extracted_text="Python developer", page_count=1)
        second = repository.create(owner_id="local-user", filename="two.pdf", display_name="Two",
            content_sha256="b" * 64, extracted_text="Data analyst", page_count=2)
        assert repository.default("local-user").resume_id == second.resume_id
        assert repository.set_default("local-user", first.resume_id).is_default is True
        repository.delete("local-user", first.resume_id)
        assert repository.default("local-user").resume_id == second.resume_id
    finally:
        database.close()


def test_upload_list_and_quick_analysis_use_server_resume(tmp_path, monkeypatch) -> None:
    database = create_database(f"sqlite:///{(tmp_path / 'api.sqlite').as_posix()}", create_schema_for_tests=True)
    runtime = SimpleNamespace(resumes=ResumeDocumentRepository(database.session_factory), owner_resolver=OwnerResolver())
    app = create_app(run_service=FakeRunService())
    app.dependency_overrides[get_session_runtime] = lambda: runtime
    monkeypatch.setattr("api.resume_routes.extract_pdf_text", lambda _content: ("Python developer", 1))
    try:
        with TestClient(app) as client:
            uploaded = client.post("/api/resumes", content=b"%PDF test", headers={
                "Content-Type": "application/pdf", "X-Resume-Filename": "resume.pdf"})
            assert uploaded.status_code == 201
            resume_id = uploaded.json()["resume_id"]
            assert client.get("/api/resumes").json()["resumes"][0]["is_default"] is True
            result = client.post("/api/quick-analysis", json={"job_description": "Requires Python"})
            assert result.status_code == 200
            assert result.json()["resume_id"] == resume_id
            assert result.json()["match_score"] == 75
            assert result.json()["suggestions"]
    finally:
        app.dependency_overrides.clear()
        database.close()


def test_quick_analysis_requires_a_default_resume(tmp_path) -> None:
    database = create_database(f"sqlite:///{(tmp_path / 'empty.sqlite').as_posix()}", create_schema_for_tests=True)
    runtime = SimpleNamespace(resumes=ResumeDocumentRepository(database.session_factory), owner_resolver=OwnerResolver())
    app = create_app(run_service=FakeRunService())
    app.dependency_overrides[get_session_runtime] = lambda: runtime
    try:
        with TestClient(app) as client:
            response = client.post("/api/quick-analysis", json={"job_description": "Requires Python"})
            assert response.status_code == 409
            assert "Upload a PDF resume" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        database.close()

"""SQLite persistence and checkpoint recovery tests without LLM calls."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from api.db import create_database
from api.main import create_app
from api.repositories.run_repository import RunRepository
from api.runtime import create_frozen_langgraph_run_service
from api.schemas.runs import CreateRunRequest, ReviewRequest
from api.services.run_service import SAFE_RUN_ERROR, InvalidRunStateError, RunNotFoundError


class StubState(TypedDict):
    resume_text: str
    job_description: str
    revision_count: NotRequired[int]
    approved: NotRequired[bool | None]
    human_feedback: NotRequired[str | None]
    workflow_status: NotRequired[str]


def build_test_graph() -> StateGraph:
    graph = StateGraph(StubState)

    def prepare(_: StubState) -> dict:
        return {
            "revision_count": 0,
            "approved": None,
            "human_feedback": None,
            "workflow_status": "running",
        }

    def human_review(_: StubState) -> dict:
        decision = interrupt({"question": "Approve this test resume?"})
        approved = bool(decision["approved"])
        return {
            "approved": approved,
            "human_feedback": decision.get("feedback"),
            "workflow_status": "approved" if approved else "revision_requested",
        }

    def route(state: StubState) -> str:
        return "end" if state.get("approved") else "revise"

    def revise(state: StubState) -> dict:
        return {
            "revision_count": state["revision_count"] + 1,
            "approved": None,
            "human_feedback": None,
            "workflow_status": "running",
        }

    graph.add_node("prepare", prepare)
    graph.add_node("human_review", human_review)
    graph.add_node("revise", revise)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "human_review")
    graph.add_conditional_edges(
        "human_review", route, {"end": END, "revise": "revise"}
    )
    graph.add_edge("revise", "human_review")
    return graph


def serialize_test_state(state: dict) -> dict:
    return {
        "revision_count": state.get("revision_count", 0),
        "approved": state.get("approved"),
        "human_feedback": state.get("human_feedback"),
        "workflow_status": state.get("workflow_status", "running"),
    }


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def new_service(tmp_path: Path):
    return create_frozen_langgraph_run_service(
        database_url=sqlite_url(tmp_path / "runs.sqlite"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        graph_builder=build_test_graph(),
        result_serializer=serialize_test_state,
    )


def create_paused_run(service) -> str:
    created = asyncio.run(
        service.create_run(
            CreateRunRequest(
                resume_text="Built Python APIs.",
                job_description="Requires Python.",
            )
        )
    )
    assert created.status == "awaiting_review"
    return created.run_id


def test_created_run_is_queryable_from_database(tmp_path):
    service = new_service(tmp_path)
    try:
        run_id = create_paused_run(service)
        database = create_database(sqlite_url(tmp_path / "runs.sqlite"))
        try:
            row = RunRepository(database.session_factory).get(run_id)
        finally:
            database.close()
        assert row is not None
        assert row.run_id == row.thread_id == run_id
        assert row.status == "awaiting_review"
        assert row.result["revision_count"] == 0
    finally:
        service.close()


def test_repository_none_result_preserves_latest_stable_projection(tmp_path):
    database = create_database(
        sqlite_url(tmp_path / "projection.sqlite"), create_schema_for_tests=True
    )
    repository = RunRepository(database.session_factory)
    try:
        repository.create(
            run_id="projection-run",
            thread_id="projection-run",
            resume_text="Resume",
            job_description="Job",
        )
        repository.update(
            "projection-run",
            status="awaiting_review",
            result={"tailored_resume": "stable"},
            error_message=None,
        )
        revising = repository.update(
            "projection-run",
            status="revising",
            result=None,
            error_message=None,
        )
        assert revising.status == "revising"
        assert revising.result == {"tailored_resume": "stable"}

        failed = repository.update(
            "projection-run",
            status="failed",
            result=None,
            error_message=SAFE_RUN_ERROR,
        )
        assert failed.status == "failed"
        assert failed.result == {"tailored_resume": "stable"}
    finally:
        database.close()


def test_new_application_instance_reads_and_approves_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        with TestClient(create_app(restarted_service)) as client:
            stored = client.get(f"/runs/{run_id}")
            approved = client.post(
                f"/runs/{run_id}/review", json={"approved": True}
            )
        assert stored.status_code == 200
        assert stored.json()["status"] == "awaiting_review"
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
    finally:
        restarted_service.close()


def test_reject_with_feedback_resumes_checkpoint_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        reviewed = asyncio.run(
            restarted_service.review_run(
                run_id,
                ReviewRequest(approved=False, feedback="Shorten the summary."),
            )
        )
        assert reviewed.status == "awaiting_review"
        assert reviewed.result["revision_count"] == 1
    finally:
        restarted_service.close()


def test_missing_run_is_404_with_new_application(tmp_path):
    service = new_service(tmp_path)
    try:
        with TestClient(create_app(service)) as client:
            response = client.get("/runs/missing")
        assert response.status_code == 404
    finally:
        service.close()


def test_completed_run_cannot_be_reviewed_again_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    asyncio.run(
        first_service.review_run(run_id, ReviewRequest(approved=True, feedback=None))
    )
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        with pytest.raises(InvalidRunStateError, match="not awaiting review"):
            asyncio.run(
                restarted_service.review_run(
                    run_id, ReviewRequest(approved=True, feedback=None)
                )
            )
    finally:
        restarted_service.close()


def test_failed_status_is_persisted_without_internal_error_details(tmp_path):
    graph = StateGraph(StubState)

    def fail(_: StubState) -> dict:
        raise RuntimeError("secret provider details and api key")

    graph.add_node("fail", fail)
    graph.add_edge(START, "fail")
    graph.add_edge("fail", END)
    service = create_frozen_langgraph_run_service(
        database_url=sqlite_url(tmp_path / "runs.sqlite"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        graph_builder=graph,
        result_serializer=serialize_test_state,
    )
    run_id = asyncio.run(
        service.create_run(
            CreateRunRequest(resume_text="Resume", job_description="Job")
        )
    ).run_id
    service.close()

    restarted_service = new_service(tmp_path)
    try:
        stored = asyncio.run(restarted_service.get_run(run_id))
        assert stored.status == "failed"
        assert stored.error == SAFE_RUN_ERROR
        assert "secret" not in stored.error
        with pytest.raises(InvalidRunStateError):
            asyncio.run(
                restarted_service.review_run(
                    run_id, ReviewRequest(approved=True, feedback=None)
                )
            )
    finally:
        restarted_service.close()


def test_repository_missing_run_raises_service_not_found(tmp_path):
    service = new_service(tmp_path)
    try:
        with pytest.raises(RunNotFoundError):
            asyncio.run(service.get_run("missing"))
    finally:
        service.close()

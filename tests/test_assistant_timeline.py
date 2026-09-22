from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from agent_runtime.assistant.actions import AssistantActionConflictError, AssistantActionService
from agent_runtime.assistant.projection import AssistantTimelineProjector
from agent_runtime.assistant.repository import AssistantTimelineRepository
from agent_runtime.assistant.types import AssistantActivityType, RegisteredAssistantAction
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionMessageDraft, SessionState
from agent_runtime.tools.messages import AgentMessage
from api.db import create_database, upgrade_database
from api.main import create_app


def runtime(tmp_path):
    database = create_database(f"sqlite:///{tmp_path / 'assistant.db'}",
                               create_schema_for_tests=True)
    sessions = SessionRepository(database.session_factory)
    state = SessionState(session_id="session-1", title="Acme Engineer")
    sessions.create(state, SessionEvent(session_id=state.session_id,
        event_type=SessionEventType.SESSION_CREATED), messages=[
        SessionMessageDraft(message=AgentMessage(message_id="system-1", role="system",
            content="private system policy")),
        SessionMessageDraft(message=AgentMessage(message_id="user-1", role="user",
            content="Help me apply for this job")),
        SessionMessageDraft(message=AgentMessage(message_id="assistant-1", role="assistant",
            content="I can help with this Workspace.")),
    ])
    value = SimpleNamespace(database=database, sessions=sessions,
        workspace=SimpleNamespace(application_id_for_session=lambda _id: None))
    return database, value


def test_timeline_projection_is_ordered_idempotent_and_private_safe(tmp_path):
    database, value = runtime(tmp_path)
    repository = AssistantTimelineRepository(database.session_factory)
    projector = AssistantTimelineProjector(repository)
    projector.refresh("session-1")
    first = repository.list("session-1")
    projector.refresh("session-1")
    second = repository.list("session-1")
    restarted_repository = AssistantTimelineRepository(database.session_factory)
    AssistantTimelineProjector(restarted_repository).refresh("session-1")
    after_restart = restarted_repository.list("session-1")
    assert [(x.sequence, x.reference_id) for x in first] == [
        (1, "user-1"), (2, "assistant-1")]
    assert [x.activity_id for x in first] == [x.activity_id for x in second]
    assert [x.activity_id for x in first] == [x.activity_id for x in after_restart]
    assert "private system policy" not in str([x.payload for x in second])
    assert [x.sequence for x in repository.list("session-1", after_sequence=1)] == [2]
    database.close()


def test_registered_action_idempotency_and_ambiguous_intent(tmp_path):
    database, value = runtime(tmp_path)
    timeline = AssistantTimelineRepository(database.session_factory)
    service = AssistantActionService(value, timeline)
    version = value.sessions.require("session-1").version
    ambiguous = service.execute("session-1", action_type=None, utterance="Please start something",
        idempotency_key="ambiguous", expected_version=version, payload={})
    assert ambiguous["status"] == "needs_clarification"
    first = service.execute("session-1", action_type=RegisteredAssistantAction.SAVE_CURRENT_JOB,
        utterance=None, idempotency_key="save", expected_version=version, payload={})
    replay = service.execute("session-1", action_type=RegisteredAssistantAction.SAVE_CURRENT_JOB,
        utterance=None, idempotency_key="save", expected_version=version, payload={})
    assert replay == first
    try:
        service.execute("session-1", action_type=RegisteredAssistantAction.SAVE_CURRENT_JOB,
            utterance=None, idempotency_key="save", expected_version=version,
            payload={"title": "different"})
        assert False
    except AssistantActionConflictError:
        pass
    assert timeline.list("session-1")[0].type == AssistantActivityType.ACTION_PROPOSAL
    database.close()


def test_application_pack_action_generates_a_cover_letter(tmp_path):
    database, value = runtime(tmp_path)
    calls = []
    value.workspace = SimpleNamespace(
        application_id_for_session=lambda _id: "application-1",
        get_application=lambda _id: SimpleNamespace(version=4),
    )
    value.pack_workflow = SimpleNamespace(
        create=lambda application_id, **kwargs: SimpleNamespace(
            pack_id="pack-1", version=2, status=SimpleNamespace(value="draft")),
        generate=lambda pack_id, **kwargs: (
            calls.append((pack_id, kwargs)) or SimpleNamespace(
                pack_item_id=("resume-item" if kwargs["artifact_type"] == "tailored_resume"
                              else "cover-item"), artifact_type=kwargs["artifact_type"],
                status=SimpleNamespace(value="awaiting_review"))),
    )
    value.packs = SimpleNamespace(get=lambda _id: SimpleNamespace(version=7))
    service = AssistantActionService(value,
        AssistantTimelineRepository(database.session_factory))
    result = service.execute("session-1",
        action_type=RegisteredAssistantAction.GENERATE_APPLICATION_PACK,
        utterance=None, idempotency_key="cover", expected_version=value.sessions.require(
            "session-1").version, payload={})
    assert result["status"] == "awaiting_review"
    assert calls == [
        ("pack-1", {"artifact_type": "tailored_resume", "expected_version": 2,
                    "idempotency_key": "cover:tailored-resume"}),
        ("pack-1", {"artifact_type": "cover_letter", "expected_version": 7,
                    "idempotency_key": "cover:cover-letter"}),
    ]
    database.close()


def test_artifact_review_updates_the_linked_pack_item(tmp_path):
    database, value = runtime(tmp_path)
    calls = []
    value.workspace = SimpleNamespace(
        application_id_for_session=lambda _id: "application-1",
        approve_artifact=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Pack artifacts must be reviewed through PackRepository")),
    )
    value.packs = SimpleNamespace(
        item_for_artifact=lambda artifact_id: SimpleNamespace(
            pack_id="pack-1", pack_item_id="item-1", version=4),
        review=lambda pack_id, item_id, **kwargs: (
            calls.append((pack_id, item_id, kwargs)) or SimpleNamespace(
                pack_id=pack_id, pack_item_id=item_id,
                status=SimpleNamespace(value="rejected"))),
    )
    service = AssistantActionService(value,
        AssistantTimelineRepository(database.session_factory))
    result = service.execute("session-1",
        action_type=RegisteredAssistantAction.REVIEW_ARTIFACT,
        utterance=None, idempotency_key="reject-cover",
        expected_version=value.sessions.require("session-1").version,
        payload={"artifact_id": "artifact-1", "decision": "reject"})
    assert result["status"] == "rejected"
    assert calls == [("pack-1", "item-1", {
        "expected_version": 4, "approve": False,
        "idempotency_key": "reject-cover"})]
    database.close()


def test_analysis_summary_is_plain_and_user_facing():
    job = SimpleNamespace(content_json='{"title":"Backend Engineer"}')
    match = SimpleNamespace(content_json='''{
      "overall_score": 75,
      "matches": [
        {"job_skill":"Python", "match_status":"matched"},
        {"job_skill":"AWS", "match_status":"missing"}
      ],
      "missing_required_requirements": [
        {"display_name":"AWS deployment"}
      ]
    }''')
    summary = AssistantTimelineProjector._analysis_summary(job, match)
    assert "Backend Engineer" in summary
    assert "75%" in summary
    assert "Python" in summary
    assert "AWS deployment" in summary


def test_mock_question_projection_reads_current_and_legacy_contracts():
    assert AssistantTimelineProjector._mock_question_text({
        "question_text": "Tell me about a difficult stakeholder conversation."
    }) == "Tell me about a difficult stakeholder conversation."
    assert AssistantTimelineProjector._mock_question_text({
        "question": "What was your role?"
    }) == "What was your role?"


def test_timeline_api_and_migration_from_previous_head(tmp_path):
    database, value = runtime(tmp_path)
    app = create_app(run_service=object(), session_runtime=value)
    with TestClient(app) as client:
        response = client.get("/api/assistant-sessions/session-1/timeline")
        assert response.status_code == 200, response.text
        assert [item["type"] for item in response.json()["activities"]] == [
            "user_message", "assistant_message"]
        cursor = response.json()["next_sequence"]
        assert client.get(f"/api/assistant-sessions/session-1/timeline?after_sequence={cursor}").json()["activities"] == []
        normal = client.post("/api/assistant-sessions/session-1/actions", json={
            "utterance": "What does this requirement mean?", "idempotency_key": "normal",
            "expected_version": value.sessions.require("session-1").version, "payload": {}})
        assert normal.status_code == 200
        assert normal.json()["status"] == "information_request"
        registered = client.post("/api/assistant-sessions/session-1/actions", json={
            "action_type": "save_current_job", "idempotency_key": "save-api",
            "expected_version": value.sessions.require("session-1").version, "payload": {}})
        assert registered.status_code == 200, registered.text
        assert registered.json()["status"] == "proposed"
    database.close()

    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0023_governed_skill_evolution")
    upgrade_database(url)
    migrated = create_database(url)
    with migrated.engine.connect() as connection:
        names = set(migrated.engine.dialect.get_table_names(connection))
    assert {"assistant_activities", "assistant_action_requests"} <= names
    migrated.close()

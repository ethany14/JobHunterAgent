from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from agent_runtime import ToolCallRepository, ToolExecutor, ToolRegistry
from agent_runtime.context.models import MemoryUsageEventRow, SkillUsageEventRow
from agent_runtime.context.projection import SessionContextProjector
from agent_runtime.context.repository import ContextSnapshotRepository
from agent_runtime.context.snapshots import (
    ContextBlockManifest,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSnapshotUnavailableError,
    MemorySnapshotRef,
)
from agent_runtime.context.types import ContextBlockKind
from agent_runtime.memory import (
    MemoryProvenance, MemoryRepository, MemoryScope, MemoryType,
)
from agent_runtime.memory.retrieval import MemoryRetriever
from agent_runtime.policy import ToolPolicy
from agent_runtime.sessions import SessionEvent, SessionEventType, SessionMessageDraft, SessionRepository, SessionState, SessionStatus
from agent_runtime.sessions.claims import SessionClaimRepository
from agent_runtime.sessions.clock import FakeClock
from agent_runtime.sessions.coordinator import SessionCoordinator
from agent_runtime.skills import SkillDiscovery, SkillLoader, SkillRegistry, SkillRepository, SkillStatus
from agent_runtime.skills.routing import SessionSkillProfile, SkillRouter
from agent_runtime.tools.loop import ToolCallingLoop
from agent_runtime.tools.messages import AgentMessage, NormalizedToolCall, TokenUsage, ToolModelResponse
from api.db import create_database


PROJECT_SKILL = Path("skills/project/job-run-analysis")
TOOLS = frozenset({
    "list_recent_runs", "get_run_result", "compare_run_requirements", "render_tailored_resume"
})


class ScriptedModel:
    def __init__(self, responses, hook=None):
        self.responses = list(responses)
        self.hook = hook
        self.calls = []

    def invoke(self, messages, tools, *, timeout_seconds=None):
        self.calls.append((messages, tools, timeout_seconds))
        if self.hook:
            self.hook()
        return self.responses.pop(0)


class CapturingModel(ScriptedModel):
    """Captures the exact provider-independent model invocation boundary."""


def response(message_id="assistant-1", content="done", calls=()):
    return ToolModelResponse(
        message=AgentMessage(
            message_id=message_id, role="assistant", content=content,
            tool_calls=list(calls),
        ),
        usage=TokenUsage(input_tokens=10, output_tokens=3, total_tokens=13),
    )


@pytest.fixture
def runtime(tmp_path):
    database = create_database(
        f"sqlite:///{(tmp_path / 'context.sqlite').as_posix()}", create_schema_for_tests=True
    )
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    sessions = SessionRepository(database.session_factory)
    snapshots = ContextSnapshotRepository(database.session_factory, clock=clock)
    memories = MemoryRepository(database.session_factory, clock=clock)
    skills = SkillRepository(database.session_factory, clock=clock)
    yield database, clock, sessions, snapshots, memories, skills
    database.close()


def activate_project_skill(skills, tmp_path):
    package = tmp_path / "packages" / "job-run-analysis"
    package.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(PROJECT_SKILL, package)
    registry = SkillRegistry(skills)
    validated = registry.register(package)
    pending = registry.request_approval(validated.version_id, expected_version=validated.version)
    approved = registry.approve(pending.version_id, expected_version=pending.version)
    return registry.activate(approved.version_id, expected_version=approved.version)


def create_running_session(sessions, *, allowed_skills=frozenset(), allowed_tools=TOOLS):
    state = SessionState(
        session_id="session-1", user_id="local-user", status=SessionStatus.RUNNING,
        allowed_tools=allowed_tools, allowed_skills=allowed_skills,
    )
    return sessions.create(
        state,
        SessionEvent(session_id=state.session_id, event_type=SessionEventType.SESSION_CREATED),
        messages=[
            SessionMessageDraft(message=AgentMessage(
                message_id="system-original", role="system", content="original system")),
            SessionMessageDraft(message=AgentMessage(
                message_id="user-1", role="user", content="Compare my recent job run requirements")),
        ],
    )


def projector(runtime, *, max_tokens=12_000):
    _, clock, sessions, snapshots, memories, skills = runtime
    return SessionContextProjector(
        sessions=sessions,
        snapshots=snapshots,
        system_policy="trusted system policy",
        memories=memories,
        memory_retriever=MemoryRetriever(memories, clock=clock),
        skills=skills,
        skill_router=SkillRouter(SkillDiscovery(skills), SkillLoader(skills)),
        max_input_tokens=max_tokens,
    )


def test_deterministic_routing_lifecycle_allowlist_and_tool_narrowing(runtime, tmp_path):
    _, _, _, _, _, skills = runtime
    active = activate_project_skill(skills, tmp_path)
    router = SkillRouter(SkillDiscovery(skills), SkillLoader(skills))
    route = router.route(
        text="compare recent job run requirements",
        profile=SessionSkillProfile(
            profile_name="readonly", allowed_skill_names=frozenset({"job-run-analysis"})
        ),
        runtime_allowed_tools=TOOLS | {"external-write"},
    )
    assert [item.skill.version_id for item in route] == [active.version_id]
    assert route[0].skill.effective_allowed_tools == TOOLS
    assert route == router.route(
        text="compare recent job run requirements",
        profile=SessionSkillProfile(
            profile_name="readonly", allowed_skill_names=frozenset({"job-run-analysis"})
        ),
        runtime_allowed_tools=TOOLS | {"external-write"},
    )
    assert router.route(
        text="compare job runs",
        profile=SessionSkillProfile(profile_name="none"),
        runtime_allowed_tools=TOOLS,
    ) == []


def test_explicit_skill_still_requires_allowlist_and_active_hash(runtime, tmp_path):
    _, _, _, _, _, skills = runtime
    active = activate_project_skill(skills, tmp_path)
    router = SkillRouter(SkillDiscovery(skills), SkillLoader(skills))
    with pytest.raises(Exception, match="unavailable or not permitted"):
        router.route(
            text="anything", profile=SessionSkillProfile(profile_name="none"),
            runtime_allowed_tools=TOOLS,
            explicit_skill_names=frozenset({"job-run-analysis"}),
        )
    Path(active.package_path, "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(Exception):
        router.route(
            text="anything",
            profile=SessionSkillProfile(
                profile_name="allowed", allowed_skill_names=frozenset({"job-run-analysis"})
            ),
            runtime_allowed_tools=TOOLS,
            explicit_skill_names=frozenset({"job-run-analysis"}),
        )


def test_memory_skill_context_order_and_snapshot_manifest(runtime, tmp_path):
    _, _, sessions, snapshots, memories, skills = runtime
    skill = activate_project_skill(skills, tmp_path)
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="preference.analysis", memory_type=MemoryType.PREFERENCE,
        display_text="Prefers comparison tables for job run analysis",
        content={"format": "comparison table"},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    confirmed = memories.confirm(
        candidate.memory_id, owner_id="local-user", expected_version=0,
        confirmed_by_user=True,
    )
    state = create_running_session(
        sessions, allowed_skills=frozenset({"job-run-analysis"})
    )
    prepared = projector(runtime).prepare(state)
    kinds = [block.kind for block in prepared.snapshot.block_manifests]
    assert kinds == [
        ContextBlockKind.SYSTEM_POLICY,
        ContextBlockKind.PERMISSION_EVIDENCE_POLICY,
        ContextBlockKind.SKILL_PROCEDURE,
        ContextBlockKind.ACTIVE_TASK,
        ContextBlockKind.MEMORY,
    ]
    assert prepared.snapshot.skill_versions[0].version_id == skill.version_id
    assert prepared.snapshot.memory_versions[0].memory_id == confirmed.memory_id
    assert prepared.snapshot.effective_tools == TOOLS
    assert "not resume evidence" in prepared.messages[0].content
    assert "When a confirmed user preference directly answers" in prepared.messages[0].content
    assert "instead of replacing it with generic advice" in prepared.messages[0].content
    assert snapshots.require(prepared.snapshot.snapshot_id).status == ContextSnapshotStatus.PREPARED


def test_snapshot_rejects_memory_references_without_included_blocks():
    with pytest.raises(ValueError, match="Memory references must match"):
        ContextSnapshot(
            session_id="session",
            system_prompt_version="v1",
            system_prompt_hash="0" * 64,
            memory_versions=[MemorySnapshotRef(memory_id="missing", version=1)],
            block_manifests=[ContextBlockManifest(
                block_id="system-policy",
                kind=ContextBlockKind.SYSTEM_POLICY,
                trust_level="trusted_policy",
                content_hash="1" * 64,
                estimated_tokens=1,
            )],
            estimated_input_tokens=1,
            context_hash="2" * 64,
            model_messages=[AgentMessage(
                message_id="system", role="system", content="policy"
            )],
        )


def test_required_source_artifact_is_hashed_and_reverified(runtime):
    _, _, sessions, snapshots, _, _ = runtime
    source = {"source-1": "grounded source evidence"}
    state = create_running_session(sessions)
    service = SessionContextProjector(
        sessions=sessions,
        snapshots=snapshots,
        system_policy="trusted system policy",
        source_resolver=lambda _state: list(source.items()),
    )
    prepared = service.prepare(state)
    assert prepared.snapshot.source_artifact_ids == ["source-1"]
    assert ContextBlockKind.REQUIRED_SOURCE_EVIDENCE in {
        item.kind for item in prepared.snapshot.block_manifests
    }
    source["source-1"] = "changed"
    with pytest.raises(ContextSnapshotUnavailableError, match="source content"):
        service.rebuild(state, prepared.snapshot.snapshot_id)


def test_conversation_tool_groups_are_never_split(runtime):
    _, _, sessions, _, _, _ = runtime
    state = sessions.create(
        SessionState(session_id="session-1", status=SessionStatus.RUNNING),
        SessionEvent(session_id="session-1", event_type=SessionEventType.SESSION_CREATED),
        messages=[
            SessionMessageDraft(message=AgentMessage(message_id="u", role="user", content="look up")),
            SessionMessageDraft(message=AgentMessage(
                message_id="a", role="assistant", tool_calls=[
                    NormalizedToolCall(tool_call_id="c1", tool_name="one"),
                    NormalizedToolCall(tool_call_id="c2", tool_name="two"),
                ])),
            SessionMessageDraft(message=AgentMessage(
                message_id="t1", role="tool", content="one", tool_call_id="c1", tool_name="one")),
            SessionMessageDraft(message=AgentMessage(
                message_id="t2", role="tool", content="two", tool_call_id="c2", tool_name="two")),
        ],
    )
    prepared = projector(runtime).prepare(state)
    included = set(prepared.snapshot.included_message_ids)
    assert {"a", "t1", "t2"} <= included
    assert [m.role for m in prepared.messages[-3:]] == ["assistant", "tool", "tool"]


def test_historical_tool_group_is_atomic_but_not_permanently_mandatory(runtime):
    _, _, sessions, snapshots, _, _ = runtime
    state = sessions.create(
        SessionState(session_id="session-1", status=SessionStatus.RUNNING),
        SessionEvent(session_id="session-1", event_type=SessionEventType.SESSION_CREATED),
        messages=[
            SessionMessageDraft(message=AgentMessage(
                message_id="old-user", role="user", content="old lookup")),
            SessionMessageDraft(message=AgentMessage(
                message_id="old-assistant-tool", role="assistant", tool_calls=[
                    NormalizedToolCall(tool_call_id="old-call", tool_name="lookup"),
                ])),
            SessionMessageDraft(message=AgentMessage(
                message_id="old-tool-result", role="tool", content="result " * 500,
                tool_call_id="old-call", tool_name="lookup")),
            SessionMessageDraft(message=AgentMessage(
                message_id="old-final", role="assistant", content="The lookup is complete.")),
            SessionMessageDraft(message=AgentMessage(
                message_id="active-user", role="user", content="Start a new task.")),
        ],
    )
    service = SessionContextProjector(
        sessions=sessions, snapshots=snapshots,
        system_policy="trusted system policy", max_input_tokens=250,
    )
    prepared = service.prepare(state)
    included = set(prepared.snapshot.included_message_ids)
    excluded = set(prepared.snapshot.excluded_message_ids)
    assert "active-user" in included
    assert {"old-assistant-tool", "old-tool-result"} <= excluded
    assert not ({"old-assistant-tool", "old-tool-result"} & included)


def test_incomplete_tool_group_fails_safely(runtime):
    _, _, sessions, _, _, _ = runtime
    state = sessions.create(
        SessionState(session_id="session-1", status=SessionStatus.RUNNING),
        SessionEvent(session_id="session-1", event_type=SessionEventType.SESSION_CREATED),
        messages=[SessionMessageDraft(message=AgentMessage(
            message_id="a", role="assistant", tool_calls=[
                NormalizedToolCall(tool_call_id="missing", tool_name="one")
            ]))],
    )
    with pytest.raises(ContextSnapshotUnavailableError, match="Incomplete"):
        projector(runtime).prepare(state)


def test_mandatory_context_budget_failure(runtime):
    _, _, sessions, _, _, _ = runtime
    state = create_running_session(sessions)
    with pytest.raises(Exception, match="Mandatory"):
        projector(runtime, max_tokens=1).prepare(state)


def test_budget_drops_memory_and_old_conversation_before_active_task(runtime):
    _, _, sessions, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="large.preference", memory_type=MemoryType.PREFERENCE,
        display_text="comparison " * 120, content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    memories.confirm(
        candidate.memory_id, owner_id="local-user", expected_version=0,
        confirmed_by_user=True,
    )
    state = sessions.create(
        SessionState(session_id="session-1", status=SessionStatus.RUNNING),
        SessionEvent(session_id="session-1", event_type=SessionEventType.SESSION_CREATED),
        messages=[
            SessionMessageDraft(message=AgentMessage(
                message_id="old-user", role="user", content="old optional context " * 50,
            )),
            SessionMessageDraft(message=AgentMessage(
                message_id="active-user", role="user", content="current task",
            )),
        ],
    )
    service = SessionContextProjector(
        sessions=sessions, snapshots=snapshots,
        system_policy="trusted system policy",
        memories=memories,
        memory_retriever=MemoryRetriever(memories),
        max_input_tokens=260,
    )
    prepared = service.prepare(state)
    assert "active-user" in prepared.snapshot.included_message_ids
    assert "old-user" in prepared.snapshot.excluded_message_ids
    assert prepared.snapshot.memory_versions == []
    assert prepared.snapshot.memory_ids == []
    assert all(
        block.kind != ContextBlockKind.MEMORY
        for block in prepared.snapshot.block_manifests
    )
    assert prepared.messages[-1].message_id == "active-user"


def test_coordinator_passes_relevant_preference_projection_to_model(runtime):
    _, _, sessions, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="resume.summary.max_sentences", memory_type=MemoryType.PREFERENCE,
        display_text="Prefers resume summaries with no more than two sentences.",
        content={"maximum_sentences": 2},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    confirmed = memories.confirm(
        candidate.memory_id, owner_id="local-user", expected_version=0,
        confirmed_by_user=True,
    )
    model = CapturingModel([response(content="Use no more than two sentences.")])
    coordinator = build_coordinator(runtime, model)
    state = coordinator.create_session(session_id="preference-session", user_id="local-user")
    outcome = coordinator.submit_user_message(
        state.session_id,
        AgentMessage(
            message_id="preference-question", role="user",
            content="How many sentences should I have in my resume summary?",
        ),
        expected_version=state.version,
    )

    sent_messages = model.calls[0][0]
    sent_text = "\n".join(message.content for message in sent_messages)
    assert [message.role for message in sent_messages].count("system") == 1
    assert "resume.summary.max_sentences" in sent_text
    assert "no more than two sentences" in sent_text
    assert outcome.final_text == "Use no more than two sentences."
    snapshot = snapshots.require(outcome.state.last_context_snapshot_id)
    manifest_memory_ids = {
        block.block_id.removeprefix("memory:")
        for block in snapshot.block_manifests
        if block.kind == ContextBlockKind.MEMORY
    }
    assert snapshot.memory_ids == [confirmed.memory_id]
    assert set(snapshot.memory_ids) == manifest_memory_ids
    assert snapshot.status == ContextSnapshotStatus.USED


def test_current_turn_override_rule_reaches_model(runtime):
    _, _, _, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="resume.summary.max_sentences", memory_type=MemoryType.PREFERENCE,
        display_text="Prefers resume summaries with no more than two sentences.", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    memories.confirm(candidate.memory_id, owner_id="local-user", expected_version=0,
                     confirmed_by_user=True)
    model = CapturingModel([response(content="For this turn, use three sentences.")])
    coordinator = build_coordinator(runtime, model)
    state = coordinator.create_session(session_id="override-session", user_id="local-user")
    outcome = coordinator.submit_user_message(
        state.session_id,
        AgentMessage(
            message_id="override-question", role="user",
            content="For this turn, override my usual preference and use three summary sentences.",
        ),
        expected_version=state.version,
    )
    sent = "\n".join(message.content for message in model.calls[0][0])
    assert "current explicit user instruction may override" in sent
    assert "use three summary sentences" in sent
    assert snapshots.require(outcome.state.last_context_snapshot_id).memory_ids


def test_irrelevant_preference_is_not_sent_to_model(runtime):
    _, _, _, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="resume.summary.max_sentences", memory_type=MemoryType.PREFERENCE,
        display_text="Prefers resume summaries with no more than two sentences.", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    memories.confirm(candidate.memory_id, owner_id="local-user", expected_version=0,
                     confirmed_by_user=True)
    model = CapturingModel([response(content="An INNER JOIN returns matching rows.")])
    coordinator = build_coordinator(runtime, model)
    state = coordinator.create_session(session_id="unrelated-session", user_id="local-user")
    outcome = coordinator.submit_user_message(
        state.session_id,
        AgentMessage(message_id="sql-question", role="user", content="Explain a SQL inner join."),
        expected_version=state.version,
    )
    sent = "\n".join(message.content for message in model.calls[0][0])
    assert "resume.summary.max_sentences" not in sent
    assert snapshots.require(outcome.state.last_context_snapshot_id).memory_ids == []


def build_coordinator(runtime, model):
    _, clock, sessions, snapshots, _, _ = runtime
    registry = ToolRegistry()
    calls = ToolCallRepository(sessions.session_factory)
    executor = ToolExecutor(registry, policy=ToolPolicy(), repository=calls)
    loop = ToolCallingLoop(model=model, registry=registry, executor=executor)
    return SessionCoordinator(
        sessions=sessions, tool_calls=calls, executor=executor, loop=loop,
        claims=SessionClaimRepository(sessions.session_factory, clock=clock),
        clock=clock, context_projector=projector(runtime), context_snapshots=snapshots,
        lease_seconds=60, model_timeout_seconds=30,
    )


def test_snapshot_is_prepared_before_model_and_used_after_message(runtime):
    _, _, sessions, snapshots, _, _ = runtime
    observed = {}

    def hook():
        state = sessions.require("session-live")
        observed["snapshot"] = snapshots.require(state.current_context_snapshot_id)
        observed["assistant_before"] = sessions.message("assistant-1")

    coordinator = build_coordinator(runtime, ScriptedModel([response()], hook=hook))
    state = coordinator.create_session(session_id="session-live")
    outcome = coordinator.submit_user_message(
        state.session_id,
        AgentMessage(message_id="user-live", role="user", content="hello"),
        expected_version=state.version,
    )
    assert observed["snapshot"].status == ContextSnapshotStatus.PREPARED
    assert observed["assistant_before"] is None
    final = sessions.require(state.session_id)
    assert final.current_context_snapshot_id is None
    assert snapshots.require(final.last_context_snapshot_id).status == ContextSnapshotStatus.USED
    assert outcome.final_text == "done"


def test_cancellation_and_timeout_abandon_prepared_snapshot(runtime):
    _, clock, sessions, snapshots, _, _ = runtime
    holder = {}

    def cancel_hook():
        current = sessions.require("cancel-session")
        holder["snapshot"] = current.current_context_snapshot_id
        holder["coordinator"].request_cancel(current.session_id, "stop")

    cancel_model = ScriptedModel([response()], hook=cancel_hook)
    cancel_coordinator = build_coordinator(runtime, cancel_model)
    holder["coordinator"] = cancel_coordinator
    state = cancel_coordinator.create_session(session_id="cancel-session")
    outcome = cancel_coordinator.submit_user_message(
        state.session_id, AgentMessage(message_id="cancel-user", role="user", content="hello"),
        expected_version=state.version,
    )
    assert outcome.state.status == SessionStatus.CANCELLED
    assert snapshots.require(holder["snapshot"]).status == ContextSnapshotStatus.ABANDONED

    def timeout_hook():
        current = sessions.require("timeout-session")
        holder["timeout_snapshot"] = current.current_context_snapshot_id
        clock.advance(seconds=3)

    timeout_coordinator = build_coordinator(runtime, ScriptedModel([response("timeout-a")], hook=timeout_hook))
    state = timeout_coordinator.create_session(session_id="timeout-session")
    outcome = timeout_coordinator.submit_user_message(
        state.session_id, AgentMessage(message_id="timeout-user", role="user", content="hello"),
        expected_version=state.version, turn_timeout_seconds=2,
    )
    assert outcome.state.status == SessionStatus.TIMED_OUT
    assert snapshots.require(holder["timeout_snapshot"]).status == ContextSnapshotStatus.ABANDONED


def test_recovery_reuses_snapshot_and_new_memory_is_not_added(runtime):
    _, _, sessions, snapshots, memories, _ = runtime
    state = create_running_session(sessions)
    service = projector(runtime)
    prepared = service.prepare(state)
    state = sessions.save_transition(
        SessionState.model_validate({
            **state.model_dump(mode="python"),
            "current_context_snapshot_id": prepared.snapshot.snapshot_id,
        }),
        SessionEvent(session_id=state.session_id, event_type=SessionEventType.CONTEXT_SNAPSHOT_PREPARED),
        expected_version=state.version,
    )
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="new.memory", memory_type=MemoryType.SEMANTIC,
        display_text="New memory after crash", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    memories.confirm(candidate.memory_id, owner_id="local-user", expected_version=0, confirmed_by_user=True)
    rebuilt = service.rebuild(state, prepared.snapshot.snapshot_id)
    assert rebuilt.snapshot.context_hash == prepared.snapshot.context_hash
    assert rebuilt.snapshot.memory_versions == []


def test_coordinator_recovery_reuses_exact_prepared_model_context(runtime):
    _, _, sessions, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="job.analysis.format", memory_type=MemoryType.PREFERENCE,
        display_text="Prefers comparison tables for job run requirements", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    confirmed = memories.confirm(
        candidate.memory_id, owner_id="local-user", expected_version=0,
        confirmed_by_user=True,
    )
    state = create_running_session(sessions)
    service = projector(runtime)
    prepared = service.prepare(state)
    state = sessions.save_transition(
        SessionState.model_validate({
            **state.model_dump(mode="python"),
            "current_context_snapshot_id": prepared.snapshot.snapshot_id,
        }),
        SessionEvent(
            session_id=state.session_id,
            event_type=SessionEventType.CONTEXT_SNAPSHOT_PREPARED,
        ),
        expected_version=state.version,
    )
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="created.after.snapshot", memory_type=MemoryType.SEMANTIC,
        display_text="Memory that recovery must not route into the old snapshot",
        content={}, provenance=[MemoryProvenance(source_type="user_message")],
    )
    memories.confirm(
        candidate.memory_id, owner_id="local-user", expected_version=0,
        confirmed_by_user=True,
    )
    model = ScriptedModel([response()])
    coordinator = build_coordinator(runtime, model)
    outcome = coordinator.recover_session(state.session_id, "recovery-worker")
    assert outcome.final_text == "done"
    recovered_context = model.calls[0][0]
    assert len(recovered_context) == len(prepared.messages)
    assert recovered_context[0].message_id == prepared.messages[0].message_id
    assert recovered_context[0].content.startswith(prepared.messages[0].content)
    rebuilt_preference = next(
        message.content for message in recovered_context
        if "job.analysis.format" in message.content
    )
    assert "job.analysis.format" in rebuilt_preference
    assert "Prefers comparison tables" in rebuilt_preference
    assert "job.analysis.format" in prepared.messages[0].content
    assert prepared.snapshot.memory_ids == [confirmed.memory_id]
    assert snapshots.require(prepared.snapshot.snapshot_id).status == ContextSnapshotStatus.USED


def test_stable_response_recovery_reconciles_prepared_snapshot(runtime):
    _, _, sessions, snapshots, _, _ = runtime
    state = create_running_session(sessions)
    prepared = projector(runtime).prepare(state)
    active = SessionState.model_validate({
        **state.model_dump(mode="python"),
        "status": SessionStatus.ACTIVE,
        "last_context_snapshot_id": prepared.snapshot.snapshot_id,
    })
    state = sessions.save_transition(
        active,
        SessionEvent(
            session_id=state.session_id,
            event_type=SessionEventType.MODEL_DECISION_PERSISTED,
        ),
        expected_version=state.version,
        messages=[SessionMessageDraft(message=AgentMessage(
            message_id="durable-assistant", role="assistant", content="saved response",
        ))],
    )
    assert snapshots.require(prepared.snapshot.snapshot_id).status == ContextSnapshotStatus.PREPARED
    outcome = build_coordinator(runtime, ScriptedModel([])).recover_session(
        state.session_id, "recovery-worker"
    )
    assert outcome.final_text == "saved response"
    assert snapshots.require(prepared.snapshot.snapshot_id).status == ContextSnapshotStatus.USED


def test_changed_referenced_memory_breaks_recovery_and_usage_waits(runtime):
    database, _, sessions, snapshots, memories, _ = runtime
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="preference.format", memory_type=MemoryType.PREFERENCE,
        display_text="Compare job run requirements using tables", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    confirmed = memories.confirm(candidate.memory_id, owner_id="local-user", expected_version=0, confirmed_by_user=True)
    state = create_running_session(sessions)
    service = projector(runtime)
    prepared = service.prepare(state)
    assert memories.require(confirmed.memory_id, owner_id="local-user").last_used_at is None
    memories.soft_delete(confirmed.memory_id, owner_id="local-user", expected_version=confirmed.version)
    with pytest.raises(ContextSnapshotUnavailableError, match="Memory changed"):
        service.rebuild(state, prepared.snapshot.snapshot_id)
    with database.session_factory() as db:
        assert db.scalars(select(MemoryUsageEventRow)).all() == []


def test_used_snapshot_records_memory_and_skill_usage(runtime, tmp_path):
    database, _, sessions, snapshots, memories, skills = runtime
    activate_project_skill(skills, tmp_path)
    candidate = memories.create_candidate(
        owner_id="local-user", scope=MemoryScope.USER, scope_id="local-user",
        memory_key="job.analysis", memory_type=MemoryType.SEMANTIC,
        display_text="Compare job run requirements", content={},
        provenance=[MemoryProvenance(source_type="user_message")],
    )
    confirmed = memories.confirm(candidate.memory_id, owner_id="local-user", expected_version=0, confirmed_by_user=True)
    state = create_running_session(sessions, allowed_skills=frozenset({"job-run-analysis"}))
    prepared = projector(runtime).prepare(state)
    snapshots.mark_used(prepared.snapshot.snapshot_id)
    assert memories.require(confirmed.memory_id, owner_id="local-user").last_used_at is not None
    with database.session_factory() as db:
        assert len(db.scalars(select(MemoryUsageEventRow)).all()) == 1
        assert len(db.scalars(select(SkillUsageEventRow)).all()) == 1


def test_context_migration_from_skill_runtime(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0010_skill_runtime")
    command.upgrade(config, "head")
    database = create_database(url)
    try:
        tables = set(inspect(database.engine).get_table_names())
        assert {"context_snapshots", "context_snapshot_events", "memory_usage_events", "skill_usage_events"} <= tables
        session_columns = {item["name"] for item in inspect(database.engine).get_columns("agent_sessions")}
        assert {"current_context_snapshot_id", "last_context_snapshot_id", "allowed_skills_json"} <= session_columns
    finally:
        database.close()

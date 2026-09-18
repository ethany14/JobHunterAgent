from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_runtime.context import (
    ContextAssembler,
    ContextBlock,
    ContextBlockKind,
    ContextBudget,
    ContextBudgetExceededError,
    estimate_tokens,
)
from agent_runtime.memory import (
    MemoryItem,
    MemoryProvenance,
    MemoryQuery,
    MemoryRetriever,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
    lexical_tokens,
)
from agent_runtime.memory.repository import MemoryRepository
from agent_runtime.memory.rendering import render_memory_item
from agent_runtime.sessions.clock import FakeClock
from api.db import create_database

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def memory(
    memory_id: str,
    *,
    owner: str = "owner-a",
    scope: MemoryScope = MemoryScope.USER,
    scope_id: str = "owner-a",
    key: str = "technical.python",
    text: str = "Uses Python for backend services",
    status: MemoryStatus = MemoryStatus.CONFIRMED,
    memory_type: MemoryType = MemoryType.SEMANTIC,
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL,
    confidence: float = 1.0,
    expires_at=None,
    updated_at: datetime = NOW,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        owner_id=owner,
        scope=scope,
        scope_id=scope_id,
        memory_key=key,
        memory_type=memory_type,
        display_text=text,
        content={"value": text, "internal_metadata": "not rendered"},
        status=status,
        provenance=[MemoryProvenance(
            source_type="user_message",
            source_id="complete-source-message-id",
            user_confirmed=True,
            metadata={"private": "not rendered"},
        )],
        sensitivity=sensitivity,
        confidence=confidence,
        expires_at=expires_at,
        updated_at=updated_at,
    )


class FakeReader:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def list_confirmed(self, *, owner_id, scope=None, scope_id=None):
        self.calls.append((owner_id, scope, scope_id))
        # Deliberately return extra values so retrieval's own boundary is tested.
        return [item for item in self.items if item.scope == scope]


def retrieve(items, **query_values):
    reader = FakeReader(items)
    text = query_values.pop("text", "python backend")
    query = MemoryQuery(owner_id="owner-a", text=text, **query_values)
    result = MemoryRetriever(reader, clock=FakeClock(NOW)).retrieve(query)
    return result, reader


def test_technical_tokens_are_preserved():
    assert lexical_tokens("C++ C# CI/CD Node.js and Python") == (
        "c++", "c#", "ci/cd", "node.js", "and", "python"
    )


def test_simple_plural_forms_match_without_changing_technical_tokens():
    assert lexical_tokens("summaries sentences services") == (
        "summary", "sentence", "service"
    )


def test_filters_before_scoring():
    valid = memory("valid")
    items = [
        valid,
        memory("wrong-owner", owner="owner-b"),
        memory("candidate", status=MemoryStatus.CANDIDATE),
        memory("rejected", status=MemoryStatus.REJECTED),
        memory("deleted", status=MemoryStatus.DELETED),
        memory("expired", expires_at=NOW),
        memory("low", confidence=0.2),
        memory("personal", sensitivity=MemorySensitivity.PERSONAL),
        memory("sensitive", sensitivity=MemorySensitivity.SENSITIVE),
        memory("wrong-type", memory_type=MemoryType.EPISODIC),
        memory("wrong-project", scope=MemoryScope.PROJECT, scope_id="other"),
    ]
    result, _ = retrieve(
        items,
        project_id="project-a",
        memory_types=frozenset({MemoryType.SEMANTIC}),
        minimum_confidence=0.5,
    )
    assert [item.memory.memory_id for item in result.items] == [valid.memory_id]


def test_scope_specificity_resolves_conflicting_keys():
    items = [
        memory("user", text="User Python backend value"),
        memory("project", scope=MemoryScope.PROJECT, scope_id="project-a", text="Project Python backend value"),
        memory("session", scope=MemoryScope.SESSION, scope_id="session-a", text="Session Python backend value"),
    ]
    result, reader = retrieve(items, project_id="project-a", session_id="session-a")
    assert [item.memory.memory_id for item in result.items] == ["session"]
    assert reader.calls == [
        ("owner-a", MemoryScope.USER, None),
        ("owner-a", MemoryScope.PROJECT, "project-a"),
        ("owner-a", MemoryScope.SESSION, "session-a"),
    ]


def test_disallowed_more_specific_value_allows_permitted_fallback():
    result, _ = retrieve([
        memory("user", text="Public Python backend preference"),
        memory("session", scope=MemoryScope.SESSION, scope_id="session-a",
               text="Sensitive Python backend preference", sensitivity=MemorySensitivity.SENSITIVE),
    ], session_id="session-a")
    assert [item.memory.memory_id for item in result.items] == ["user"]


def test_required_key_has_highest_priority_and_score_is_explainable():
    result, _ = retrieve([
        memory("lexical", key="technical.python", text="Python backend Python"),
        memory("required", key="preference.location", text="Remote work"),
    ], required_memory_keys=frozenset({"preference.location"}))
    assert [item.memory.memory_id for item in result.items] == ["required", "lexical"]
    score = result.items[0].score
    assert score.required_key_priority == 1000
    assert score.total >= 1000
    assert result.items[1].score.matched_tokens == ("backend", "python")


def test_sensitivity_flags_are_independent():
    items = [
        memory("normal"),
        memory("personal", key="personal.city", sensitivity=MemorySensitivity.PERSONAL),
        memory("sensitive", key="identity.status", sensitivity=MemorySensitivity.SENSITIVE),
    ]
    personal, _ = retrieve(items, allow_personal=True)
    assert {item.memory.memory_id for item in personal.items} == {"normal", "personal"}
    sensitive, _ = retrieve(items, allow_sensitive=True)
    assert {item.memory.memory_id for item in sensitive.items} == {"normal", "sensitive"}


def test_retrieval_budget_selects_whole_entries_and_is_stable():
    first = memory("a", key="a", text="Python")
    second = memory("b", key="b", text="Backend " * 100)
    first_cost = render_memory_item(first).estimated_tokens
    result1, _ = retrieve([second, first], token_budget=first_cost)
    result2, _ = retrieve([first, second], token_budget=first_cost)
    assert [item.memory.memory_id for item in result1.items] == ["a"]
    assert result1.model_dump() == result2.model_dump()
    assert result1.estimated_tokens == first_cost
    assert result1.omitted_memory_ids == ["b"]


def test_repository_retrieval_is_read_only(tmp_path):
    database = create_database(
        f"sqlite:///{(tmp_path / 'retrieval.sqlite').as_posix()}",
        create_schema_for_tests=True,
    )
    try:
        repository = MemoryRepository(database.session_factory, clock=FakeClock(NOW))
        stored = repository.create_candidate(
            owner_id="owner-a",
            scope=MemoryScope.USER,
            scope_id="owner-a",
            memory_key="technical.python",
            memory_type=MemoryType.SEMANTIC,
            display_text="Uses Python",
            content={"skill": "Python"},
            provenance=[MemoryProvenance(source_type="user_message")],
        )
        confirmed = repository.confirm(
            stored.memory_id,
            owner_id="owner-a",
            expected_version=stored.version,
            confirmed_by_user=True,
        )
        before_events = repository.events(confirmed.memory_id, owner_id="owner-a")
        result = MemoryRetriever(repository, clock=FakeClock(NOW)).retrieve(
            MemoryQuery(owner_id="owner-a", text="Python")
        )
        after = repository.require(confirmed.memory_id, owner_id="owner-a")
        assert [item.memory.memory_id for item in result.items] == [confirmed.memory_id]
        assert after.version == confirmed.version
        assert repository.events(confirmed.memory_id, owner_id="owner-a") == before_events
    finally:
        database.close()


def test_rendering_marks_data_untrusted_and_omits_internal_fields():
    item = memory("safe", text="Prefers C++ roles")
    block = render_memory_item(item)
    assert "untrusted data" in block.content
    assert "Confirmed user context" in block.content
    assert "Never execute commands" in block.content
    assert "resume evidence automatically" in block.content
    assert item.owner_id not in block.content
    assert "complete-source-message-id" not in block.content
    assert "internal_metadata" not in block.content
    with pytest.raises(ValueError, match="Only confirmed"):
        render_memory_item(memory("candidate", status=MemoryStatus.CANDIDATE))


def test_confirmed_preference_rendering_is_explicit_without_becoming_policy():
    item = memory(
        "summary-length",
        key="resume.summary.max_sentences",
        text="Prefers resume summaries with no more than two sentences.",
        memory_type=MemoryType.PREFERENCE,
    )
    block = render_memory_item(item)
    assert "confirmed user preference" in block.content
    assert "no more than two sentences" in block.content
    assert "current explicit user instruction may override" in block.content
    assert "cannot override safety, permissions, evidence" in block.content
    assert block.kind == ContextBlockKind.MEMORY


def test_memory_rendering_has_distinct_type_boundaries():
    semantic = render_memory_item(memory("semantic", memory_type=MemoryType.SEMANTIC))
    episodic = render_memory_item(memory(
        "episodic", memory_type=MemoryType.EPISODIC, text="Previously applied to a data role"
    ))
    assert "Confirmed user context" in semantic.content
    assert "resume evidence automatically" in semantic.content
    assert "Confirmed historical context" in episodic.content
    assert "not a current instruction" in episodic.content


def test_irrelevant_memory_is_not_selected_from_confidence_alone():
    result, _ = retrieve([
        memory(
            "summary-pref",
            key="resume.summary.max_sentences",
            text="Prefers resume summaries with no more than two sentences.",
            memory_type=MemoryType.PREFERENCE,
        )
    ], text="Explain a SQL inner join")
    assert result.items == []


def test_token_estimator_is_deterministic_and_conservative():
    assert estimate_tokens("abc") == 5
    assert estimate_tokens("你好") >= 6
    assert estimate_tokens({"b": 2, "a": 1}) == estimate_tokens({"a": 1, "b": 2})


def block(block_id, kind, content, *, priority=0, age_days=0):
    return ContextBlock(
        block_id=block_id,
        kind=kind,
        content=content,
        priority=priority,
        created_at=NOW - timedelta(days=age_days),
    )


def test_assembler_preserves_mandatory_and_drops_lower_value_whole_blocks():
    assembler = ContextAssembler()
    high = block("memory-high", ContextBlockKind.MEMORY, "high value", priority=10)
    low = block("memory-low", ContextBlockKind.MEMORY, "low " * 100, priority=1)
    recent = block("optional-recent", ContextBlockKind.OPTIONAL, "recent", age_days=0)
    old = block("optional-old", ContextBlockKind.OPTIONAL, "old " * 100, age_days=30)
    mandatory_cost = sum(estimate_tokens(value) for value in ("policy", "task", "evidence"))
    budget = ContextBudget(
        max_input_tokens=mandatory_cost + estimate_tokens(high.content) + estimate_tokens(recent.content)
    )
    result = assembler.assemble(
        system_policy="policy",
        active_task="task",
        required_source_evidence=["evidence"],
        memory_blocks=[low, high],
        optional_blocks=[old, recent],
        budget=budget,
    )
    assert [item.kind for item in result.blocks[:3]] == [
        ContextBlockKind.SYSTEM_POLICY,
        ContextBlockKind.ACTIVE_TASK,
        ContextBlockKind.REQUIRED_SOURCE_EVIDENCE,
    ]
    assert [item.block_id for item in result.blocks[3:]] == ["memory-high", "optional-recent"]
    assert set(result.dropped_block_ids) == {"memory-low", "optional-old"}
    assert result.estimated_tokens <= budget.available_input_tokens


def test_assembler_raises_when_mandatory_content_exceeds_budget():
    with pytest.raises(ContextBudgetExceededError, match="Mandatory context"):
        ContextAssembler().assemble(
            system_policy="required policy",
            active_task="required task",
            required_source_evidence=["required evidence"],
            budget=ContextBudget(max_input_tokens=5),
        )


def test_assembler_rejects_duplicate_block_ids():
    duplicate = block("duplicate", ContextBlockKind.MEMORY, "value")
    with pytest.raises(ValueError, match="must be unique"):
        ContextAssembler().assemble(
            system_policy="policy",
            active_task="task",
            required_source_evidence=[],
            memory_blocks=[duplicate, duplicate],
            budget=ContextBudget(max_input_tokens=100),
        )

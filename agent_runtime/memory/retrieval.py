"""Read-only deterministic memory selection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import Field

from agent_runtime.context.budget import estimate_tokens
from agent_runtime.context.types import ContextBlock
from agent_runtime.memory.query import MemoryQuery
from agent_runtime.memory.rendering import render_memory_item
from agent_runtime.memory.scoring import MemoryScoreBreakdown, score_memory
from agent_runtime.memory.types import MemoryItem, MemoryScope, MemorySensitivity, MemoryStatus
from agent_runtime.sessions.clock import Clock, SystemClock
from agent_runtime.types import RuntimeModel


class ConfirmedMemoryReader(Protocol):
    def list_confirmed(
        self, *, owner_id: str, scope: MemoryScope | None = None, scope_id: str | None = None
    ) -> list[MemoryItem]: ...


class RetrievedMemory(RuntimeModel):
    memory: MemoryItem
    score: MemoryScoreBreakdown
    rendered_block: ContextBlock


class MemoryRetrievalResult(RuntimeModel):
    items: list[RetrievedMemory]
    omitted_memory_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)


_SCOPE_RANK = {MemoryScope.USER: 1, MemoryScope.PROJECT: 2, MemoryScope.SESSION: 3}


class MemoryRetriever:
    def __init__(self, reader: ConfirmedMemoryReader, *, clock: Clock | None = None) -> None:
        self._reader = reader
        self._clock = clock or SystemClock()

    def retrieve(self, query: MemoryQuery) -> MemoryRetrievalResult:
        candidates = list(self._reader.list_confirmed(
            owner_id=query.owner_id, scope=MemoryScope.USER
        ))
        if query.project_id:
            candidates.extend(self._reader.list_confirmed(
                owner_id=query.owner_id, scope=MemoryScope.PROJECT, scope_id=query.project_id
            ))
        if query.session_id:
            candidates.extend(self._reader.list_confirmed(
                owner_id=query.owner_id, scope=MemoryScope.SESSION, scope_id=query.session_id
            ))
        filtered = [item for item in candidates if self._allowed(item, query)]
        resolved = self._resolve_keys(filtered)
        scored: list[tuple[MemoryItem, MemoryScoreBreakdown]] = [
            (item, score_memory(item, query.text, query.required_memory_keys))
            for item in resolved
        ]
        # Confidence alone does not make a Memory relevant. Required keys bypass
        # lexical matching; every other item needs an actual query match.
        scored = [
            (item, score)
            for item, score in scored
            if score.required_key_priority > 0 or bool(score.matched_tokens)
        ]
        scored.sort(key=lambda pair: (
            -pair[1].required_key_priority,
            -pair[1].total,
            -_SCOPE_RANK[pair[0].scope],
            -pair[0].updated_at.timestamp(),
            pair[0].memory_key,
            pair[0].memory_id,
        ))
        selected: list[RetrievedMemory] = []
        omitted: list[str] = []
        used = 0
        for item, score in scored:
            if len(selected) >= query.limit:
                omitted.append(item.memory_id)
                continue
            block = render_memory_item(item, priority=score.total)
            cost = block.estimated_tokens or estimate_tokens(block.content)
            if used + cost > query.token_budget:
                omitted.append(item.memory_id)
                continue
            selected.append(RetrievedMemory(memory=item, score=score, rendered_block=block))
            used += cost
        return MemoryRetrievalResult(items=selected, omitted_memory_ids=omitted,
                                     estimated_tokens=used)

    def _allowed(self, item: MemoryItem, query: MemoryQuery) -> bool:
        now = self._clock.now()
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        expires = item.expires_at
        if expires is not None:
            expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
        allowed_scope = (
            item.scope == MemoryScope.USER
            or (item.scope == MemoryScope.PROJECT and item.scope_id == query.project_id)
            or (item.scope == MemoryScope.SESSION and item.scope_id == query.session_id)
        )
        if item.status != MemoryStatus.CONFIRMED or item.owner_id != query.owner_id or not allowed_scope:
            return False
        if expires is not None and expires <= now:
            return False
        if item.confidence < query.minimum_confidence:
            return False
        if query.memory_types and item.memory_type not in query.memory_types:
            return False
        if item.sensitivity == MemorySensitivity.PERSONAL and not query.allow_personal:
            return False
        if item.sensitivity == MemorySensitivity.SENSITIVE and not query.allow_sensitive:
            return False
        return True

    @staticmethod
    def _resolve_keys(items: list[MemoryItem]) -> list[MemoryItem]:
        by_key: dict[str, MemoryItem] = {}
        for item in sorted(items, key=lambda value: (
            -_SCOPE_RANK[value.scope], -value.updated_at.timestamp(), value.memory_id
        )):
            by_key.setdefault(item.memory_key, item)
        return list(by_key.values())

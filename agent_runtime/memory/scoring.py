"""Explainable lexical scoring which does not call a model."""

from __future__ import annotations

import json
import re

from pydantic import Field

from agent_runtime.memory.types import MemoryItem
from agent_runtime.types import RuntimeModel

_TOKEN_PATTERN = re.compile(r"(?u)[a-z0-9]+(?:\+\+|#)?(?:[./][a-z0-9+#]+)*")


def _lexical_form(token: str) -> str:
    if any(character in token for character in "+#./"):
        return token
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def lexical_tokens(text: str) -> tuple[str, ...]:
    """Normalize search text while retaining meaningful technical punctuation."""
    return tuple(
        _lexical_form(token)
        for token in _TOKEN_PATTERN.findall(text.casefold().replace("_", " "))
    )


class MemoryScoreBreakdown(RuntimeModel):
    required_key_priority: float = Field(ge=0)
    query_coverage: float = Field(ge=0, le=1)
    memory_specificity: float = Field(ge=0, le=1)
    exact_phrase_bonus: float = Field(ge=0)
    confidence_component: float = Field(ge=0, le=1)
    matched_tokens: tuple[str, ...] = ()
    total: float = Field(ge=0)


def score_memory(item: MemoryItem, query_text: str, required_keys: frozenset[str]) -> MemoryScoreBreakdown:
    query_tokens = set(lexical_tokens(query_text))
    searchable = " ".join(
        (item.memory_key, item.display_text, json.dumps(item.content, ensure_ascii=False, sort_keys=True))
    )
    memory_tokens = set(lexical_tokens(searchable))
    matched = tuple(sorted(query_tokens & memory_tokens))
    coverage = len(matched) / len(query_tokens) if query_tokens else 0.0
    specificity = len(matched) / len(memory_tokens) if memory_tokens else 0.0
    normalized_query = " ".join(lexical_tokens(query_text))
    normalized_display = " ".join(lexical_tokens(item.display_text))
    phrase = 1.0 if normalized_query and normalized_query in normalized_display else 0.0
    required = 1_000.0 if item.memory_key in required_keys else 0.0
    total = required + 4.0 * coverage + 2.0 * specificity + phrase + item.confidence
    return MemoryScoreBreakdown(
        required_key_priority=required,
        query_coverage=coverage,
        memory_specificity=specificity,
        exact_phrase_bonus=phrase,
        confidence_component=item.confidence,
        matched_tokens=matched,
        total=total,
    )

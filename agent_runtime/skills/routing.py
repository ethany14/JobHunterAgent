"""Deterministic lexical Skill routing over active discovery metadata."""

from __future__ import annotations

from pydantic import Field

from agent_runtime.memory.scoring import lexical_tokens
from agent_runtime.skills.discovery import SkillDiscovery
from agent_runtime.skills.errors import SkillNotFoundError
from agent_runtime.skills.loader import SkillLoader
from agent_runtime.skills.types import ActiveSkill, SkillStatus
from agent_runtime.types import RuntimeModel


class SessionSkillProfile(RuntimeModel):
    profile_name: str = Field(min_length=1)
    allowed_skill_names: frozenset[str] = Field(default_factory=frozenset)


class SkillRoute(RuntimeModel):
    skill: ActiveSkill
    explicit: bool
    matched_tokens: tuple[str, ...]
    score: float


class SkillRouter:
    def __init__(self, discovery: SkillDiscovery, loader: SkillLoader) -> None:
        self._discovery = discovery
        self._loader = loader

    def route(
        self,
        *,
        text: str,
        profile: SessionSkillProfile,
        runtime_allowed_tools: frozenset[str],
        explicit_skill_names: frozenset[str] = frozenset(),
        limit: int = 3,
    ) -> list[SkillRoute]:
        active = {
            item.name: item
            for item in self._discovery.discover()
            if item.status == SkillStatus.ACTIVE and item.name in profile.allowed_skill_names
        }
        missing = explicit_skill_names - active.keys()
        if missing:
            raise SkillNotFoundError(
                "Explicit Skill is unavailable or not permitted: " + ", ".join(sorted(missing))
            )
        query = set(lexical_tokens(text))
        ranked: list[tuple[float, bool, tuple[str, ...], str, str]] = []
        for name, item in active.items():
            tokens = set(lexical_tokens(f"{item.name} {item.description}"))
            matched = tuple(sorted(query & tokens))
            explicit = name in explicit_skill_names
            coverage = len(matched) / len(query) if query else 0.0
            specificity = len(matched) / len(tokens) if tokens else 0.0
            score = (1000.0 if explicit else 0.0) + 4 * coverage + 2 * specificity
            if explicit or matched:
                ranked.append((score, explicit, matched, name, item.version_id))
        ranked.sort(key=lambda value: (-int(value[1]), -value[0], value[3], value[4]))
        return [SkillRoute(
            skill=self._loader.activate(version_id, runtime_allowed_tools=runtime_allowed_tools),
            explicit=explicit,
            matched_tokens=matched,
            score=score,
        ) for score, explicit, matched, _, version_id in ranked[:limit]]

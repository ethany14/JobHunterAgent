"""Lightweight Skill discovery without instruction disclosure."""

from __future__ import annotations

from agent_runtime.skills.repository import SkillRepository
from agent_runtime.skills.types import SkillDiscoveryRecord


class SkillDiscovery:
    def __init__(self, repository: SkillRepository) -> None:
        self._repository = repository

    def discover(self) -> list[SkillDiscoveryRecord]:
        """Return routing metadata only; instructions and resources stay unloaded."""
        return self._repository.discover()

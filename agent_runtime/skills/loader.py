"""Activation and safe, on-demand reference loading."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.skills.errors import (
    InvalidSkillTransitionError,
    SkillContentChangedError,
    SkillResourceError,
    SkillScriptExecutionDisabledError,
)
from agent_runtime.skills.hashing import hash_bytes, hash_skill_package
from agent_runtime.skills.repository import SkillRepository
from agent_runtime.skills.security import ensure_regular_file, media_type_for, safe_relative_path
from agent_runtime.skills.types import ActiveSkill, SkillResourceKind, SkillStatus


class SkillLoader:
    def __init__(self, repository: SkillRepository) -> None:
        self._repository = repository

    def activate(self, version_id: str, *, runtime_allowed_tools: frozenset[str]) -> ActiveSkill:
        version = self._repository.require(version_id)
        if version.status != SkillStatus.ACTIVE:
            raise InvalidSkillTransitionError("Only active Skill versions may be loaded.")
        self._verify_package(version.package_path, version.content_hash)
        effective = runtime_allowed_tools
        if version.allowed_tools is not None:
            effective = runtime_allowed_tools & version.allowed_tools
        return ActiveSkill(
            skill_id=version.skill_id,
            version_id=version.version_id,
            name=version.name,
            description=version.description,
            version=version.version_label,
            instructions=version.instruction_snapshot,
            metadata=version.metadata,
            effective_allowed_tools=effective,
            resource_manifest=version.resource_manifest,
        )

    def load_reference(self, version_id: str, relative_path: str) -> str:
        version = self._repository.require(version_id)
        if version.status != SkillStatus.ACTIVE:
            raise InvalidSkillTransitionError("Only active Skill versions may load references.")
        package_root = Path(version.package_path)
        self._verify_package(version.package_path, version.content_hash)
        safe_path = safe_relative_path(relative_path).as_posix()
        entry = next(
            (item for item in version.resource_manifest.resources if item.path == safe_path), None
        )
        if entry is None or entry.kind != SkillResourceKind.REFERENCE:
            raise SkillResourceError("Reference is not registered in the Skill manifest.")
        path = package_root / safe_path
        ensure_regular_file(path, package_root)
        data = path.read_bytes()
        if len(data) != entry.size_bytes or hash_bytes(data) != entry.sha256:
            raise SkillContentChangedError("Registered Skill reference changed on disk.")
        if media_type_for(path) != entry.media_type:
            raise SkillResourceError("Reference media type no longer matches its manifest.")
        if entry.media_type not in {
            "text/plain", "text/markdown", "application/json", "application/yaml",
            "text/yaml", "text/csv",
        }:
            raise SkillResourceError("Reference type is not safe for text loading.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillResourceError("Reference must be UTF-8 text.") from exc

    @staticmethod
    def execute_script(*_args, **_kwargs):
        raise SkillScriptExecutionDisabledError("Skill script execution is disabled in phase 3C.")

    @staticmethod
    def _verify_package(package_path: str, expected_hash: str) -> None:
        if hash_skill_package(Path(package_path)) != expected_hash:
            raise SkillContentChangedError("Skill package changed after registration.")

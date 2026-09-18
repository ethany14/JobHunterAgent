"""Registration, validation, approval, and activation orchestration."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.skills.errors import SkillContentChangedError, SkillValidationError
from agent_runtime.skills.hashing import hash_bytes, hash_skill_package
from agent_runtime.skills.parser import SkillParser
from agent_runtime.skills.repository import SkillRepository
from agent_runtime.skills.security import iter_package_files, media_type_for, resource_kind
from agent_runtime.skills.types import (
    SkillEventType,
    SkillResourceEntry,
    SkillResourceManifest,
    SkillStatus,
    SkillVersion,
    ValidatedSkillPackage,
)
from agent_runtime.skills.validator import SkillValidator


class SkillRegistry:
    def __init__(
        self,
        repository: SkillRepository,
        *,
        parser: SkillParser | None = None,
        validator: SkillValidator | None = None,
    ) -> None:
        self._repository = repository
        self._parser = parser or SkillParser()
        self._validator = validator or SkillValidator()

    def register(self, package_path: Path) -> SkillVersion:
        package = self.inspect_package(package_path)
        version = self._repository.create_version(package)
        version = self._repository.transition(
            version.version_id,
            expected_version=version.version,
            allowed_from={SkillStatus.DRAFT},
            to_status=SkillStatus.VALIDATING,
            event_type=SkillEventType.VALIDATION_STARTED,
        )
        return self._repository.transition(
            version.version_id,
            expected_version=version.version,
            allowed_from={SkillStatus.VALIDATING},
            to_status=SkillStatus.VALIDATED,
            event_type=SkillEventType.VALIDATED,
            warnings=package.warnings,
        )

    def request_approval(self, version_id: str, *, expected_version: int) -> SkillVersion:
        return self._repository.transition(
            version_id,
            expected_version=expected_version,
            allowed_from={SkillStatus.VALIDATED},
            to_status=SkillStatus.APPROVAL_REQUIRED,
            event_type=SkillEventType.APPROVAL_REQUESTED,
        )

    def approve(self, version_id: str, *, expected_version: int) -> SkillVersion:
        version = self._repository.require(version_id)
        if version.version != expected_version:
            from agent_runtime.skills.errors import StaleSkillVersionError
            raise StaleSkillVersionError(f"Skill version '{version_id}' has a stale version.")
        self._assert_unchanged(version)
        return self._repository.transition(
            version_id,
            expected_version=expected_version,
            allowed_from={SkillStatus.APPROVAL_REQUIRED},
            to_status=SkillStatus.APPROVED,
            event_type=SkillEventType.APPROVED,
        )

    def activate(self, version_id: str, *, expected_version: int) -> SkillVersion:
        version = self._repository.require(version_id)
        if version.version != expected_version:
            from agent_runtime.skills.errors import StaleSkillVersionError
            raise StaleSkillVersionError(f"Skill version '{version_id}' has a stale version.")
        self._assert_unchanged(version)
        if version.metadata.get("scope", "").casefold() == "generated":
            if not self._repository.has_passing_evaluation(version.version_id):
                raise SkillValidationError(
                    ["Generated Skills require a passing evaluation before activation."]
                )
        return self._repository.activate(version_id, expected_version=expected_version)

    def reject(self, version_id: str, *, expected_version: int) -> SkillVersion:
        return self._repository.transition(
            version_id,
            expected_version=expected_version,
            allowed_from={
                SkillStatus.VALIDATED,
                SkillStatus.APPROVAL_REQUIRED,
                SkillStatus.APPROVED,
            },
            to_status=SkillStatus.REJECTED,
            event_type=SkillEventType.REJECTED,
        )

    def retire(self, version_id: str, *, expected_version: int) -> SkillVersion:
        return self._repository.retire(version_id, expected_version=expected_version)

    def _assert_unchanged(self, version: SkillVersion) -> None:
        package = self.inspect_package(Path(version.package_path))
        if (
            package.content_hash != version.content_hash
            or package.resource_manifest != version.resource_manifest
            or package.parsed.name != version.name
            or package.version_label != version.version_label
            or package.parsed.instructions != version.instruction_snapshot
            or package.parsed.allowed_tools != version.allowed_tools
        ):
            raise SkillContentChangedError("Skill package changed after registration.")

    def inspect_package(self, package_path: Path) -> ValidatedSkillPackage:
        files = iter_package_files(package_path)
        if not any(relative == "SKILL.md" for relative, _ in files):
            raise SkillValidationError(["Skill package must contain SKILL.md"])
        parsed = self._parser.parse(package_path)
        report = self._validator.validate(parsed, package_path)
        if not report.valid:
            raise SkillValidationError(report.errors, report.warnings)
        package_hash = hash_skill_package(package_path)
        resources: list[SkillResourceEntry] = []
        total = 0
        for relative, path in files:
            total += path.stat().st_size
            kind = resource_kind(relative)
            if kind is not None:
                data = path.read_bytes()
                resources.append(SkillResourceEntry(
                    path=relative,
                    kind=kind,
                    size_bytes=len(data),
                    media_type=media_type_for(path),
                    sha256=hash_bytes(data),
                ))
        manifest = SkillResourceManifest(
            resources=resources,
            file_count=len(files),
            total_size_bytes=total,
        )
        version_label = parsed.version_label or f"sha256:{package_hash[:12]}"
        return ValidatedSkillPackage(
            package_path=package_path.resolve(),
            parsed=parsed,
            version_label=version_label,
            content_hash=package_hash,
            resource_manifest=manifest,
            warnings=report.warnings,
        )

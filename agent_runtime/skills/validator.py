"""Agent Skills format validation and progressive-disclosure checks."""

from __future__ import annotations

import re
from pathlib import Path

from agent_runtime.skills.security import resource_kind, safe_relative_path
from agent_runtime.skills.types import ParsedSkill, SkillValidationReport

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


class SkillValidator:
    def validate(self, parsed: ParsedSkill, package_root: Path) -> SkillValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        if not parsed.name or len(parsed.name) > 64 or not _NAME.fullmatch(parsed.name):
            errors.append("name must be 1-64 lowercase letters, digits, and single hyphens")
        if parsed.name != package_root.name:
            errors.append("name must equal the Skill directory name")
        if not parsed.description.strip() or len(parsed.description) > 1024:
            errors.append("description must be 1-1024 characters")
        if parsed.compatibility is not None and len(parsed.compatibility) > 500:
            errors.append("compatibility must not exceed 500 characters")
        if not parsed.instructions:
            errors.append("SKILL.md must contain Markdown instructions")
        if len(parsed.metadata) > 64:
            errors.append("metadata contains too many entries")
        for key, value in parsed.metadata.items():
            if not key.strip() or len(key) > 128 or len(value) > 1024:
                errors.append("metadata keys and values exceed supported limits")
                break
        if parsed.allowed_tools == frozenset():
            warnings.append("allowed-tools is empty; the Skill will have no effective tools")
        for target in _MARKDOWN_LINK.findall(parsed.instructions):
            target = target.strip().split("#", 1)[0]
            if not target or target.startswith(("https://", "http://", "mailto:")):
                continue
            try:
                relative = safe_relative_path(target)
            except Exception:
                errors.append(f"unsafe resource path in SKILL.md: {target}")
                continue
            if resource_kind(relative.as_posix()) is None:
                warnings.append(f"local link is outside references/assets/scripts: {target}")
        return SkillValidationReport(valid=not errors, errors=errors, warnings=warnings)

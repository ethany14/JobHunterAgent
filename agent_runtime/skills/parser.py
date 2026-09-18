"""Safe YAML-frontmatter parser for SKILL.md."""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_runtime.skills.errors import SkillParseError, SkillSecurityError
from agent_runtime.skills.security import MAX_SKILL_FILE_BYTES, ensure_regular_file
from agent_runtime.skills.types import ParsedSkill

SUPPORTED_FIELDS = frozenset({
    "name", "description", "license", "compatibility", "metadata", "allowed-tools"
})


class SkillParser:
    def parse(self, package_root: Path) -> ParsedSkill:
        skill_file = package_root / "SKILL.md"
        ensure_regular_file(skill_file, package_root)
        data = skill_file.read_bytes()
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise SkillSecurityError("SKILL.md is too large.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillParseError("SKILL.md must be UTF-8.") from exc
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillParseError("SKILL.md must start with YAML frontmatter.")
        try:
            closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        except StopIteration as exc:
            raise SkillParseError("SKILL.md frontmatter is not closed.") from exc
        frontmatter_text = "\n".join(lines[1:closing])
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            raise SkillParseError("SKILL.md contains unsafe or invalid YAML.") from exc
        if not isinstance(frontmatter, dict):
            raise SkillParseError("SKILL.md frontmatter must be a mapping.")
        unknown = set(frontmatter) - SUPPORTED_FIELDS
        if unknown:
            raise SkillParseError(f"Unsupported frontmatter fields: {sorted(unknown)}")
        metadata = frontmatter.get("metadata") or {}
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise SkillParseError("metadata must be a string-to-string mapping.")
        raw_tools = frontmatter.get("allowed-tools")
        if raw_tools is not None and not isinstance(raw_tools, str):
            raise SkillParseError("allowed-tools must be a space-delimited string.")
        allowed_tools = None if raw_tools is None else frozenset(raw_tools.split())
        if not isinstance(frontmatter.get("name"), str) or not isinstance(
            frontmatter.get("description"), str
        ):
            raise SkillParseError("SKILL.md requires string name and description fields.")
        for optional in ("license", "compatibility"):
            if frontmatter.get(optional) is not None and not isinstance(frontmatter[optional], str):
                raise SkillParseError(f"{optional} must be a string.")
        body = "\n".join(lines[closing + 1:]).strip()
        return ParsedSkill(
            name=frontmatter.get("name"),
            description=frontmatter.get("description"),
            license=frontmatter.get("license"),
            compatibility=frontmatter.get("compatibility"),
            metadata=metadata,
            allowed_tools=allowed_tools,
            instructions=body,
        )

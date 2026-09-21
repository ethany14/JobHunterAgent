"""Strict validation for generated, non-executable Skill packages."""
from __future__ import annotations

import re
from pathlib import Path

from agent_runtime.feedback.policy import contains_pii
from agent_runtime.skills.errors import SkillValidationError
from agent_runtime.skills.parser import SkillParser
from agent_runtime.skills.registry import SkillRegistry
from agent_runtime.skills.security import iter_package_files, safe_relative_path
from agent_runtime.skills.types import ValidatedSkillPackage

_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
_SECRET = re.compile(r"(?i)(?:api[_-]?key|password|secret|bearer|authorization)\s*[:=]\s*\S+|\b(?:sk-|AIza)[A-Za-z0-9_-]{12,}")
_UNSAFE = re.compile(r"(?i)ignore (?:system|previous|safety|approval)|bypass (?:approval|permissions)|(?:job description|\bJD\b) (?:is|as|proves?) (?:candidate )?evidence")
_PLACEHOLDER = re.compile(r"(?i)\b(?:TODO|TBD|FIXME|PLACEHOLDER)\b|<insert [^>]+>")
_PERSONAL_FACT = re.compile(r"(?i)\b(?:I|my|we)\s+(?:have|built|created|led|worked|managed|graduated|prefer)\b")


def validate_generated_package(package_root: Path, *, available_tools: frozenset[str],
                               max_files: int = 12, max_bytes: int = 64_000,
                               max_tokens: int = 8_000) -> ValidatedSkillPackage:
    files = iter_package_files(package_root)
    errors: list[str] = []
    if len(files) > max_files or sum(path.stat().st_size for _, path in files) > max_bytes:
        errors.append("Generated Skill exceeds configured package limits.")
    for relative, path in files:
        if relative != "SKILL.md" and not (relative.startswith("references/") and relative.endswith(".md")):
            errors.append("Generated Skills may contain only SKILL.md and Markdown references.")
        if any(part.startswith(".") for part in Path(relative).parts):
            errors.append("Hidden files are not permitted.")
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            errors.append("Generated resources must be readable UTF-8 text.")
            continue
        if len(content.split()) > max_tokens:
            errors.append("Generated Skill exceeds the configured token bound.")
        if contains_pii(content) or _SECRET.search(content) or _PERSONAL_FACT.search(content):
            errors.append("Personal information or secrets are not permitted.")
        unsafe_matches = [match for match in _UNSAFE.finditer(content)
            if not re.search(r"(?i)\b(?:never|do not|don't)\s+(?:\w+\s+){0,5}$",
                             content[max(0, match.start()-50):match.start()])]
        if unsafe_matches:
            errors.append("Skill instructions conflict with safety or evidence policy.")
        if _PLACEHOLDER.search(content):
            errors.append("Unfinished placeholders are not permitted.")
        for target in _LINK.findall(content):
            target = target.split("#", 1)[0]
            if not target:
                continue
            try:
                clean = safe_relative_path(target)
                resolved = (path.parent / str(clean)).resolve()
                if not resolved.is_file() or not resolved.is_relative_to(package_root.resolve()):
                    errors.append("Skill reference is unavailable or escapes the package.")
            except Exception:
                errors.append("Skill reference path is unsafe.")
    if errors:
        raise SkillValidationError(sorted(set(errors)))
    package = SkillRegistry.__new__(SkillRegistry)
    package._parser = SkillParser()
    from agent_runtime.skills.validator import SkillValidator
    package._validator = SkillValidator()
    inspected = package.inspect_package(package_root)
    tools = inspected.parsed.allowed_tools
    if tools is not None and not tools <= available_tools:
        raise SkillValidationError(["Skill tool restriction names unavailable or unauthorized tools."])
    return inspected

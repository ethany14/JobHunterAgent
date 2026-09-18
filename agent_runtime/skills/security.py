"""Filesystem safety limits for local Skill packages."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_runtime.skills.errors import SkillSecurityError
from agent_runtime.skills.types import SkillResourceKind

MAX_SKILL_FILE_BYTES = 256 * 1024
MAX_RESOURCE_FILE_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 5 * 1024 * 1024
MAX_PACKAGE_FILES = 100

RESOURCE_ROOTS = {
    "references": SkillResourceKind.REFERENCE,
    "assets": SkillResourceKind.ASSET,
    "scripts": SkillResourceKind.SCRIPT,
}


def safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if not normalized or path.is_absolute() or windows.is_absolute() or ".." in path.parts:
        raise SkillSecurityError("Resource path must be a safe relative path.")
    if any(part in {"", "."} for part in path.parts):
        raise SkillSecurityError("Resource path contains an invalid segment.")
    return path


def ensure_regular_file(path: Path, package_root: Path) -> None:
    if package_root.is_symlink() or path.is_symlink():
        raise SkillSecurityError("Skill packages may not contain symlinks.")
    try:
        path.resolve(strict=True).relative_to(package_root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise SkillSecurityError("Resource escapes the Skill package.") from exc
    if not path.is_file():
        raise SkillSecurityError("Skill resource must be a regular file.")


def resource_kind(relative_path: str) -> SkillResourceKind | None:
    path = safe_relative_path(relative_path)
    return RESOURCE_ROOTS.get(path.parts[0])


def media_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def iter_package_files(package_root: Path) -> list[tuple[str, Path]]:
    if not package_root.exists() or not package_root.is_dir() or package_root.is_symlink():
        raise SkillSecurityError("Skill package must be a real directory.")
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(package_root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise SkillSecurityError("Skill packages may not contain symlinks.")
        for name in names:
            path = current_path / name
            ensure_regular_file(path, package_root)
            relative = path.relative_to(package_root).as_posix()
            safe_relative_path(relative)
            files.append((relative, path))
    files.sort(key=lambda pair: pair[0].encode("utf-8"))
    if len(files) > MAX_PACKAGE_FILES:
        raise SkillSecurityError("Skill package contains too many files.")
    total = 0
    for relative, path in files:
        size = path.stat().st_size
        maximum = MAX_SKILL_FILE_BYTES if relative == "SKILL.md" else MAX_RESOURCE_FILE_BYTES
        if size > maximum:
            raise SkillSecurityError(f"Skill file is too large: {relative}")
        total += size
    if total > MAX_PACKAGE_BYTES:
        raise SkillSecurityError("Skill package is too large.")
    return files

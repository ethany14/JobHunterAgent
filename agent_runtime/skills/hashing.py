"""Deterministic cross-platform hashing for complete Skill packages."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent_runtime.skills.security import iter_package_files


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_skill_package(package_root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path in iter_package_files(package_root):
        relative_bytes = relative.encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()

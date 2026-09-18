from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from agent_runtime.skills import (
    InvalidSkillTransitionError,
    SkillContentChangedError,
    SkillDiscovery,
    SkillEventType,
    SkillLoader,
    SkillParseError,
    SkillRegistry,
    SkillRepository,
    SkillResourceError,
    SkillScriptExecutionDisabledError,
    SkillSecurityError,
    SkillStatus,
    SkillValidationError,
)
from agent_runtime.skills.hashing import hash_skill_package
from agent_runtime.skills.security import MAX_PACKAGE_FILES
from agent_runtime.sessions.clock import FakeClock
from api.db import create_database


def write_skill(
    root: Path,
    name: str = "test-skill",
    *,
    version: str = "1.0.0",
    allowed_tools: str | None = "read-run write-run",
    extra_frontmatter: str = "",
    body: str = "# Workflow\n\nRead [the guide](references/guide.md) when needed.",
) -> Path:
    package = root / name
    (package / "references").mkdir(parents=True)
    (package / "assets").mkdir()
    (package / "scripts").mkdir()
    tools = "" if allowed_tools is None else f"allowed-tools: {allowed_tools}\n"
    (package / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Helps test deterministic Skill registration.\n"
        "license: MIT\n"
        "compatibility: Local runtime\n"
        "metadata:\n"
        f"  version: '{version}'\n"
        "  scope: project\n"
        "  author: test-suite\n"
        f"{tools}{extra_frontmatter}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    (package / "references" / "guide.md").write_text("Reference data", encoding="utf-8")
    (package / "assets" / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (package / "scripts" / "helper.py").write_text("print('never executed')", encoding="utf-8")
    return package


@pytest.fixture
def skill_runtime(tmp_path):
    database = create_database(
        f"sqlite:///{(tmp_path / 'skills.sqlite').as_posix()}", create_schema_for_tests=True
    )
    repository = SkillRepository(database.session_factory, clock=FakeClock())
    yield database, repository, SkillRegistry(repository), SkillLoader(repository)
    database.close()


def approval_ready(registry: SkillRegistry, package: Path):
    version = registry.register(package)
    assert version.status == SkillStatus.VALIDATED
    return registry.request_approval(version.version_id, expected_version=version.version)


def approve_and_activate(registry: SkillRegistry, pending):
    approved = registry.approve(pending.version_id, expected_version=pending.version)
    assert approved.status == SkillStatus.APPROVED
    return registry.activate(approved.version_id, expected_version=approved.version)


def test_format_compliance_and_custom_metadata_convention(tmp_path):
    package = write_skill(tmp_path)
    from agent_runtime.skills.parser import SkillParser
    from agent_runtime.skills.validator import SkillValidator
    parsed = SkillParser().parse(package)
    report = SkillValidator().validate(parsed, package)
    assert report.valid
    assert parsed.metadata == {
        "version": "1.0.0", "scope": "project", "author": "test-suite"
    }
    assert parsed.version_label == "1.0.0"
    assert parsed.allowed_tools == frozenset({"read-run", "write-run"})
    assert "version" not in parsed.model_fields_set - {"metadata"}


@pytest.mark.parametrize("name", ["Upper", "bad_name", "bad--name", "-leading", "trailing-"])
def test_official_name_constraints_and_directory_equality(tmp_path, name):
    package = write_skill(tmp_path, name="directory-name")
    text = (package / "SKILL.md").read_text(encoding="utf-8")
    (package / "SKILL.md").write_text(text.replace("name: directory-name", f"name: {name}"), encoding="utf-8")
    registry = SkillRegistry.__new__(SkillRegistry)
    from agent_runtime.skills.parser import SkillParser
    from agent_runtime.skills.validator import SkillValidator
    report = SkillValidator().validate(SkillParser().parse(package), package)
    assert not report.valid


def test_unknown_fields_invalid_metadata_and_unsafe_yaml_are_rejected(tmp_path):
    from agent_runtime.skills.parser import SkillParser
    package = write_skill(tmp_path, extra_frontmatter="version: 1.0\n")
    with pytest.raises(SkillParseError, match="Unsupported"):
        SkillParser().parse(package)
    (package / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: !!python/object/apply:os.system ['echo bad']\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(SkillParseError, match="unsafe or invalid YAML"):
        SkillParser().parse(package)

    (package / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: valid\nmetadata:\n  version: 2\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(SkillParseError, match="string-to-string"):
        SkillParser().parse(package)


def test_path_traversal_and_absolute_resource_links_are_rejected(tmp_path):
    for target in ("../secret.txt", "/etc/passwd", "C:\\secret.txt"):
        root = tmp_path / target.replace("/", "_").replace("\\", "_").replace(":", "")
        package = write_skill(root, body=f"Read [unsafe]({target}).")
        with pytest.raises(SkillValidationError, match="unsafe resource path"):
            database = create_database("sqlite:///:memory:", create_schema_for_tests=True)
            try:
                SkillRegistry(SkillRepository(database.session_factory)).inspect_package(package)
            finally:
                database.close()


def test_symlinks_are_rejected(tmp_path):
    package = write_skill(tmp_path)
    target = package / "references" / "guide.md"
    link = package / "references" / "linked.md"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform.")
    with pytest.raises(SkillSecurityError, match="symlinks"):
        hash_skill_package(package)


def test_package_hash_is_order_independent_and_content_sensitive(tmp_path):
    first = write_skill(tmp_path / "first")
    second = write_skill(tmp_path / "second")
    assert hash_skill_package(first) == hash_skill_package(second)
    (second / "references" / "guide.md").write_text("Changed", encoding="utf-8")
    assert hash_skill_package(first) != hash_skill_package(second)


def test_manifest_discovers_resources_and_scripts_without_execution(tmp_path, skill_runtime):
    _, _, registry, loader = skill_runtime
    version = registry.register(write_skill(tmp_path))
    manifest = version.resource_manifest
    assert {item.path for item in manifest.resources} == {
        "assets/icon.svg", "references/guide.md", "scripts/helper.py"
    }
    assert {item.kind.value for item in manifest.resources} == {"asset", "reference", "script"}
    with pytest.raises(SkillScriptExecutionDisabledError):
        loader.execute_script(version.version_id, "scripts/helper.py")


def test_lifecycle_discovery_activation_and_tool_narrowing(tmp_path, skill_runtime):
    _, repository, registry, loader = skill_runtime
    package = write_skill(tmp_path)
    validated = registry.register(package)
    assert [event.event_type for event in repository.events(validated.version_id)] == [
        SkillEventType.VERSION_CREATED,
        SkillEventType.VALIDATION_STARTED,
        SkillEventType.VALIDATED,
    ]
    discovered = SkillDiscovery(repository).discover()[0]
    assert set(discovered.model_dump()) == {
        "skill_id", "version_id", "name", "description", "version", "status"
    }
    with pytest.raises(InvalidSkillTransitionError, match="Only active"):
        loader.activate(validated.version_id, runtime_allowed_tools=frozenset({"read-run"}))
    pending = registry.request_approval(validated.version_id, expected_version=validated.version)
    active = approve_and_activate(registry, pending)
    loaded = loader.activate(
        active.version_id,
        runtime_allowed_tools=frozenset({"read-run", "admin", "other"}),
    )
    assert loaded.effective_allowed_tools == frozenset({"read-run"})
    assert loaded.instructions == active.instruction_snapshot


def test_rejection_is_audited_and_cannot_activate(tmp_path, skill_runtime):
    _, repository, registry, loader = skill_runtime
    pending = approval_ready(registry, write_skill(tmp_path))
    rejected = registry.reject(pending.version_id, expected_version=pending.version)
    assert rejected.status == SkillStatus.REJECTED
    assert repository.events(rejected.version_id)[-1].event_type == SkillEventType.REJECTED
    with pytest.raises(InvalidSkillTransitionError):
        loader.activate(rejected.version_id, runtime_allowed_tools=frozenset())


def test_retirement_clears_active_version_atomically(tmp_path, skill_runtime):
    _, repository, registry, _ = skill_runtime
    pending = approval_ready(registry, write_skill(tmp_path))
    active = approve_and_activate(registry, pending)
    retired = repository.retire(active.version_id, expected_version=active.version)
    assert retired.status == SkillStatus.RETIRED
    assert repository.active_by_name(active.name) is None


def test_absent_allowed_tools_does_not_expand_runtime_permissions(tmp_path, skill_runtime):
    _, _, registry, loader = skill_runtime
    pending = approval_ready(registry, write_skill(tmp_path, allowed_tools=None))
    active = approve_and_activate(registry, pending)
    assert loader.activate(active.version_id, runtime_allowed_tools=frozenset({"only-this"})).effective_allowed_tools == frozenset({"only-this"})


def test_new_activation_atomically_supersedes_previous_version(tmp_path, skill_runtime):
    _, repository, registry, _ = skill_runtime
    package = write_skill(tmp_path, version="1.0.0")
    first_pending = approval_ready(registry, package)
    first = approve_and_activate(registry, first_pending)
    text = (package / "SKILL.md").read_text(encoding="utf-8")
    (package / "SKILL.md").write_text(text.replace("'1.0.0'", "'2.0.0'").replace("# Workflow", "# Updated Workflow"), encoding="utf-8")
    second_pending = approval_ready(registry, package)
    second = approve_and_activate(registry, second_pending)
    assert repository.require(first.version_id).status == SkillStatus.SUPERSEDED
    assert repository.active_by_name("test-skill").version_id == second.version_id


def test_changed_package_blocks_approval_and_loading(tmp_path, skill_runtime):
    _, _, registry, loader = skill_runtime
    package = write_skill(tmp_path)
    pending = approval_ready(registry, package)
    (package / "references" / "guide.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(SkillContentChangedError) as error:
        registry.approve(pending.version_id, expected_version=pending.version)
    assert error.value.code == "skill_content_changed"
    (package / "references" / "guide.md").write_text("Reference data", encoding="utf-8")
    active = approve_and_activate(registry, pending)
    (package / "SKILL.md").write_text((package / "SKILL.md").read_text() + "\nchanged", encoding="utf-8")
    with pytest.raises(SkillContentChangedError):
        loader.activate(active.version_id, runtime_allowed_tools=frozenset())


def test_safe_reference_loading_checks_manifest_and_hash(tmp_path, skill_runtime):
    _, _, registry, loader = skill_runtime
    pending = approval_ready(registry, write_skill(tmp_path))
    active = approve_and_activate(registry, pending)
    assert loader.load_reference(active.version_id, "references/guide.md") == "Reference data"
    with pytest.raises((SkillSecurityError, SkillResourceError)):
        loader.load_reference(active.version_id, "../secret.txt")
    with pytest.raises(SkillResourceError):
        loader.load_reference(active.version_id, "scripts/helper.py")


def test_activation_event_failure_rolls_back_supersession(tmp_path, skill_runtime, monkeypatch):
    _, repository, registry, _ = skill_runtime
    package = write_skill(tmp_path, version="1.0.0")
    first_pending = approval_ready(registry, package)
    first = approve_and_activate(registry, first_pending)
    text = (package / "SKILL.md").read_text(encoding="utf-8")
    (package / "SKILL.md").write_text(text.replace("'1.0.0'", "'2.0.0'"), encoding="utf-8")
    second_pending = approval_ready(registry, package)
    second = registry.approve(
        second_pending.version_id, expected_version=second_pending.version
    )
    original = repository._event_row

    def fail(event):
        if event.event_type == SkillEventType.ACTIVATED:
            raise RuntimeError("injected event failure")
        return original(event)

    monkeypatch.setattr(repository, "_event_row", fail)
    with pytest.raises(RuntimeError, match="injected"):
        registry.activate(second.version_id, expected_version=second.version)
    assert repository.require(first.version_id).status == SkillStatus.ACTIVE
    assert repository.require(second.version_id).status == SkillStatus.APPROVED
    assert repository.active_by_name("test-skill").version_id == first.version_id


def test_package_file_count_limit(tmp_path, monkeypatch):
    import agent_runtime.skills.security as security
    package = write_skill(tmp_path)
    monkeypatch.setattr(security, "MAX_PACKAGE_FILES", 3)
    with pytest.raises(SkillSecurityError, match="too many files"):
        hash_skill_package(package)
    monkeypatch.setattr(security, "MAX_PACKAGE_FILES", MAX_PACKAGE_FILES)


def test_oversized_file_is_rejected_without_large_fixture(tmp_path, monkeypatch):
    import agent_runtime.skills.security as security
    package = write_skill(tmp_path)
    monkeypatch.setattr(security, "MAX_RESOURCE_FILE_BYTES", 2)
    with pytest.raises(SkillSecurityError, match="too large"):
        hash_skill_package(package)


def test_migration_upgrades_existing_database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0009_memory_runtime")
    command.upgrade(config, "head")
    database = create_database(url)
    try:
        assert {"skill_registry", "skill_versions", "skill_events"} <= set(
            inspect(database.engine).get_table_names()
        )
    finally:
        database.close()

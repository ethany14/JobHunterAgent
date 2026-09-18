"""Transactional Skill version registry and approval lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.security import canonical_json, redact_sensitive
from agent_runtime.sessions.clock import Clock, SystemClock
from agent_runtime.skills.errors import (
    InvalidSkillTransitionError,
    SkillNotFoundError,
    SkillVersionConflictError,
    StaleSkillVersionError,
)
from agent_runtime.skills.models import (
    SkillEvaluationResultRow,
    SkillEventRow,
    SkillRow,
    SkillVersionRow,
)
from agent_runtime.skills.types import (
    SkillDiscoveryRecord,
    SkillEvent,
    SkillEventType,
    SkillStatus,
    SkillVersion,
    ValidatedSkillPackage,
)


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SkillRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, clock: Clock | None = None) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    def create_version(self, package: ValidatedSkillPackage) -> SkillVersion:
        now = ensure_utc(self._clock.now())
        parsed = package.parsed
        with self._session_factory.begin() as session:
            skill = session.scalar(select(SkillRow).where(SkillRow.name == parsed.name))
            if skill is None:
                skill = SkillRow(
                    skill_id=str(uuid4()), name=parsed.name, active_version_id=None,
                    created_at=now, updated_at=now,
                )
                session.add(skill)
                session.flush()
            duplicate = session.scalar(select(SkillVersionRow.version_id).where(
                SkillVersionRow.skill_id == skill.skill_id,
                SkillVersionRow.version_label == package.version_label,
            ))
            if duplicate is not None:
                raise SkillVersionConflictError(
                    f"Skill '{parsed.name}' version '{package.version_label}' already exists."
                )
            version = SkillVersion(
                skill_id=skill.skill_id,
                name=parsed.name,
                description=parsed.description,
                version_label=package.version_label,
                package_path=str(package.package_path.resolve()),
                license=parsed.license,
                compatibility=parsed.compatibility,
                metadata=parsed.metadata,
                allowed_tools=parsed.allowed_tools,
                instruction_snapshot=parsed.instructions,
                content_hash=package.content_hash,
                resource_manifest=package.resource_manifest,
                validation_warnings=package.warnings,
                event_sequence=1,
                created_at=now,
                updated_at=now,
            )
            session.add(self._row(version))
            session.flush()
            session.add(self._event_row(SkillEvent(
                version_id=version.version_id,
                sequence=1,
                event_type=SkillEventType.VERSION_CREATED,
                to_status=SkillStatus.DRAFT,
                occurred_at=now,
            )))
            try:
                session.flush()
            except IntegrityError as exc:
                raise SkillVersionConflictError(
                    f"Skill '{parsed.name}' version '{package.version_label}' already exists."
                ) from exc
        return version

    def get(self, version_id: str) -> SkillVersion | None:
        with self._session_factory() as session:
            row = session.get(SkillVersionRow, version_id)
            return None if row is None else self._version(row)

    def require(self, version_id: str) -> SkillVersion:
        version = self.get(version_id)
        if version is None:
            raise SkillNotFoundError(f"Skill version '{version_id}' was not found.")
        return version

    def active_by_name(self, name: str) -> SkillVersion | None:
        with self._session_factory() as session:
            skill = session.scalar(select(SkillRow).where(SkillRow.name == name))
            if skill is None or skill.active_version_id is None:
                return None
            row = session.get(SkillVersionRow, skill.active_version_id)
            return None if row is None else self._version(row)

    def discover(self) -> list[SkillDiscoveryRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SkillVersionRow.skill_id,
                    SkillVersionRow.version_id,
                    SkillVersionRow.name,
                    SkillVersionRow.description,
                    SkillVersionRow.version_label,
                    SkillVersionRow.status,
                ).order_by(
                    SkillVersionRow.name, SkillVersionRow.version_label, SkillVersionRow.version_id
                )
            ).all()
            return [SkillDiscoveryRecord(
                skill_id=row.skill_id,
                version_id=row.version_id,
                name=row.name,
                description=row.description,
                version=row.version_label,
                status=SkillStatus(row.status),
            ) for row in rows]

    def versions_by_name(self, name: str) -> list[SkillVersion]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SkillVersionRow)
                .where(SkillVersionRow.name == name)
                .order_by(
                    SkillVersionRow.created_at.desc(),
                    SkillVersionRow.version_id,
                )
            ).all()
            return [self._version(row) for row in rows]

    def transition(
        self,
        version_id: str,
        *,
        expected_version: int,
        allowed_from: set[SkillStatus],
        to_status: SkillStatus,
        event_type: SkillEventType,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> SkillVersion:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._current(session, version_id, expected_version)
            if current.status not in allowed_from:
                raise InvalidSkillTransitionError(
                    f"Skill version in '{current.status.value}' cannot transition to '{to_status.value}'."
                )
            next_version = self._updated(current, {
                "status": to_status,
                "version": current.version + 1,
                "event_sequence": current.event_sequence + 1,
                "validation_errors": current.validation_errors if errors is None else errors,
                "validation_warnings": current.validation_warnings if warnings is None else warnings,
                "updated_at": now,
            })
            self._persist(session, next_version, expected_version)
            session.add(self._event_row(SkillEvent(
                version_id=version_id,
                sequence=next_version.event_sequence,
                event_type=event_type,
                from_status=current.status,
                to_status=to_status,
                payload=redact_sensitive({"error_count": len(errors or [])}),
                occurred_at=now,
            )))
            session.flush()
        return next_version

    def activate(self, version_id: str, *, expected_version: int) -> SkillVersion:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            row, current = self._current(session, version_id, expected_version)
            if current.status != SkillStatus.APPROVED:
                raise InvalidSkillTransitionError("Only an approved Skill version may be activated.")
            skill = session.get(SkillRow, current.skill_id)
            if skill is None:
                raise SkillNotFoundError(f"Skill '{current.skill_id}' was not found.")
            if skill.active_version_id and skill.active_version_id != version_id:
                old_row = session.get(SkillVersionRow, skill.active_version_id)
                if old_row is not None:
                    old = self._version(old_row)
                    if old.status == SkillStatus.ACTIVE:
                        old_next = self._updated(old, {
                            "status": SkillStatus.SUPERSEDED,
                            "version": old.version + 1,
                            "event_sequence": old.event_sequence + 1,
                            "updated_at": now,
                        })
                        self._persist(session, old_next, old.version)
                        session.add(self._event_row(SkillEvent(
                            version_id=old.version_id,
                            sequence=old_next.event_sequence,
                            event_type=SkillEventType.SUPERSEDED,
                            from_status=SkillStatus.ACTIVE,
                            to_status=SkillStatus.SUPERSEDED,
                            payload={"replacement_version_id": version_id},
                            occurred_at=now,
                        )))
            active = self._updated(current, {
                "status": SkillStatus.ACTIVE,
                "version": current.version + 1,
                "event_sequence": current.event_sequence + 1,
                "updated_at": now,
            })
            self._persist(session, active, expected_version)
            session.add(self._event_row(SkillEvent(
                version_id=version_id,
                sequence=active.event_sequence,
                event_type=SkillEventType.ACTIVATED,
                from_status=current.status,
                to_status=SkillStatus.ACTIVE,
                occurred_at=now,
            )))
            skill.active_version_id = version_id
            skill.updated_at = now
            session.flush()
        return active

    def retire(self, version_id: str, *, expected_version: int) -> SkillVersion:
        now = ensure_utc(self._clock.now())
        with self._session_factory.begin() as session:
            _, current = self._current(session, version_id, expected_version)
            if current.status != SkillStatus.ACTIVE:
                raise InvalidSkillTransitionError("Only an active Skill version may be retired.")
            retired = self._updated(current, {
                "status": SkillStatus.RETIRED,
                "version": current.version + 1,
                "event_sequence": current.event_sequence + 1,
                "updated_at": now,
            })
            self._persist(session, retired, expected_version)
            session.add(self._event_row(SkillEvent(
                version_id=version_id,
                sequence=retired.event_sequence,
                event_type=SkillEventType.RETIRED,
                from_status=SkillStatus.ACTIVE,
                to_status=SkillStatus.RETIRED,
                occurred_at=now,
            )))
            skill = session.get(SkillRow, retired.skill_id)
            if skill is None or skill.active_version_id != version_id:
                raise InvalidSkillTransitionError("Skill registry active version is inconsistent.")
            skill.active_version_id = None
            skill.updated_at = now
            session.flush()
        return retired

    def events(self, version_id: str) -> list[SkillEvent]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SkillEventRow).where(SkillEventRow.version_id == version_id)
                .order_by(SkillEventRow.sequence)
            ).all()
            return [SkillEvent(
                event_id=row.event_id, version_id=row.version_id, sequence=row.sequence,
                event_type=row.event_type, from_status=row.from_status, to_status=row.to_status,
                payload=json.loads(row.payload_json), occurred_at=ensure_utc(row.occurred_at),
            ) for row in rows]

    def record_evaluation(
        self,
        version_id: str,
        *,
        suite_name: str,
        artifact_hash: str,
        passed: bool,
    ) -> None:
        if len(artifact_hash) != 64 or any(
            character not in "0123456789abcdef" for character in artifact_hash
        ):
            raise ValueError("Evaluation artifact hash must be lowercase SHA-256.")
        with self._session_factory.begin() as session:
            if session.get(SkillVersionRow, version_id) is None:
                raise SkillNotFoundError(f"Skill version '{version_id}' was not found.")
            session.add(SkillEvaluationResultRow(
                evaluation_id=str(uuid4()), version_id=version_id,
                suite_name=suite_name.strip(), artifact_hash=artifact_hash,
                passed=passed, evaluated_at=ensure_utc(self._clock.now()),
            ))

    def has_passing_evaluation(self, version_id: str) -> bool:
        with self._session_factory() as session:
            return session.scalar(
                select(SkillEvaluationResultRow.evaluation_id)
                .where(
                    SkillEvaluationResultRow.version_id == version_id,
                    SkillEvaluationResultRow.passed.is_(True),
                )
                .limit(1)
            ) is not None

    def _current(self, session: Session, version_id: str,
                 expected_version: int) -> tuple[SkillVersionRow, SkillVersion]:
        row = session.get(SkillVersionRow, version_id)
        if row is None:
            raise SkillNotFoundError(f"Skill version '{version_id}' was not found.")
        if row.version != expected_version:
            raise StaleSkillVersionError(f"Skill version '{version_id}' has a stale version.")
        return row, self._version(row)

    def _persist(self, session: Session, version: SkillVersion, expected_version: int) -> None:
        changed = session.execute(
            update(SkillVersionRow).where(
                SkillVersionRow.version_id == version.version_id,
                SkillVersionRow.version == expected_version,
            ).values(**self._projection(version))
        )
        if changed.rowcount != 1:
            raise StaleSkillVersionError(f"Skill version '{version.version_id}' has a stale version.")

    @staticmethod
    def _updated(version: SkillVersion, updates: dict[str, Any]) -> SkillVersion:
        return SkillVersion.model_validate({**version.model_dump(mode="python"), **updates})

    @staticmethod
    def _version(row: SkillVersionRow) -> SkillVersion:
        return SkillVersion.model_validate_json(row.version_json)

    @staticmethod
    def _row(version: SkillVersion) -> SkillVersionRow:
        return SkillVersionRow(**SkillRepository._projection(version))

    @staticmethod
    def _projection(version: SkillVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id, "skill_id": version.skill_id,
            "schema_version": version.schema_version, "name": version.name,
            "description": version.description, "version_label": version.version_label,
            "status": version.status.value, "package_path": version.package_path,
            "license": version.license, "compatibility": version.compatibility,
            "metadata_json": canonical_json(version.metadata),
            "allowed_tools_json": None if version.allowed_tools is None else canonical_json(sorted(version.allowed_tools)),
            "instruction_snapshot": version.instruction_snapshot,
            "content_hash": version.content_hash,
            "resource_manifest_json": version.resource_manifest.model_dump_json(),
            "validation_errors_json": canonical_json(version.validation_errors),
            "validation_warnings_json": canonical_json(version.validation_warnings),
            "version": version.version, "event_sequence": version.event_sequence,
            "version_json": version.model_dump_json(), "created_at": version.created_at,
            "updated_at": version.updated_at,
        }

    @staticmethod
    def _event_row(event: SkillEvent) -> SkillEventRow:
        return SkillEventRow(
            event_id=event.event_id, version_id=event.version_id, sequence=event.sequence,
            event_type=event.event_type.value,
            from_status=None if event.from_status is None else event.from_status.value,
            to_status=event.to_status.value,
            payload_json=canonical_json(redact_sensitive(event.payload)),
            occurred_at=event.occurred_at,
        )

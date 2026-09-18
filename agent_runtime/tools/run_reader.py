"""Read-only boundary for accessing persisted Job Agent runs."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from api.models import Run
from job_agent.results import public_result
from agent_runtime.tools.schemas import PublicRunResult, RunStatus

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

class ReadRun(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    run_id: str
    status: RunStatus
    result: PublicRunResult | None
    created_at: datetime
    updated_at: datetime

class RunReader(Protocol):
    def list_recent_runs(self, limit: int) -> list[ReadRun]: ...
    def get_run(self, run_id: str) -> ReadRun | None: ...

class SqlAlchemyRunReader:
    """Queries only safe run columns; raw resume and JD columns are never selected."""
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_recent_runs(self, limit: int) -> list[ReadRun]:
        with self._session_factory() as session:
            rows = session.execute(select(Run.run_id, Run.status, Run.result_json,
                Run.created_at, Run.updated_at).order_by(Run.updated_at.desc()).limit(limit)).all()
        return [self._read(*row) for row in rows]

    def get_run(self, run_id: str) -> ReadRun | None:
        with self._session_factory() as session:
            row = session.execute(select(Run.run_id, Run.status, Run.result_json,
                Run.created_at, Run.updated_at).where(Run.run_id == run_id)).one_or_none()
        return None if row is None else self._read(*row)

    @staticmethod
    def _read(run_id, status, result_json, created_at, updated_at) -> ReadRun:
        result = None
        if result_json is not None:
            try:
                stored = json.loads(result_json)
                # Reapply the established public projection at the storage boundary.
                result = PublicRunResult.model_validate(public_result(stored))
            except (json.JSONDecodeError, ValueError, RuntimeError):
                # Legacy, partial, or corrupt rows are visible as having no safe
                # public result; their raw payload is never returned.
                result = None
        return ReadRun(run_id=run_id, status=status, result=result,
            created_at=_utc(created_at), updated_at=_utc(updated_at))

"""Database operations for runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from api.models import Run, utc_now
from api.schemas.runs import RunStatus


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    thread_id: str
    status: RunStatus
    resume_text: str
    job_description: str
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class RunRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        resume_text: str,
        job_description: str,
    ) -> RunRecord:
        row = Run(
            run_id=run_id,
            thread_id=thread_id,
            status="running",
            resume_text=resume_text,
            job_description=job_description,
        )
        with self._session_factory.begin() as session:
            session.add(row)
        return self._record(row)

    def get(self, run_id: str) -> RunRecord | None:
        with self._session_factory() as session:
            row = session.get(Run, run_id)
            return None if row is None else self._record(row)

    def update(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: dict[str, Any] | None,
        error_message: str | None,
    ) -> RunRecord | None:
        """Update status and retain the latest complete result when result is None."""
        with self._session_factory.begin() as session:
            row = session.get(Run, run_id)
            if row is None:
                return None
            row.status = status
            if result is not None:
                row.result_json = self._encode_result(result)
            row.error_message = error_message
            row.updated_at = utc_now()
        return self._record(row)

    def transition_status(
        self,
        run_id: str,
        *,
        expected: RunStatus,
        new_status: RunStatus,
    ) -> bool:
        """Atomically claim a run for review processing."""
        with self._session_factory.begin() as session:
            result = session.execute(
                update(Run)
                .where(Run.run_id == run_id, Run.status == expected)
                .values(status=new_status, updated_at=utc_now())
            )
            return result.rowcount == 1

    @staticmethod
    def _encode_result(result: dict[str, Any] | None) -> str | None:
        if result is None:
            return None
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _record(row: Run) -> RunRecord:
        result = json.loads(row.result_json) if row.result_json is not None else None
        return RunRecord(
            run_id=row.run_id,
            thread_id=row.thread_id,
            status=row.status,  # type: ignore[arg-type]
            resume_text=row.resume_text,
            job_description=row.job_description,
            result=result,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

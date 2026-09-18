"""Evidence-based classification of runs created before backend cutover."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from api.models import Run
from custom_agent.models import CustomAgentStateRow


@dataclass(frozen=True)
class BackendClassificationReport:
    custom: int
    langgraph: int
    unknown: int
    ambiguous: tuple[str, ...]
    no_evidence: tuple[str, ...]


def _checkpoint_threads(path: Path) -> set[str]:
    if not path.exists():
        return set()
    connection = sqlite3.connect(path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        if exists is None:
            return set()
        return {str(row[0]) for row in connection.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ).fetchall()}
    finally:
        connection.close()


def classify_legacy_run_backends(
    session_factory: sessionmaker[Session], checkpoint_path: str | Path
) -> BackendClassificationReport:
    checkpoint_ids = _checkpoint_threads(Path(checkpoint_path))
    ambiguous: list[str] = []
    no_evidence: list[str] = []
    with session_factory.begin() as session:
        custom_ids = set(session.scalars(select(CustomAgentStateRow.run_id)).all())
        runs = session.scalars(select(Run)).all()
        for row in runs:
            if row.backend_source in {"explicit_new_run", "langgraph_checkpoint"}:
                continue
            custom = row.run_id in custom_ids
            langgraph = row.thread_id in checkpoint_ids
            if custom and langgraph:
                row.backend = "unknown"
                row.backend_source = "ambiguous"
                ambiguous.append(row.run_id)
            elif custom:
                row.backend = "custom"
                row.backend_source = "custom_state"
            elif langgraph:
                row.backend = "langgraph"
                row.backend_source = "langgraph_checkpoint"
            else:
                row.backend = "unknown"
                row.backend_source = "no_evidence"
                no_evidence.append(row.run_id)

        counts = {"custom": 0, "langgraph": 0, "unknown": 0}
        for backend in session.scalars(select(Run.backend)).all():
            counts[backend] += 1
    return BackendClassificationReport(custom=counts["custom"],
        langgraph=counts["langgraph"], unknown=counts["unknown"],
        ambiguous=tuple(sorted(ambiguous)), no_evidence=tuple(sorted(no_evidence)))

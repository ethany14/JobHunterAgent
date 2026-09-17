"""Build the API's durable database and LangGraph runtime."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from api.db import create_database, upgrade_database
from api.repositories.run_repository import RunRepository
from api.services.run_service import RunService
from job_agent.graph import builder
from job_agent.results import public_result

DEFAULT_CHECKPOINT_PATH = "job_agent_checkpoints.sqlite"


def create_run_service(
    *,
    database_url: str | None = None,
    checkpoint_path: str | Path | None = None,
    graph_builder: Any = builder,
    result_serializer: Callable[[dict[str, Any]], dict[str, Any]] = public_result,
) -> RunService:
    upgrade_database(database_url)
    database = create_database(database_url)
    path = Path(
        checkpoint_path
        or os.getenv("JOB_AGENT_CHECKPOINT_PATH", DEFAULT_CHECKPOINT_PATH)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    checkpointer = SqliteSaver(
        connection,
        serde=JsonPlusSerializer(allowed_msgpack_modules=None),
    )
    checkpointer.setup()
    graph = graph_builder.compile(checkpointer=checkpointer)
    return RunService(
        repository=RunRepository(database.session_factory),
        graph=graph,
        result_serializer=result_serializer,
        resources=(database, connection),
    )

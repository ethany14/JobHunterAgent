"""Build the custom-default API runtime and frozen LangGraph legacy backend."""
from __future__ import annotations

import os
import sqlite3
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from api.backend_classification import classify_legacy_run_backends
from api.db import create_database, upgrade_database
from api.repositories.run_repository import RunRepository
from api.services.run_service import (BackendRoutingRunService, CustomRunService,
    RunService)
from custom_agent.handlers import JobAgentStepHandler
from custom_agent.loop import AgentLoop
from custom_agent.repository import StateRepository
from job_agent.graph import builder
from job_agent.results import public_result

DEFAULT_CHECKPOINT_PATH = "job_agent_checkpoints.sqlite"
logger = logging.getLogger(__name__)


def _report_classification(report: Any) -> None:
    logger.info(
        "Run backend classification: custom=%d langgraph=%d unknown=%d",
        report.custom,
        report.langgraph,
        report.unknown,
    )
    if report.unknown:
        logger.warning(
            "Some runs have no safe backend classification: ambiguous=%d no_evidence=%d",
            len(report.ambiguous),
            len(report.no_evidence),
        )


def _checkpoint_graph(path: Path, graph_builder: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    checkpointer = SqliteSaver(connection,
        serde=JsonPlusSerializer(allowed_msgpack_modules=None))
    checkpointer.setup()
    return connection, graph_builder.compile(checkpointer=checkpointer)


def create_run_service(*, database_url: str | None = None,
                       checkpoint_path: str | Path | None = None,
                       graph_builder: Any = builder,
                       result_serializer: Callable[[dict[str, Any]], dict[str, Any]] = public_result,
                       custom_handler: Any = None) -> BackendRoutingRunService:
    """Create new runs on custom; dispatch historical review by stored backend."""
    path = Path(checkpoint_path or os.getenv(
        "JOB_AGENT_CHECKPOINT_PATH", DEFAULT_CHECKPOINT_PATH))
    upgrade_database(database_url)
    database = create_database(database_url)
    report = classify_legacy_run_backends(database.session_factory, path)
    _report_classification(report)
    connection, graph = _checkpoint_graph(path, graph_builder)
    run_repository = RunRepository(database.session_factory)
    custom_loop = AgentLoop(repository=StateRepository(database.session_factory),
        handler=custom_handler or JobAgentStepHandler(),
        result_projector=result_serializer)
    custom = CustomRunService(repository=run_repository, loop=custom_loop)
    langgraph = RunService(repository=run_repository, graph=graph,
        result_serializer=result_serializer)
    return BackendRoutingRunService(repository=run_repository, custom=custom,
        langgraph=langgraph, resources=(database, connection),
        classification_report=report)


def create_frozen_langgraph_run_service(*, database_url: str | None = None,
                                        checkpoint_path: str | Path | None = None,
                                        graph_builder: Any = builder,
                                        result_serializer: Callable[[dict[str, Any]], dict[str, Any]] = public_result) -> RunService:
    """Frozen baseline factory for compatibility tests and explicit evaluations."""
    path = Path(checkpoint_path or os.getenv(
        "JOB_AGENT_CHECKPOINT_PATH", DEFAULT_CHECKPOINT_PATH))
    upgrade_database(database_url)
    database = create_database(database_url)
    _report_classification(
        classify_legacy_run_backends(database.session_factory, path)
    )
    connection, graph = _checkpoint_graph(path, graph_builder)
    return RunService(repository=RunRepository(database.session_factory), graph=graph,
        result_serializer=result_serializer, resources=(database, connection))

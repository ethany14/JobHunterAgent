"""Database configuration for persisted API runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///./job_agent.db"


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]

    def create_all_for_tests(self) -> None:
        # Importing registers model metadata on Base before create_all runs.
        import api.models  # noqa: F401
        import custom_agent.models  # noqa: F401

        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()


def resolve_database_url(database_url: str | None = None) -> str:
    return database_url or os.getenv("JOB_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)


def upgrade_database(database_url: str | None = None) -> None:
    """Apply Alembic migrations for an application database."""
    config_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", resolve_database_url(database_url))
    command.upgrade(config, "head")


def create_database(
    database_url: str | None = None, *, create_schema_for_tests: bool = False
) -> Database:
    url = resolve_database_url(database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    database = Database(
        engine=engine,
        session_factory=sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        ),
    )
    if create_schema_for_tests:
        database.create_all_for_tests()
    return database

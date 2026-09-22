"""Transactional access to local resume documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.resumes.models import ResumeDocumentRow


class ResumeNotFoundError(LookupError):
    pass


class ResumeConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResumeDocument:
    resume_id: str
    filename: str
    display_name: str
    content_sha256: str
    extracted_text: str
    page_count: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ResumeDocumentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(self, *, owner_id: str, filename: str, display_name: str,
               content_sha256: str, extracted_text: str, page_count: int,
               make_default: bool = True) -> ResumeDocument:
        now = datetime.now(UTC)
        row = ResumeDocumentRow(
            resume_id=str(uuid4()), owner_id=owner_id, filename=filename,
            display_name=display_name, content_sha256=content_sha256,
            extracted_text=extracted_text, page_count=page_count,
            is_default=make_default, created_at=now, updated_at=now,
        )
        try:
            with self._session_factory.begin() as session:
                if make_default:
                    session.execute(update(ResumeDocumentRow).where(
                        ResumeDocumentRow.owner_id == owner_id
                    ).values(is_default=False, updated_at=now))
                session.add(row)
        except IntegrityError as exc:
            raise ResumeConflictError("This PDF resume has already been uploaded.") from exc
        return self._record(row)

    def list(self, owner_id: str) -> list[ResumeDocument]:
        with self._session_factory() as session:
            rows = session.scalars(select(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id
            ).order_by(ResumeDocumentRow.is_default.desc(),
                       ResumeDocumentRow.updated_at.desc())).all()
            return [self._record(row) for row in rows]

    def require(self, owner_id: str, resume_id: str) -> ResumeDocument:
        with self._session_factory() as session:
            row = session.scalar(select(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id,
                ResumeDocumentRow.resume_id == resume_id,
            ))
            if row is None:
                raise ResumeNotFoundError("The resume was not found.")
            return self._record(row)

    def default(self, owner_id: str) -> ResumeDocument:
        with self._session_factory() as session:
            row = session.scalar(select(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id,
                ResumeDocumentRow.is_default.is_(True),
            ).order_by(ResumeDocumentRow.updated_at.desc()))
            if row is None:
                raise ResumeNotFoundError(
                    "Upload a PDF resume in the web app before analyzing a job."
                )
            return self._record(row)

    def set_default(self, owner_id: str, resume_id: str) -> ResumeDocument:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            row = session.scalar(select(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id,
                ResumeDocumentRow.resume_id == resume_id,
            ))
            if row is None:
                raise ResumeNotFoundError("The resume was not found.")
            session.execute(update(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id
            ).values(is_default=False, updated_at=now))
            row.is_default = True
            row.updated_at = now
        return self._record(row)

    def delete(self, owner_id: str, resume_id: str) -> None:
        with self._session_factory.begin() as session:
            row = session.scalar(select(ResumeDocumentRow).where(
                ResumeDocumentRow.owner_id == owner_id,
                ResumeDocumentRow.resume_id == resume_id,
            ))
            if row is None:
                raise ResumeNotFoundError("The resume was not found.")
            was_default = row.is_default
            session.delete(row)
            session.flush()
            if was_default:
                replacement = session.scalar(select(ResumeDocumentRow).where(
                    ResumeDocumentRow.owner_id == owner_id
                ).order_by(ResumeDocumentRow.updated_at.desc()))
                if replacement is not None:
                    replacement.is_default = True
                    replacement.updated_at = datetime.now(UTC)

    @staticmethod
    def _record(row: ResumeDocumentRow) -> ResumeDocument:
        return ResumeDocument(
            resume_id=row.resume_id, filename=row.filename,
            display_name=row.display_name, content_sha256=row.content_sha256,
            extracted_text=row.extracted_text, page_count=row.page_count,
            is_default=row.is_default, created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
        )

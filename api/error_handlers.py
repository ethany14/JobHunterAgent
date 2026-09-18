"""Stable, non-sensitive HTTP mappings for session runtime errors."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agent_runtime.errors import (
    ApprovalBindingError,
    IdempotencyConflictError,
    InvalidToolCallTransitionError,
    StaleToolCallError,
    ToolCallNotFoundError,
    UnknownToolError,
)
from agent_runtime.sessions.errors import (
    InvalidSessionTransitionError,
    MessageConflictError,
    SessionAlreadyExistsError,
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
    SessionNotFoundError,
    SessionTerminalError,
    StaleSessionError,
)
from api.services.run_service import RunNotFoundError
from agent_runtime.context.snapshots import ContextSnapshotUnavailableError
from agent_runtime.memory.errors import (
    InvalidMemoryTransitionError,
    MemoryAlreadyExistsError,
    MemoryConfirmationRequiredError,
    MemoryNotFoundError,
    MemoryOwnerMismatchError,
    StaleMemoryError,
)
from agent_runtime.skills.errors import (
    InvalidSkillTransitionError,
    SkillContentChangedError,
    SkillNotFoundError,
    SkillSecurityError,
    SkillValidationError,
    SkillVersionConflictError,
    StaleSkillVersionError,
)

logger = logging.getLogger(__name__)


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_: Request, __: SessionNotFoundError) -> JSONResponse:
        return _response(404, "session_not_found", "The session was not found.")

    @app.exception_handler(ToolCallNotFoundError)
    async def tool_call_not_found(_: Request, __: ToolCallNotFoundError) -> JSONResponse:
        return _response(404, "tool_call_not_found", "The tool call was not found.")

    @app.exception_handler(RunNotFoundError)
    async def run_not_found(_: Request, __: RunNotFoundError) -> JSONResponse:
        return _response(404, "run_not_found", "The associated run was not found.")

    @app.exception_handler(MemoryNotFoundError)
    async def memory_not_found(_: Request, __: MemoryNotFoundError) -> JSONResponse:
        return _response(404, "memory_not_found", "The Memory item was not found.")

    @app.exception_handler(SkillNotFoundError)
    async def skill_not_found(_: Request, __: SkillNotFoundError) -> JSONResponse:
        return _response(404, "skill_not_found", "The Skill version was not found.")

    conflict_errors = (
        SessionAlreadyExistsError,
        StaleSessionError,
        MessageConflictError,
        InvalidSessionTransitionError,
        SessionTerminalError,
        SessionClaimConflictError,
        SessionClaimNotOwnedError,
        StaleToolCallError,
        IdempotencyConflictError,
        ApprovalBindingError,
        InvalidToolCallTransitionError,
        MemoryAlreadyExistsError,
        StaleMemoryError,
        InvalidMemoryTransitionError,
        StaleSkillVersionError,
        SkillVersionConflictError,
        InvalidSkillTransitionError,
        SkillContentChangedError,
        ContextSnapshotUnavailableError,
    )

    async def conflict(_: Request, exc: Exception) -> JSONResponse:
        codes = {
            StaleSessionError: "stale_session_version",
            MessageConflictError: "message_conflict",
            SessionClaimConflictError: "session_claim_conflict",
            SessionClaimNotOwnedError: "session_claim_not_owned",
            StaleMemoryError: "stale_memory_version",
            MemoryAlreadyExistsError: "memory_value_conflict",
            StaleSkillVersionError: "stale_skill_version",
            SkillContentChangedError: "skill_content_changed",
            ContextSnapshotUnavailableError: "context_snapshot_unavailable",
        }
        code = next((value for kind, value in codes.items() if isinstance(exc, kind)), "invalid_state")
        return _response(409, code, "The request conflicts with the current persisted state.")

    for error_type in conflict_errors:
        app.add_exception_handler(error_type, conflict)

    @app.exception_handler(UnknownToolError)
    async def forbidden_tool(_: Request, __: UnknownToolError) -> JSONResponse:
        return _response(403, "tool_not_allowed", "The requested capability is not allowed.")

    @app.exception_handler(MemoryOwnerMismatchError)
    async def forbidden_memory(_: Request, __: MemoryOwnerMismatchError) -> JSONResponse:
        return _response(403, "memory_not_allowed", "The Memory item is not accessible.")

    @app.exception_handler(SkillSecurityError)
    async def forbidden_skill(_: Request, __: SkillSecurityError) -> JSONResponse:
        return _response(403, "skill_not_allowed", "The Skill package is not allowed.")

    @app.exception_handler(SkillValidationError)
    async def invalid_skill(_: Request, __: SkillValidationError) -> JSONResponse:
        return _response(422, "skill_validation_failed", "The Skill failed validation.")

    @app.exception_handler(MemoryConfirmationRequiredError)
    async def confirmation_required(_: Request, __: MemoryConfirmationRequiredError) -> JSONResponse:
        return _response(422, "memory_confirmation_required", "Explicit confirmation is required.")

    @app.exception_handler(ValueError)
    async def invalid_domain_input(_: Request, __: ValueError) -> JSONResponse:
        return _response(422, "invalid_request", "The request is invalid.")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled Session API error", exc_info=exc)
        return _response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "The request could not be completed.",
        )

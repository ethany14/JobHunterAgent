"""Versioned persistence contracts for future agent sessions."""

from agent_runtime.sessions.errors import (
    InvalidSessionTransitionError,
    MessageConflictError,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    SessionTerminalError,
    StaleSessionError,
    SessionClaimConflictError,
    SessionClaimNotOwnedError,
)
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.coordinator import SessionCoordinator
from agent_runtime.sessions.outcome import SessionOutcome, SessionOutcomeStatus
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import (
    PersistedSessionMessage,
    SessionMessageDraft,
    SessionMessageVisibility,
    SessionState,
    SessionStatus,
)
from agent_runtime.sessions.clock import Clock, FakeClock, SystemClock
from agent_runtime.sessions.claims import (
    SessionAttemptStatus,
    SessionClaimRepository,
    SessionExecutionAttempt,
)
from agent_runtime.sessions.recovery import (
    RecoveryAction,
    SessionRecoveryPlan,
    SessionRecoveryPlanner,
)

__all__ = [
    "InvalidSessionTransitionError",
    "MessageConflictError",
    "PersistedSessionMessage",
    "SessionAlreadyExistsError",
    "SessionCoordinator",
    "SessionEvent",
    "SessionEventType",
    "SessionMessageDraft",
    "SessionMessageVisibility",
    "SessionNotFoundError",
    "SessionOutcome",
    "SessionOutcomeStatus",
    "SessionRepository",
    "SessionState",
    "SessionStatus",
    "SessionTerminalError",
    "StaleSessionError",
    "SessionClaimConflictError",
    "SessionClaimNotOwnedError",
    "Clock",
    "FakeClock",
    "SystemClock",
    "SessionAttemptStatus",
    "SessionClaimRepository",
    "SessionExecutionAttempt",
    "RecoveryAction",
    "SessionRecoveryPlan",
    "SessionRecoveryPlanner",
]

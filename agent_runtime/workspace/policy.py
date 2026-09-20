"""Pure Application lifecycle rules."""

from __future__ import annotations

from datetime import datetime

from agent_runtime.workspace.errors import InvalidApplicationTransitionError
from agent_runtime.workspace.types import ApplicationStatus


class ApplicationTransitionPolicy:
    _allowed = {
        ApplicationStatus.SAVED: {ApplicationStatus.ANALYZING},
        ApplicationStatus.ANALYZING: {
            ApplicationStatus.NEEDS_EVIDENCE,
            ApplicationStatus.MATERIALS_READY,
            ApplicationStatus.ANALYSIS_FAILED,
        },
        ApplicationStatus.ANALYSIS_FAILED: {
            ApplicationStatus.ANALYZING,
            ApplicationStatus.ARCHIVED,
        },
        ApplicationStatus.NEEDS_EVIDENCE: {
            ApplicationStatus.ANALYZING,
            ApplicationStatus.MATERIALS_READY,
        },
        ApplicationStatus.MATERIALS_READY: {
            ApplicationStatus.READY_TO_APPLY,
            ApplicationStatus.ANALYZING,
        },
        ApplicationStatus.READY_TO_APPLY: {
            ApplicationStatus.APPLIED,
            ApplicationStatus.ANALYZING,
        },
        ApplicationStatus.APPLIED: {
            ApplicationStatus.INTERVIEWING,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        },
        ApplicationStatus.INTERVIEWING: {
            ApplicationStatus.OFFER,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        },
        ApplicationStatus.OFFER: {ApplicationStatus.ARCHIVED},
        ApplicationStatus.REJECTED: {ApplicationStatus.ARCHIVED},
        ApplicationStatus.WITHDRAWN: {ApplicationStatus.ARCHIVED},
        ApplicationStatus.ARCHIVED: set(),
    }

    def validate(
        self,
        current: ApplicationStatus,
        target: ApplicationStatus,
        *,
        applied_at: datetime | None = None,
    ) -> None:
        if current == target:
            if target == ApplicationStatus.APPLIED and applied_at is None:
                raise InvalidApplicationTransitionError(
                    "applied_at is required for applied applications."
                )
            return
        if target not in self._allowed[current]:
            raise InvalidApplicationTransitionError(
                f"Application cannot transition from {current.value} to {target.value}."
            )
        if target == ApplicationStatus.APPLIED and applied_at is None:
            raise InvalidApplicationTransitionError(
                "applied_at is required when entering applied."
            )

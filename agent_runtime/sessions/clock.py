"""Injectable clocks for lease calculations."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, current: datetime | None = None) -> None:
        self._current = current or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, *, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)

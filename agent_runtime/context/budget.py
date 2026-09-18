"""Conservative provider-independent token estimation."""

from __future__ import annotations

import math
from typing import Any

from agent_runtime.security import canonical_json


class ContextBudgetExceededError(ValueError):
    pass


def estimate_tokens(value: str | Any) -> int:
    """Estimate conservatively without relying on a provider tokenizer."""
    text = value if isinstance(value, str) else canonical_json(value)
    if not text:
        return 0
    # Three UTF-8 bytes per token plus a small per-block framing allowance.
    return max(1, math.ceil(len(text.encode("utf-8")) / 3) + 4)

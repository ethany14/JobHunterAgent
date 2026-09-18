from __future__ import annotations

import argparse

import pytest

from agent_runtime import AgentMessage, ToolLoopOutcome, ToolLoopStatus
from scripts.smoke_tool_loop import (
    build_parser,
    exit_code_for_outcome,
    exit_code_for_status,
)


def test_cli_argument_defaults_and_overrides():
    defaults = build_parser().parse_args(["--prompt", "Show recent runs"])
    assert defaults.prompt == "Show recent runs"
    assert defaults.session_id is None
    assert defaults.max_iterations == 6
    assert defaults.max_tool_calls == 10
    assert defaults.show_messages is False
    assert defaults.show_tool_events is False

    configured = build_parser().parse_args([
        "--prompt", "Compare runs", "--session-id", "smoke-fixed",
        "--show-messages", "--show-tool-events", "--max-iterations", "3",
        "--max-tool-calls", "4",
    ])
    assert configured.session_id == "smoke-fixed"
    assert configured.show_messages and configured.show_tool_events
    assert configured.max_iterations == 3 and configured.max_tool_calls == 4


def test_cli_rejects_missing_prompt_and_nonpositive_limits():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--prompt", "x", "--max-iterations", "0"])


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ToolLoopStatus.COMPLETED, 0),
        (ToolLoopStatus.AWAITING_APPROVAL, 2),
        (ToolLoopStatus.FAILED, 3),
        (ToolLoopStatus.LIMIT_EXCEEDED, 4),
    ],
)
def test_outcome_status_exit_codes(status, expected):
    assert exit_code_for_status(status) == expected


def test_provider_failure_uses_initialization_provider_exit_code():
    outcome = ToolLoopOutcome(
        status=ToolLoopStatus.FAILED,
        messages=[AgentMessage(role="user", content="x")],
        error_code="model_invocation_failed",
    )
    assert exit_code_for_outcome(outcome) == 5

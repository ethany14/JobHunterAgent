"""Safe serialization for data returned by tools."""
from __future__ import annotations

from agent_runtime.security import canonical_json
from agent_runtime.tools.messages import SafeToolError, ToolMessageEnvelope
from agent_runtime.types import ToolCallRecord, ToolExecutionStatus

_ERROR_CODE_MAP = {
    "unknown_tool": "tool_not_found",
    "tool_not_allowed": "tool_not_allowed",
    "invalid_arguments": "invalid_tool_arguments",
    "invalid_tool_output": "invalid_tool_output",
    "run_not_found": "run_not_found",
    "run_result_not_ready": "run_result_not_ready",
    "tool_execution_failed": "tool_execution_failed",
}


def safe_error_code(record: ToolCallRecord) -> str | None:
    if not record.error_code:
        return None
    return _ERROR_CODE_MAP.get(record.error_code, record.error_code)


def tool_message_envelope(
    *, tool_call_id: str, tool_name: str, record: ToolCallRecord
) -> ToolMessageEnvelope:
    code = safe_error_code(record)
    message = record.error_message if code else None
    return ToolMessageEnvelope(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status=record.status,
        data=record.result.output if record.result is not None else None,
        provenance=record.result.provenance if record.result is not None else [],
        safe_error=SafeToolError(code=code, message=message or "The tool failed safely.")
        if code
        else None,
        error_code=code,
        error_message=message,
    )


def serialize_tool_envelope(
    envelope: ToolMessageEnvelope, *, max_bytes: int
) -> str:
    try:
        serialized = canonical_json(envelope.model_dump(mode="json"))
    except (TypeError, ValueError):
        serialized = canonical_json(
            _serialization_failure(envelope, "invalid_tool_output").model_dump(
                mode="json"
            )
        )
    if len(serialized.encode("utf-8")) <= max_bytes:
        return serialized
    return canonical_json(
        _serialization_failure(envelope, "tool_result_too_large").model_dump(
            mode="json"
        )
    )


def _serialization_failure(
    envelope: ToolMessageEnvelope, code: str
) -> ToolMessageEnvelope:
    message = (
        "The tool result exceeded the configured size limit."
        if code == "tool_result_too_large"
        else "The tool returned data that could not be safely serialized."
    )
    return ToolMessageEnvelope(
        tool_call_id=envelope.tool_call_id,
        tool_name=envelope.tool_name,
        status=ToolExecutionStatus.FAILED,
        safe_error=SafeToolError(code=code, message=message),
        error_code=code,
        error_message=message,
    )

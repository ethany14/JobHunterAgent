"""Policy-gated tool execution with optional durable auditing."""
from __future__ import annotations

import inspect
from time import monotonic
from pydantic import ValidationError
from agent_runtime.errors import (ApprovalBindingError, ClassifiedToolError, RetryableToolError,
    ToolTimeoutError, UnknownToolError)
from agent_runtime.policy import ToolPolicy
from agent_runtime.registry import ToolRegistry
from agent_runtime.repository import ToolCallRepository
from agent_runtime.security import arguments_hash, redact_sensitive
from agent_runtime.types import (ToolCallRecord, ToolCallRequest, ToolContext,
    ToolExecutionStatus, ToolResult, ToolRiskLevel)

_WRITE_RISKS = {ToolRiskLevel.LOCAL_WRITE, ToolRiskLevel.EXTERNAL_WRITE, ToolRiskLevel.SENSITIVE}

def _tool_version(tool) -> str:
    """Keep phase-1A tools compatible while giving persisted approvals a version."""
    return str(getattr(tool, "version", "1"))

def _scope(context: ToolContext) -> tuple[str, str]:
    for name in ("task_id", "session_id", "run_id", "user_id"):
        value = getattr(context, name)
        if value:
            return name.removesuffix("_id"), value
    raise ValueError("Persisted tool calls require a task, session, run, or user scope.")


def _observability(tool) -> dict:
    provenance = getattr(tool, "mcp_provenance", None)
    payload = {"public_tool_name": tool.name, "provider": "builtin"}
    if provenance is not None:
        payload.update({"provider": "mcp", **provenance.model_dump(mode="json")})
    return payload


def _result_metadata(result: ToolResult | None, started: float) -> dict:
    output = result.output if result is not None else None
    return {
        "duration_ms": max(0, round((monotonic() - started) * 1000)),
        "result_truncated": bool(
            isinstance(output, dict) and output.get("truncated") is True
        ),
    }

class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: ToolPolicy | None = None,
                 repository: ToolCallRepository | None = None) -> None:
        self._registry = registry
        self._policy = policy or ToolPolicy()
        self._repository = repository

    @property
    def persistent(self) -> bool:
        return self._repository is not None

    def approve(self, call_id: str, *, expected_version: int) -> ToolCallRecord:
        if self._repository is None:
            raise ValueError("Persisted approval requires a repository.")
        record = self._repository.require(call_id)
        tool = self._registry.get(record.request.tool_name)
        return self._repository.approve(call_id, expected_version=expected_version,
            tool_name=tool.name, tool_version=_tool_version(tool),
            arguments_hash=record.arguments_hash or "")

    def reject(self, call_id: str, *, expected_version: int) -> ToolCallRecord:
        if self._repository is None:
            raise ValueError("Persisted rejection requires a repository.")
        return self._repository.reject(call_id, expected_version=expected_version)

    def execute(self, request: ToolCallRequest, context: ToolContext) -> ToolCallRecord:
        try:
            tool = self._registry.get(request.tool_name)
        except UnknownToolError:
            return ToolCallRecord(request=request, status=ToolExecutionStatus.DENIED,
                error_code="unknown_tool", error_message="The requested tool is not available.")
        try:
            validated = tool.input_schema.model_validate(request.arguments)
        except ValidationError:
            return ToolCallRecord(request=request, status=ToolExecutionStatus.FAILED,
                tool_version=_tool_version(tool), risk_level=tool.risk_level,
                error_code="invalid_arguments", error_message="The tool arguments are invalid.")
        canonical_arguments = validated.model_dump(mode="json")
        effective_timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else getattr(tool, "timeout_seconds", None)
        )
        effective_request = ToolCallRequest.model_validate(
            {**request.model_dump(), "timeout_seconds": effective_timeout}
        )
        digest = arguments_hash(canonical_arguments)
        if self._repository is None:
            return self._execute_transient(tool, validated, effective_request, context, digest)
        scope_type, scope_id = _scope(context)
        record, created = self._repository.get_or_create(request=ToolCallRequest(
                **effective_request.model_dump(exclude={"arguments"}), arguments=canonical_arguments),
            tool_version=_tool_version(tool), risk_level=tool.risk_level,
            scope_type=scope_type, scope_id=scope_id, arguments_hash=digest,
            redacted_arguments=redact_sensitive(canonical_arguments),
            side_effect=getattr(tool, "side_effect", None),
            idempotent=getattr(tool, "idempotent", None),
            event_payload=_observability(tool))

        if not created:
            if record.status in {
                    ToolExecutionStatus.RUNNING,
                    ToolExecutionStatus.APPROVAL_REQUIRED,
            }:
                # Do not mutate an in-flight or approval-pending record: doing so
                # would invalidate the owner's optimistic version.
                return record
            if record.status in {ToolExecutionStatus.COMPLETED, ToolExecutionStatus.OUTCOME_UNKNOWN,
                    ToolExecutionStatus.TIMED_OUT, ToolExecutionStatus.DENIED}:
                return self._repository.record_idempotent_reuse(
                    record.call_id, expected_version=record.version
                )
            if record.status == ToolExecutionStatus.FAILED and not (
                    request.retry_failed and record.retryable
                    and record.attempt_count < record.request.max_attempts):
                return self._repository.record_idempotent_reuse(
                    record.call_id, expected_version=record.version
                )
            if record.status == ToolExecutionStatus.APPROVED and not self._approval_valid(record, tool, digest):
                raise ApprovalBindingError("Persisted approval no longer matches the tool version and arguments.")

        approved = record.status == ToolExecutionStatus.APPROVED or (
            record.approval_tool_name == tool.name
            and record.approval_tool_version == _tool_version(tool)
            and record.approval_arguments_hash == digest)
        decision = self._policy.evaluate(tool, context, approval_granted=approved)
        if not decision.allowed and not decision.approval_required:
            return self._repository.transition(record.call_id, expected_version=record.version,
                status=ToolExecutionStatus.DENIED, event_type="denied",
                error_code="tool_not_allowed", error_message=decision.reason)
        if decision.approval_required:
            return self._repository.transition(record.call_id, expected_version=record.version,
                status=ToolExecutionStatus.APPROVAL_REQUIRED, event_type="approval_required",
                error_code="approval_required", error_message=decision.reason)
        running = self._repository.transition(record.call_id, expected_version=record.version,
            status=ToolExecutionStatus.RUNNING, event_type="execution_started",
            increment_attempt=True, execution_attempt_id=context.attempt_id,
            execution_lease_until=context.lease_until,
            payload={**_observability(tool), "approval_status": (
                "approved" if approved else "not_required"
            )})
        started = monotonic()
        try:
            result = self._validate_result(tool, self._invoke(
                tool, validated, context, effective_timeout
            ))
        except ValidationError:
            return self._repository.transition(running.call_id, expected_version=running.version,
                status=ToolExecutionStatus.FAILED, event_type="failed",
                error_code="invalid_tool_output",
                error_message="The tool returned an invalid result.", retryable=False,
                payload=_result_metadata(None, started))
        except ToolTimeoutError:
            status = ToolExecutionStatus.TIMED_OUT if tool.risk_level == ToolRiskLevel.READ_ONLY else ToolExecutionStatus.OUTCOME_UNKNOWN
            return self._repository.transition(running.call_id, expected_version=running.version,
                status=status, event_type=status.value, error_code=status.value,
                error_message="The tool did not report completion before its timeout.",
                payload=_result_metadata(None, started))
        except ClassifiedToolError as exc:
            return self._repository.transition(
                running.call_id,
                expected_version=running.version,
                status=ToolExecutionStatus.FAILED,
                event_type="failed",
                error_code=exc.error_code,
                error_message=exc.safe_message,
                retryable=exc.retryable,
                payload=_result_metadata(None, started),
            )
        except Exception as exc:
            return self._repository.transition(running.call_id, expected_version=running.version,
                status=ToolExecutionStatus.FAILED, event_type="failed",
                error_code="tool_execution_failed", error_message="The tool could not complete the request.",
                retryable=isinstance(exc, RetryableToolError),
                payload=_result_metadata(None, started))
        if result.is_error:
            return self._repository.transition(
                running.call_id,
                expected_version=running.version,
                status=ToolExecutionStatus.FAILED,
                event_type="tool_reported_error",
                result=result,
                error_code=result.error_code or "tool_reported_error",
                error_message="The tool reported that it could not complete the request.",
                retryable=False,
                payload=_result_metadata(result, started),
            )
        return self._repository.transition(running.call_id, expected_version=running.version,
            status=ToolExecutionStatus.COMPLETED, event_type="completed", result=result,
            payload=_result_metadata(result, started))

    @staticmethod
    def _approval_valid(record, tool, digest: str) -> bool:
        return (record.approval_tool_name == tool.name
            and record.approval_tool_version == _tool_version(tool)
            and record.approval_arguments_hash == digest)

    @staticmethod
    def _invoke(tool, arguments, context, timeout_seconds):
        signature = inspect.signature(tool.execute)
        supports_timeout = "timeout_seconds" in signature.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if supports_timeout:
            return tool.execute(arguments, context, timeout_seconds=timeout_seconds)
        return tool.execute(arguments, context)

    @staticmethod
    def _validate_result(tool, value) -> ToolResult:
        result = ToolResult.model_validate(value)
        output_schema = getattr(tool, "output_schema", None)
        if output_schema is None:
            return result
        output = output_schema.model_validate(result.output).model_dump(mode="json")
        return ToolResult(
            output=output,
            provenance=result.provenance,
            is_error=result.is_error,
            error_code=result.error_code,
        )

    def _execute_transient(self, tool, arguments, request, context, digest):
        # A request boolean is not an auditable approval. Without a repository,
        # only read-only tools can execute.
        decision = self._policy.evaluate(tool, context, approval_granted=False)
        if not decision.allowed:
            status = ToolExecutionStatus.APPROVAL_REQUIRED if decision.approval_required else ToolExecutionStatus.DENIED
            return ToolCallRecord(request=request, status=status, tool_version=_tool_version(tool),
                risk_level=tool.risk_level, arguments_hash=digest,
                error_code="approval_required" if decision.approval_required else "tool_not_allowed",
                error_message=decision.reason)
        try:
            result = self._validate_result(
                tool, self._invoke(tool, arguments, context, request.timeout_seconds)
            )
        except ValidationError:
            return ToolCallRecord(request=request, status=ToolExecutionStatus.FAILED,
                tool_version=_tool_version(tool), risk_level=tool.risk_level,
                arguments_hash=digest, error_code="invalid_tool_output",
                error_message="The tool returned an invalid result.")
        except ToolTimeoutError:
            status = ToolExecutionStatus.TIMED_OUT if tool.risk_level == ToolRiskLevel.READ_ONLY else ToolExecutionStatus.OUTCOME_UNKNOWN
            return ToolCallRecord(request=request, status=status, tool_version=_tool_version(tool),
                risk_level=tool.risk_level, arguments_hash=digest,
                error_code=status.value, error_message="The tool did not report completion before its timeout.")
        except ClassifiedToolError as exc:
            return ToolCallRecord(
                request=request,
                status=ToolExecutionStatus.FAILED,
                tool_version=_tool_version(tool),
                risk_level=tool.risk_level,
                arguments_hash=digest,
                error_code=exc.error_code,
                error_message=exc.safe_message,
                retryable=exc.retryable,
            )
        except Exception:
            return ToolCallRecord(request=request, status=ToolExecutionStatus.FAILED,
                tool_version=_tool_version(tool), risk_level=tool.risk_level,
                arguments_hash=digest, error_code="tool_execution_failed",
                error_message="The tool could not complete the request.")
        if result.is_error:
            return ToolCallRecord(
                request=request,
                status=ToolExecutionStatus.FAILED,
                tool_version=_tool_version(tool),
                risk_level=tool.risk_level,
                arguments_hash=digest,
                result=result,
                error_code=result.error_code or "tool_reported_error",
                error_message="The tool reported that it could not complete the request.",
                attempt_count=1,
            )
        return ToolCallRecord(request=request, status=ToolExecutionStatus.COMPLETED,
            tool_version=_tool_version(tool), risk_level=tool.risk_level,
            arguments_hash=digest, result=result, attempt_count=1)

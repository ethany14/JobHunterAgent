"""Domain errors raised at tool-runtime boundaries."""
class ToolRuntimeError(Exception): pass
class DuplicateToolError(ToolRuntimeError): pass
class UnknownToolError(ToolRuntimeError): pass
class ToolCallNotFoundError(ToolRuntimeError): pass
class StaleToolCallError(ToolRuntimeError): pass
class IdempotencyConflictError(ToolRuntimeError): pass
class ApprovalBindingError(ToolRuntimeError): pass
class InvalidToolCallTransitionError(ToolRuntimeError): pass
class ToolTimeoutError(ToolRuntimeError): pass
class RetryableToolError(ToolRuntimeError): pass
class ToolModelError(ToolRuntimeError): pass


class ClassifiedToolError(ToolRuntimeError):
    """A runtime failure with a safe, stable persistence classification."""

    error_code = "tool_execution_failed"
    safe_message = "The tool could not complete the request."
    retryable = False

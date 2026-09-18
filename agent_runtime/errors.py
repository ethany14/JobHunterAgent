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

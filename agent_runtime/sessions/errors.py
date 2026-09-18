"""Domain errors raised at session persistence boundaries."""


class SessionRuntimeError(RuntimeError):
    pass


class SessionNotFoundError(SessionRuntimeError):
    pass


class SessionAlreadyExistsError(SessionRuntimeError):
    pass


class StaleSessionError(SessionRuntimeError):
    pass


class MessageConflictError(SessionRuntimeError):
    pass


class InvalidSessionTransitionError(SessionRuntimeError):
    pass


class SessionTerminalError(InvalidSessionTransitionError):
    pass


class SessionClaimConflictError(SessionRuntimeError):
    pass


class SessionClaimNotOwnedError(SessionRuntimeError):
    pass

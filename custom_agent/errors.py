"""Domain errors raised by the custom agent runtime."""


class AgentLoopError(RuntimeError):
    pass


class StateNotFoundError(AgentLoopError):
    pass


class StaleStateError(AgentLoopError):
    pass


class InvalidTransitionError(AgentLoopError):
    pass

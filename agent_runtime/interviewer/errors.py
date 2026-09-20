class InterviewNotFoundError(Exception):
    pass


class InterviewConflictError(Exception):
    pass


class InterviewValidationError(Exception):
    pass


class InterviewStaleVersionError(Exception):
    pass

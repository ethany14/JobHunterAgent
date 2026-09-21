"""Stable public failures without source text or provider exceptions."""

class MockInterviewError(RuntimeError):
    pass

class MockInterviewNotFound(MockInterviewError):
    pass

class MockInterviewConflict(MockInterviewError):
    pass

class MockInterviewValidation(MockInterviewError):
    pass

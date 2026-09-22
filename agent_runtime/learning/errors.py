"""Stable conversation-learning domain errors."""


class ConversationLearningError(Exception):
    pass


class LearningExperienceConflictError(ConversationLearningError):
    pass


class LearningExperienceNotFoundError(ConversationLearningError):
    pass


class WorkspaceError(Exception):
    pass


class JobNotFoundError(WorkspaceError):
    pass


class SnapshotNotFoundError(WorkspaceError):
    pass


class ApplicationNotFoundError(WorkspaceError):
    pass


class ArtifactNotFoundError(WorkspaceError):
    pass


class StaleApplicationError(WorkspaceError):
    pass


class InvalidApplicationTransitionError(WorkspaceError):
    pass


class WorkspaceAssociationError(WorkspaceError):
    pass


class ArtifactValidationError(WorkspaceError):
    pass

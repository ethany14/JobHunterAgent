"""Safe domain errors for Skill Runtime."""


class SkillRuntimeError(RuntimeError):
    code = "skill_runtime_error"


class SkillParseError(SkillRuntimeError):
    code = "skill_parse_error"


class SkillValidationError(SkillRuntimeError):
    code = "skill_validation_failed"

    def __init__(self, errors: list[str], warnings: list[str] | None = None) -> None:
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("Skill validation failed: " + "; ".join(errors))


class SkillSecurityError(SkillRuntimeError):
    code = "skill_security_error"


class SkillNotFoundError(SkillRuntimeError):
    code = "skill_not_found"


class SkillVersionConflictError(SkillRuntimeError):
    code = "skill_version_conflict"


class StaleSkillVersionError(SkillRuntimeError):
    code = "stale_skill_version"


class InvalidSkillTransitionError(SkillRuntimeError):
    code = "invalid_skill_transition"


class SkillContentChangedError(SkillRuntimeError):
    code = "skill_content_changed"


class SkillResourceError(SkillRuntimeError):
    code = "skill_resource_error"


class SkillScriptExecutionDisabledError(SkillRuntimeError):
    code = "skill_script_execution_disabled"

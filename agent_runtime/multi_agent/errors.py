"""Stable, safe task runtime failures."""

class TaskRuntimeError(RuntimeError):
    code = "task_runtime_error"

class TaskNotFoundError(TaskRuntimeError):
    code = "task_not_found"

class TaskConflictError(TaskRuntimeError):
    code = "task_conflict"

class InvalidPlanError(TaskRuntimeError):
    code = "invalid_plan"

class UnknownWorkerError(InvalidPlanError):
    code = "unknown_worker"

class LeaseConflictError(TaskConflictError):
    code = "task_lease_conflict"

class BudgetExceededError(TaskRuntimeError):
    code = "budget_exceeded"

class ContextBudgetExceededError(BudgetExceededError):
    code = "context_budget_exceeded"

class WorkerStepError(TaskRuntimeError):
    """Safe, fixed failure code for a worker stage; never includes source text."""

    def __init__(self, code: str) -> None:
        if code not in {
            "resume_analysis_failed", "resume_evidence_invalid",
            "evidence_import_failed", "job_analysis_failed",
        }:
            raise ValueError("Unknown worker failure code.")
        self.code = code
        super().__init__(code)

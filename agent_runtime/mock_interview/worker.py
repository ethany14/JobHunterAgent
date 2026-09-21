"""Narrow Multi-Agent adapter around the persisted mock interview controller."""
from __future__ import annotations

from agent_runtime.mock_interview.controller import MockInterviewController
from agent_runtime.mock_interview.types import Difficulty, InterviewMode
from agent_runtime.multi_agent.types import AgentTask, AgentTaskResult, ExecutionContext, OutputArtifact


class MockInterviewerWorker:
    task_type = "mock_interviewer"

    def __init__(self, controller: MockInterviewController) -> None:
        self._controller = controller

    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult:
        if not task.application_id or task.allowed_tools or context.allowed_tools:
            raise ValueError("Mock interview requires one Application and no runtime tools.")
        previous = (task.result_summary or {}).get("pause_metadata", {})
        interview_id = previous.get("mock_interview_id")
        if interview_id:
            view = self._controller.view(interview_id)
            if view["interview"]["application_id"] != task.application_id:
                raise ValueError("Interview belongs to another Application.")
        else:
            state = self._controller.start(task.application_id,
                mode=InterviewMode(task.input_spec.get("mode", "mixed")),
                difficulty=Difficulty(task.input_spec.get("difficulty", "standard")),
                target_question_count=int(task.input_spec.get("target_question_count", 5)),
                idempotency_key=f"task:{task.task_id}", root_task_id=task.task_id)
            interview_id = state.mock_interview_id
            view = self._controller.view(interview_id)
        status = view["interview"]["status"]
        if status == "completed":
            return AgentTaskResult(summary="Mock interview completed.", output_artifacts=[
                OutputArtifact(role="interview_report_reference", content={
                    "kind": "interview_report_reference",
                    "data": {"mock_interview_id": interview_id}})])
        if status == "awaiting_answer":
            return AgentTaskResult(summary="Mock interview needs an answer.",
                awaiting_input=True, pause_metadata={"mock_interview_id": interview_id})
        if status == "cancelled":
            return AgentTaskResult(summary="Mock interview cancelled by user.")
        raise ValueError("Mock interview is not ready to resume.")

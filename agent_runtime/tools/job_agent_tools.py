"""Read-only built-in tools over persisted Job Agent public results."""
from __future__ import annotations

from collections import Counter

from agent_runtime.registry import ToolRegistry
from agent_runtime.tools.run_reader import RunReader
from agent_runtime.tools.schemas import (
    CompareRunRequirementsInput, CompareRunRequirementsOutput,
    GetRunResultInput, ListRecentRunsInput, ListRecentRunsOutput,
    PublicRunResult, RecentRunSummary, RenderTailoredResumeInput,
    RenderTailoredResumeOutput, RunRequirementComparison,
)
from agent_runtime.types import (ToolContext, ToolDataClassification,
    ToolProvenance, ToolResult, ToolRiskLevel, ToolSideEffect)
from job_agent.rendering import render_tailored_resume_markdown, render_tailored_resume_text


def _provenance(source_id: str) -> ToolProvenance:
    return ToolProvenance(source_type="internal_database", source_id=source_id,
        metadata={"resource": "runs"})


class _ReadOnlyBuiltin:
    version = "1.0"
    risk_level = ToolRiskLevel.READ_ONLY
    side_effect = ToolSideEffect.NONE
    data_classification = ToolDataClassification.SENSITIVE
    timeout_seconds = 5.0
    idempotent = True

    def __init__(self, reader: RunReader) -> None:
        self._reader = reader


class ListRecentRunsTool(_ReadOnlyBuiltin):
    name = "list_recent_runs"
    description = "List safe summaries of recent local Job Agent runs."
    input_schema = ListRecentRunsInput
    output_schema = ListRecentRunsOutput

    def execute(self, arguments: ListRecentRunsInput, context: ToolContext, *, timeout_seconds=None) -> ToolResult:
        runs = []
        for run in self._reader.list_recent_runs(arguments.limit):
            result = run.result
            runs.append(RecentRunSummary(run_id=run.run_id, status=run.status,
                job_title=result.job_analysis.title if result else None,
                match_score=result.skill_match.overall_score if result else None,
                missing_required_count=(len(result.skill_match.missing_required_requirements) if result else None),
                created_at=run.created_at, updated_at=run.updated_at))
        return ToolResult(output=ListRecentRunsOutput(runs=runs).model_dump(mode="json"),
            provenance=[_provenance("recent-runs")])


class GetRunResultTool(_ReadOnlyBuiltin):
    name = "get_run_result"
    description = "Get the existing public result projection for one local run."
    input_schema = GetRunResultInput
    output_schema = PublicRunResult

    def execute(self, arguments: GetRunResultInput, context: ToolContext, *, timeout_seconds=None) -> ToolResult:
        run = self._reader.get_run(arguments.run_id)
        if run is None or run.result is None:
            raise LookupError("Run result is unavailable.")
        return ToolResult(output=run.result.model_dump(mode="json"),
            provenance=[_provenance(run.run_id)])


class CompareRunRequirementsTool(_ReadOnlyBuiltin):
    name = "compare_run_requirements"
    description = "Deterministically compare canonical requirements across local runs."
    input_schema = CompareRunRequirementsInput
    output_schema = CompareRunRequirementsOutput

    def execute(self, arguments: CompareRunRequirementsInput, context: ToolContext, *, timeout_seconds=None) -> ToolResult:
        runs = []
        for run_id in arguments.run_ids:
            run = self._reader.get_run(run_id)
            if run is None or run.result is None:
                raise LookupError("A requested run result is unavailable.")
            runs.append(run)

        required_sets: list[set[str]] = []
        missing_sets: list[set[str]] = []
        partial_sets: list[set[str]] = []
        matched_sets: list[set[str]] = []
        confirmation_sets: list[set[str]] = []
        for run in runs:
            result = run.result
            requirements = {item.requirement_id: item for item in result.job_analysis.requirements}
            required = {item.canonical_name.strip().casefold() for item in requirements.values() if item.level == "required"}
            missing: set[str] = set(); partial: set[str] = set()
            matched: set[str] = set(); confirmation: set[str] = set()
            for match in result.skill_match.matches:
                requirement = requirements.get(match.requirement_id)
                if requirement is None:
                    continue
                name = requirement.canonical_name.strip().casefold()
                if match.match_status == "matched": matched.add(name)
                elif match.match_status == "needs_confirmation": confirmation.add(name)
                elif requirement.level == "required" and match.match_status == "partial": partial.add(name)
                elif requirement.level == "required" and match.match_status == "missing": missing.add(name)
            required_sets.append(required); missing_sets.append(missing)
            partial_sets.append(partial); matched_sets.append(matched)
            confirmation_sets.append(confirmation)

        common_required = set.intersection(*required_sets)
        common_missing = set.intersection(*missing_sets)
        common_partial = set.intersection(*partial_sets)
        matched_counts = Counter(name for values in matched_sets for name in values)
        comparisons = []
        for index, run in enumerate(runs):
            other_missing = set().union(*(values for i, values in enumerate(missing_sets) if i != index))
            comparisons.append(RunRequirementComparison(run_id=run.run_id,
                score=run.result.skill_match.overall_score,
                unique_missing_requirements=sorted(missing_sets[index] - other_missing),
                partial_requirements=sorted(partial_sets[index]),
                confirmation_requirements=sorted(confirmation_sets[index])))
        output = CompareRunRequirementsOutput(
            common_required_requirements=sorted(common_required),
            common_missing_requirements=sorted(common_missing),
            common_partial_requirements=sorted(common_partial),
            recurring_matched_requirements=sorted(name for name, count in matched_counts.items() if count >= 2),
            runs=comparisons)
        return ToolResult(output=output.model_dump(mode="json"),
            provenance=[_provenance(run.run_id) for run in runs])


class RenderTailoredResumeTool(_ReadOnlyBuiltin):
    name = "render_tailored_resume"
    description = "Render an existing grounded tailored resume as text or Markdown."
    input_schema = RenderTailoredResumeInput
    output_schema = RenderTailoredResumeOutput

    def execute(self, arguments: RenderTailoredResumeInput, context: ToolContext, *, timeout_seconds=None) -> ToolResult:
        run = self._reader.get_run(arguments.run_id)
        if run is None or run.result is None:
            raise LookupError("Run result is unavailable.")
        renderer = render_tailored_resume_markdown if arguments.format == "markdown" else render_tailored_resume_text
        output = RenderTailoredResumeOutput(run_id=run.run_id, format=arguments.format,
            content=renderer(run.result.tailored_resume))
        return ToolResult(output=output.model_dump(mode="json"), provenance=[_provenance(run.run_id)])


def register_builtin_job_agent_tools(registry: ToolRegistry, run_reader: RunReader) -> ToolRegistry:
    """Register built-ins into the caller-owned registry."""
    for tool_type in (ListRecentRunsTool, GetRunResultTool,
                      CompareRunRequirementsTool, RenderTailoredResumeTool):
        registry.register(tool_type(run_reader))
    return registry

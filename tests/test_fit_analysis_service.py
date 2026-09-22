from custom_agent.state import Step
from custom_agent.steps import StepOutcome
from api.services.fit_analysis_service import FitAnalysisService
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillMatch


class AnalysisOnlyHandler:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def execute(self, step: Step, state) -> StepOutcome:
        self.steps.append(step)
        if step == Step.VALIDATE_INPUT:
            if not state.resume_text.strip() or not state.job_description.strip():
                raise ValueError("invalid input")
            return StepOutcome()
        if step == Step.ANALYZE_RESUME:
            return StepOutcome(updates={"resume_analysis": ResumeAnalysis(
                summary="Python developer", skills=["Python"], evidence=[], education=[]
            )})
        if step == Step.VALIDATE_EVIDENCE:
            return StepOutcome()
        if step == Step.ANALYZE_JOB:
            return StepOutcome(updates={"job_analysis": JobAnalysis(
                title="Backend Engineer", summary="Python", requirements=[],
                responsibilities=[]
            )})
        if step == Step.MATCH_SKILLS:
            return StepOutcome(updates={"skill_match": SkillMatch.model_validate({
                "matches": [], "explanation": "Deterministic match",
                "recommendations": [], "missing_required_requirements": [],
                "missing_preferred_requirements": [], "overall_score": 100,
                "score_breakdown": {"overall_score": 100},
                "confirmation_requirements": [],
            })})
        raise AssertionError(f"Fit analysis called forbidden step {step.value}")


def test_fit_analysis_stops_before_writer_and_verifier():
    handler = AnalysisOnlyHandler()
    ticks = iter([10.0, 10.25])
    result = FitAnalysisService(handler, timer=lambda: next(ticks))._analyze_sync(
        resume_text="Python developer",
        job_description="Requires Python",
    )
    assert handler.steps == [
        Step.VALIDATE_INPUT,
        Step.ANALYZE_RESUME,
        Step.VALIDATE_EVIDENCE,
        Step.ANALYZE_JOB,
        Step.MATCH_SKILLS,
    ]
    assert result.status == "completed"
    assert result.skill_match.overall_score == 100
    assert result.model_calls == 3
    assert result.latency_seconds == .25


def test_writer_failure_cannot_affect_fit_analysis():
    class WriterBombHandler(AnalysisOnlyHandler):
        def execute(self, step, state):
            if step in {Step.WRITE_RESUME, Step.VERIFY_RESUME}:
                raise RuntimeError("writer/verifier unavailable")
            return super().execute(step, state)

    result = FitAnalysisService(WriterBombHandler())._analyze_sync(
        resume_text="Python developer",
        job_description="Requires Python",
    )
    assert result.skill_match.overall_score == 100

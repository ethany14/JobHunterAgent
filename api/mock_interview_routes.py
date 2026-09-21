"""Synchronous endpoints: model calls execute in FastAPI's threadpool."""
from fastapi import APIRouter, Depends

from api.mock_interview_schemas import MockAnswerRequest, MockMutation, StartMockInterviewRequest
from api.session_dependencies import SessionRuntime, get_session_runtime
from api.evidence_routes import _public as public_evidence

router = APIRouter(prefix="/api", tags=["mock-interview"])


def _controller(runtime: SessionRuntime):
    if runtime.mock_interviewer is None:
        raise RuntimeError("Mock interviewer is unavailable.")
    return runtime.mock_interviewer


@router.post("/applications/{application_id}/mock-interviews")
def start_mock_interview(application_id: str, body: StartMockInterviewRequest,
                         runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    state = controller.start(application_id, **body.model_dump())
    return controller.view(state.mock_interview_id)


@router.get("/applications/{application_id}/mock-interviews/active")
def active_mock_interview(application_id: str,
                          runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    record = (controller.interviews.active(application_id)
              or controller.interviews.latest(application_id))
    return controller.view(record.mock_interview_id) if record else {"interview": None}


@router.get("/mock-interviews/{mock_interview_id}")
def get_mock_interview(mock_interview_id: str,
                       runtime: SessionRuntime = Depends(get_session_runtime)):
    return _controller(runtime).view(mock_interview_id)


@router.get("/mock-interviews/{mock_interview_id}/plan")
def get_mock_plan(mock_interview_id: str,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    return _controller(runtime).interviews.plan(mock_interview_id).model_dump(mode="json")


@router.get("/mock-interviews/{mock_interview_id}/turns")
def get_mock_turns(mock_interview_id: str,
                   runtime: SessionRuntime = Depends(get_session_runtime)):
    return {"turns": _controller(runtime).interviews.turns(mock_interview_id)}


@router.post("/mock-interviews/{mock_interview_id}/answers")
def answer_mock(mock_interview_id: str, body: MockAnswerRequest,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.answer(mock_interview_id, body.answer,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key)
    return controller.view(mock_interview_id)


def _mutation(mock_interview_id: str, body: MockMutation, runtime: SessionRuntime,
              method: str):
    controller = _controller(runtime)
    getattr(controller, method)(mock_interview_id,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key)
    return controller.view(mock_interview_id)


@router.post("/mock-interviews/{mock_interview_id}/skip")
def skip_mock(mock_interview_id: str, body: MockMutation,
              runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutation(mock_interview_id, body, runtime, "skip")


@router.post("/mock-interviews/{mock_interview_id}/end")
def end_mock(mock_interview_id: str, body: MockMutation,
             runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutation(mock_interview_id, body, runtime, "end")


@router.post("/mock-interviews/{mock_interview_id}/cancel")
def cancel_mock(mock_interview_id: str, body: MockMutation,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutation(mock_interview_id, body, runtime, "cancel")


@router.post("/mock-interviews/{mock_interview_id}/resume")
def resume_mock(mock_interview_id: str, body: MockMutation,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    return _mutation(mock_interview_id, body, runtime, "resume")


@router.get("/mock-interviews/{mock_interview_id}/report")
def get_mock_report(mock_interview_id: str,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.interviews.get(mock_interview_id)
    return {"report": controller.interviews.report(mock_interview_id)}


@router.get("/mock-interviews/{mock_interview_id}/evidence-candidates")
def get_mock_candidates(mock_interview_id: str,
                        runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    ids = controller.interviews.candidate_ids(mock_interview_id)
    return {"evidence_candidate_ids": ids,
            "items": [public_evidence(controller.evidence.get(evidence_id)) for evidence_id in ids]}

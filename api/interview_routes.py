"""Synchronous local Interviewer APIs; all model calls run off the event loop."""
from fastapi import APIRouter, Depends

from api.interview_schemas import (
    AnswerRequest, ConfirmCandidateRequest, InterviewMutation, StartInterviewRequest,
)
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api", tags=["interviewer"])


def _controller(runtime: SessionRuntime):
    if runtime.interviewer is None:
        raise RuntimeError("Interviewer is unavailable.")
    return runtime.interviewer


@router.post("/applications/{application_id}/interviews")
def start_interview(application_id: str, body: StartInterviewRequest,
                    runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    result = controller.start(application_id, **body.model_dump())
    return controller.view(result.interview_session_id)


@router.get("/applications/{application_id}/interviews/active")
def active_interview(application_id: str,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    record = controller._interviews.active_for_application(application_id)
    return controller.view(record.interview_session_id) if record else {"interview": None}


@router.get("/interviews/{interview_session_id}")
def get_interview(interview_session_id: str,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    return _controller(runtime).view(interview_session_id)


@router.post("/interviews/{interview_session_id}/answers")
def submit_answer(interview_session_id: str, body: AnswerRequest,
                  runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.answer(interview_session_id, body.answer, expected_version=body.expected_version,
                      idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/skip")
def skip_requirement(interview_session_id: str, body: InterviewMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.skip(interview_session_id, expected_version=body.expected_version,
                    idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/confirm-gap")
def confirm_gap(interview_session_id: str, body: InterviewMutation,
                runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.confirm_gap(interview_session_id, expected_version=body.expected_version,
                           idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/candidates/{evidence_id}/confirm")
def confirm_candidate(interview_session_id: str, evidence_id: str,
                      body: ConfirmCandidateRequest,
                      runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.confirm_candidate(interview_session_id, evidence_id,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key,
        edited_claim=body.edited_claim)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/candidates/{evidence_id}/reject")
def reject_candidate(interview_session_id: str, evidence_id: str,
                     body: InterviewMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.reject_candidate(interview_session_id, evidence_id,
        expected_version=body.expected_version, idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/cancel")
def cancel_interview(interview_session_id: str, body: InterviewMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.cancel(interview_session_id, expected_version=body.expected_version,
                      idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)


@router.post("/interviews/{interview_session_id}/resume")
def resume_interview(interview_session_id: str, body: InterviewMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    controller = _controller(runtime)
    controller.resume(interview_session_id, expected_version=body.expected_version,
                      idempotency_key=body.idempotency_key)
    return controller.view(interview_session_id)

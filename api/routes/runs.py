"""Run creation, lookup, and human-review routes."""

from fastapi import APIRouter, Depends, HTTPException, status

from api.schemas.runs import CreateRunRequest, CreateRunResponse, ReviewRequest, RunResponse
from api.services.run_service import InvalidRunStateError, RunNotFoundError, RunService

router = APIRouter(prefix="/runs", tags=["runs"])
_run_service = RunService()


def get_run_service() -> RunService:
    return _run_service


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RunNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, InvalidRunStateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    request: CreateRunRequest,
    service: RunService = Depends(get_run_service),
) -> CreateRunResponse:
    return await service.create_run(request)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    service: RunService = Depends(get_run_service),
) -> RunResponse:
    try:
        return await service.get_run(run_id)
    except (RunNotFoundError, InvalidRunStateError) as exc:
        raise _http_error(exc) from exc


@router.post("/{run_id}/review", response_model=RunResponse)
async def review_run(
    run_id: str,
    request: ReviewRequest,
    service: RunService = Depends(get_run_service),
) -> RunResponse:
    try:
        return await service.review_run(run_id, request)
    except (RunNotFoundError, InvalidRunStateError) as exc:
        raise _http_error(exc) from exc

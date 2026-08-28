"""第四阶段反馈闭环 API。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import FeedbackEvidence, FeedbackRun
from app.schemas.feedback import (
    FeedbackCandidateRead,
    FeedbackConfirmRequest,
    FeedbackEvidenceRead,
    FeedbackRejectRequest,
    FeedbackRetryRequest,
    FeedbackRunCreateRequest,
    FeedbackRunRead,
)
from app.services.feedback_agent import DeepSeekFeedbackAgent, FeedbackAgent
from app.services.feedback_service import FeedbackService, FeedbackServiceError

router = APIRouter(prefix="/api/v1")


def get_feedback_agent() -> FeedbackAgent:
    return DeepSeekFeedbackAgent()


def get_feedback_service(
    db: Session = Depends(get_db),
    agent: FeedbackAgent = Depends(get_feedback_agent),
) -> FeedbackService:
    return FeedbackService(db, agent=agent)


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _error(exc: FeedbackServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "details": exc.detail},
    )


def feedback_payload(row: FeedbackRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "output_id": row.output_id,
        "primary_metric_id": row.primary_metric_id,
        "task_id": row.task_id,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "snapshot_hash": row.snapshot_hash,
        "analysis": _decode(row.analysis_json, {}),
        "summary": row.summary,
        "prompt_version": row.prompt_version,
        "model_name": row.model_name,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "applied_at": row.applied_at,
        "rejected_at": row.rejected_at,
    }


def evidence_payload(row: FeedbackEvidence) -> dict[str, Any]:
    return {
        "id": row.id,
        "feedback_run_id": row.feedback_run_id,
        "evidence_type": row.evidence_type,
        "ref_id": row.ref_id,
        "claim": row.claim,
        "snapshot": _decode(row.snapshot_json, {}),
    }


@router.post("/bloggers/{blogger_id}/feedback-runs", response_model=FeedbackRunRead)
def create_feedback_run(
    blogger_id: int,
    body: FeedbackRunCreateRequest,
    service: FeedbackService = Depends(get_feedback_service),
) -> dict[str, Any]:
    try:
        row = service.start(
            blogger_id,
            body.output_id,
            body.primary_metric_id,
            body.idempotency_key,
            user_instruction=body.user_instruction,
        )
        if row.status == "pending":
            row = service.analyze(blogger_id, row.id)
        return feedback_payload(row)
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/feedback-runs", response_model=list[FeedbackRunRead])
def list_feedback_runs(
    blogger_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
    service: FeedbackService = Depends(get_feedback_service),
) -> list[dict[str, Any]]:
    try:
        rows = service.list_runs(blogger_id, limit=page_size, offset=(page - 1) * page_size)
        return [feedback_payload(row) for row in rows]
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


# 静态子资源必须先于详情动态路由注册，避免被 run_id 吞噬。
@router.get(
    "/bloggers/{blogger_id}/feedback-runs/{run_id}/evidence",
    response_model=list[FeedbackEvidenceRead],
)
def get_feedback_evidence(
    blogger_id: int,
    run_id: int,
    service: FeedbackService = Depends(get_feedback_service),
) -> list[dict[str, Any]]:
    try:
        return [evidence_payload(row) for row in service.get_evidence(blogger_id, run_id)]
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.get(
    "/bloggers/{blogger_id}/feedback-runs/{run_id}/candidates",
    response_model=list[FeedbackCandidateRead],
)
def get_feedback_candidates(
    blogger_id: int,
    run_id: int,
    service: FeedbackService = Depends(get_feedback_service),
) -> list[dict[str, Any]]:
    try:
        return service.get_candidates(blogger_id, run_id)
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/feedback-runs/{run_id}", response_model=FeedbackRunRead)
def get_feedback_run(
    blogger_id: int,
    run_id: int,
    service: FeedbackService = Depends(get_feedback_service),
) -> dict[str, Any]:
    try:
        return feedback_payload(service.get(blogger_id, run_id))
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.post(
    "/bloggers/{blogger_id}/feedback-runs/{run_id}/retry",
    response_model=FeedbackRunRead,
)
def retry_feedback_run(
    blogger_id: int,
    run_id: int,
    body: FeedbackRetryRequest,
    service: FeedbackService = Depends(get_feedback_service),
) -> dict[str, Any]:
    try:
        return feedback_payload(
            service.retry(blogger_id, run_id, user_instruction=body.user_instruction)
        )
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.post(
    "/bloggers/{blogger_id}/feedback-runs/{run_id}/confirm",
    response_model=FeedbackRunRead,
)
def confirm_feedback_run(
    blogger_id: int,
    run_id: int,
    body: FeedbackConfirmRequest,
    service: FeedbackService = Depends(get_feedback_service),
) -> dict[str, Any]:
    try:
        overrides: dict[int | str, dict[str, Any]] = {
            key: value.model_dump(exclude_none=True) for key, value in body.place_overrides.items()
        }
        row = service.confirm(
            blogger_id,
            run_id,
            candidate_ids=body.candidate_ids,
            place_overrides=overrides,
        )
        return feedback_payload(row)
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


@router.post(
    "/bloggers/{blogger_id}/feedback-runs/{run_id}/reject",
    response_model=FeedbackRunRead,
)
def reject_feedback_run(
    blogger_id: int,
    run_id: int,
    body: FeedbackRejectRequest,
    service: FeedbackService = Depends(get_feedback_service),
) -> dict[str, Any]:
    try:
        return feedback_payload(
            service.reject(
                blogger_id,
                run_id,
                candidate_ids=body.candidate_ids,
                reason=body.reason,
            )
        )
    except FeedbackServiceError as exc:
        raise _error(exc) from exc


__all__ = ["get_feedback_agent", "get_feedback_service", "router"]

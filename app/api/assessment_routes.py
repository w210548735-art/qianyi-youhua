"""第二阶段知识库体检 API。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Assessment, AssessmentEvidence, AssessmentIndicator
from app.schemas.assessment import (
    AssessmentCompareRead,
    AssessmentCreate,
    AssessmentEvidenceRead,
    AssessmentListItem,
    AssessmentRead,
)
from app.services.assessment_agent import AssessmentAgent, DeepSeekAssessmentAgent
from app.services.assessment_comparison_service import (
    AssessmentComparisonError,
    AssessmentComparisonService,
)
from app.services.assessment_service import AssessmentService, AssessmentServiceError

router = APIRouter(prefix="/api/v1")


def get_assessment_agent() -> AssessmentAgent:
    return DeepSeekAssessmentAgent()


def get_assessment_service(
    db: Session = Depends(get_db),
    agent: AssessmentAgent = Depends(get_assessment_agent),
) -> AssessmentService:
    return AssessmentService(db, agent=agent)


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssessmentServiceError):
        return HTTPException(status_code=exc.status_code, detail=exc.code)
    if isinstance(exc, AssessmentComparisonError):
        status_code = 404 if exc.code in {"BLOGGER_NOT_FOUND", "ASSESSMENT_NOT_FOUND"} else 422
        return HTTPException(status_code=status_code, detail=exc.code)
    return HTTPException(status_code=500, detail="ASSESSMENT_PERSIST_FAILED")


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _evidence_payload(row: AssessmentEvidence) -> dict[str, Any]:
    return {
        "id": row.id,
        "assessment_id": row.assessment_id,
        "indicator_id": row.indicator_id,
        "evidence_type": row.evidence_type,
        "asset_id": row.asset_id,
        "source_document_id": row.source_document_id,
        "claim": row.claim,
        "created_at": row.created_at.isoformat(),
    }


def _indicator_payload(row: AssessmentIndicator) -> dict[str, Any]:
    return {
        "id": row.id,
        "assessment_id": row.assessment_id,
        "ordinal": row.ordinal,
        "name": row.name,
        "meaning": row.meaning,
        "score_logic": row.score_logic,
        "business_meaning": row.business_meaning,
        "weight": row.weight,
        "weight_reason": row.weight_reason,
        "score": row.score,
        "reason": row.reason,
        "evidence": _json(row.evidence_json, []),
        "evidences": [_evidence_payload(item) for item in row.evidences],
        "created_at": row.created_at.isoformat(),
    }


def assessment_payload(row: Assessment, *, details: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "task_id": row.task_id,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "snapshot_hash": row.snapshot_hash,
        "summary": row.summary,
        "overall_score": row.overall_score,
        "decision_id": row.decision_id,
        "prompt_version": row.prompt_version,
        "model_name": row.model_name,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat(),
    }
    if details:
        payload.update(
            {
                "input_snapshot": _json(row.input_snapshot_json, {}),
                "library_analysis": _json(row.library_analysis_json, {}),
                "feature_readiness": _json(row.feature_readiness_json, {}),
                "suggestions": _json(row.suggestions_json, []),
                "indicators": [_indicator_payload(item) for item in row.indicators],
                "evidence": [_evidence_payload(item) for item in row.evidences],
            }
        )
    return payload


@router.post("/bloggers/{blogger_id}/assessments", response_model=AssessmentRead)
def create_assessment(
    blogger_id: int,
    body: AssessmentCreate,
    service: AssessmentService = Depends(get_assessment_service),
) -> dict[str, Any]:
    try:
        assessment = service.start_assessment(blogger_id, body.idempotency_key)
        if assessment.status == "pending":
            assessment = service.execute_assessment(assessment.id, blogger_id)
        return assessment_payload(service.get_assessment(blogger_id, assessment.id))
    except AssessmentServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/assessments", response_model=list[AssessmentListItem])
def list_assessments(
    blogger_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: AssessmentService = Depends(get_assessment_service),
) -> list[dict[str, Any]]:
    try:
        rows = service.list_assessments(blogger_id, limit=page_size, offset=(page - 1) * page_size)
        return [assessment_payload(row, details=False) for row in rows]
    except AssessmentServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/assessments/compare", response_model=AssessmentCompareRead)
def compare_assessments(
    blogger_id: int,
    left_id: int = Query(gt=0),
    right_id: int = Query(gt=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return AssessmentComparisonService(db).compare(blogger_id, left_id, right_id)
    except AssessmentComparisonError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/assessments/{assessment_id}", response_model=AssessmentRead)
def get_assessment(
    blogger_id: int,
    assessment_id: int,
    service: AssessmentService = Depends(get_assessment_service),
) -> dict[str, Any]:
    try:
        return assessment_payload(service.get_assessment(blogger_id, assessment_id))
    except AssessmentServiceError as exc:
        raise _api_error(exc) from exc


@router.post(
    "/bloggers/{blogger_id}/assessments/{assessment_id}/retry",
    response_model=AssessmentRead,
)
def retry_assessment(
    blogger_id: int,
    assessment_id: int,
    service: AssessmentService = Depends(get_assessment_service),
) -> dict[str, Any]:
    try:
        row = service.retry_assessment(blogger_id, assessment_id)
        return assessment_payload(service.get_assessment(blogger_id, row.id))
    except AssessmentServiceError as exc:
        raise _api_error(exc) from exc


@router.get(
    "/bloggers/{blogger_id}/assessments/{assessment_id}/evidence",
    response_model=list[AssessmentEvidenceRead],
)
def list_assessment_evidence(
    blogger_id: int,
    assessment_id: int,
    service: AssessmentService = Depends(get_assessment_service),
) -> list[dict[str, Any]]:
    try:
        return [_evidence_payload(row) for row in service.list_evidence(blogger_id, assessment_id)]
    except AssessmentServiceError as exc:
        raise _api_error(exc) from exc


__all__ = [
    "assessment_payload",
    "get_assessment_agent",
    "get_assessment_service",
    "router",
]

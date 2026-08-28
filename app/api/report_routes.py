"""第四阶段经营指标与报告 API。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import IndicatorObservation, OperationalIndicator, Report, ReportEvidence
from app.schemas.report import (
    IndicatorCreateRequest,
    IndicatorRead,
    IndicatorRecomputeRequest,
    IndicatorUpdateRequest,
    ObservationRead,
    ReportGenerateRequest,
    ReportRead,
    ReportRetryRequest,
)
from app.services.indicator_service import IndicatorService, IndicatorServiceError
from app.services.report_agent import DeepSeekReportAgent, ReportAgent
from app.services.report_service import ReportService, ReportServiceError

router = APIRouter(prefix="/api/v1")


def get_report_agent() -> ReportAgent:
    return DeepSeekReportAgent()


def get_report_service(
    db: Session = Depends(get_db),
    agent: ReportAgent = Depends(get_report_agent),
) -> ReportService:
    return ReportService(db, agent=agent)


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReportServiceError):
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "details": exc.message},
        )
    if isinstance(exc, IndicatorServiceError):
        status = 404 if exc.code in {"BLOGGER_NOT_FOUND", "INDICATOR_NOT_FOUND"} else 422
        return HTTPException(status_code=status, detail={"code": exc.code, "details": exc.message})
    return HTTPException(status_code=500, detail={"code": "REPORT_PERSIST_FAILED"})


def indicator_payload(row: OperationalIndicator) -> dict[str, Any]:
    source = _decode(row.source_tables_json, [])
    if isinstance(source, dict):
        source = source.get("tables", [])
    return {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "category": row.category,
        "name": row.name,
        "meaning": row.meaning,
        "formula_key": row.formula_key,
        "source_tables": source if isinstance(source, list) else [],
        "unit": row.unit,
        "direction": row.direction,
        "target_value": row.target_value,
        "active": row.active,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def observation_payload(row: IndicatorObservation) -> dict[str, Any]:
    return {
        "id": row.id,
        "indicator_id": row.indicator_id,
        "feedback_run_id": row.feedback_run_id,
        "report_id": row.report_id,
        "value": row.value,
        "status": row.status,
        "trend": row.trend,
        "evidence": _decode(row.evidence_json, {}),
        "observed_at": row.observed_at,
    }


def report_payload(row: Report) -> dict[str, Any]:
    return {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "task_id": row.task_id,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "snapshot_hash": row.snapshot_hash,
        "conclusion": _decode(row.conclusion_json, {}),
        "charts": _decode(row.charts_json, []),
        "suggestions": _decode(row.suggestions_json, []),
        "data_quality": _decode(row.data_quality_json, {}),
        "prompt_version": row.prompt_version,
        "model_name": row.model_name,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def report_evidence_payload(row: ReportEvidence) -> dict[str, Any]:
    return {
        "id": row.id,
        "report_id": row.report_id,
        "evidence_type": row.evidence_type,
        "ref_id": row.ref_id,
        "claim": row.claim,
        "snapshot": _decode(row.snapshot_json, {}),
        "created_at": row.created_at,
    }


@router.post("/bloggers/{blogger_id}/indicators/defaults", response_model=list[IndicatorRead])
def initialize_indicators(
    blogger_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    try:
        return [indicator_payload(row) for row in IndicatorService(db).initialize_defaults(blogger_id)]
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.post("/bloggers/{blogger_id}/indicators", response_model=IndicatorRead)
def create_indicator(
    blogger_id: int,
    body: IndicatorCreateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return indicator_payload(
            IndicatorService(db).create_indicator(blogger_id, body.model_dump())
        )
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/indicators", response_model=list[IndicatorRead])
def list_indicators(
    blogger_id: int,
    active: bool | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        return [
            indicator_payload(row)
            for row in IndicatorService(db).list_indicators(blogger_id, active=active)
        ]
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.post("/bloggers/{blogger_id}/indicators/recompute", response_model=list[ObservationRead])
def recompute_indicators(
    blogger_id: int,
    body: IndicatorRecomputeRequest,
    indicator_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # idempotency_key 由 API 契约要求；同一次 feedback/report 由数据库唯一约束去重。
    _ = body.idempotency_key
    try:
        return [
            observation_payload(row)
            for row in IndicatorService(db).recompute(
                blogger_id,
                indicator_id=indicator_id,
                feedback_run_id=body.feedback_run_id,
                idempotency_key=body.idempotency_key,
            )
        ]
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/indicators/{indicator_id}", response_model=IndicatorRead)
def get_indicator(
    blogger_id: int, indicator_id: int, db: Session = Depends(get_db)
) -> dict[str, Any]:
    try:
        return indicator_payload(IndicatorService(db).get_indicator(blogger_id, indicator_id))
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.patch("/bloggers/{blogger_id}/indicators/{indicator_id}", response_model=IndicatorRead)
def update_indicator(
    blogger_id: int,
    indicator_id: int,
    body: IndicatorUpdateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return indicator_payload(
            IndicatorService(db).update_indicator(
                blogger_id, indicator_id, body.model_dump(exclude_unset=True)
            )
        )
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.delete("/bloggers/{blogger_id}/indicators/{indicator_id}", status_code=204)
def deactivate_indicator(
    blogger_id: int, indicator_id: int, db: Session = Depends(get_db)
) -> Response:
    try:
        IndicatorService(db).deactivate_indicator(blogger_id, indicator_id)
        return Response(status_code=204)
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.get(
    "/bloggers/{blogger_id}/indicators/{indicator_id}/observations",
    response_model=list[ObservationRead],
)
def list_indicator_observations(
    blogger_id: int,
    indicator_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        return [
            observation_payload(row)
            for row in IndicatorService(db).get_history(blogger_id, indicator_id, limit=limit)
        ]
    except IndicatorServiceError as exc:
        raise _error(exc) from exc


@router.post("/bloggers/{blogger_id}/reports", response_model=ReportRead)
def generate_report(
    blogger_id: int,
    body: ReportGenerateRequest,
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    try:
        return report_payload(
            service.generate(
                blogger_id,
                body.idempotency_key,
                user_instruction=body.user_instruction,
            )
        )
    except ReportServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/reports", response_model=list[ReportRead])
def list_reports(
    blogger_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
    service: ReportService = Depends(get_report_service),
) -> list[dict[str, Any]]:
    try:
        return [
            report_payload(row)
            for row in service.list_reports(
                blogger_id, limit=page_size, offset=(page - 1) * page_size
            )
        ]
    except ReportServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/reports/compare")
def compare_reports(
    blogger_id: int,
    left_id: int = Query(gt=0),
    right_id: int = Query(gt=0),
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    try:
        return service.compare(blogger_id, left_id, right_id)
    except ReportServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/reports/{report_id}/evidence")
def get_report_evidence(
    blogger_id: int,
    report_id: int,
    service: ReportService = Depends(get_report_service),
) -> list[dict[str, Any]]:
    try:
        return [report_evidence_payload(row) for row in service.list_evidence(blogger_id, report_id)]
    except ReportServiceError as exc:
        raise _error(exc) from exc


@router.get("/bloggers/{blogger_id}/reports/{report_id}", response_model=ReportRead)
def get_report(
    blogger_id: int,
    report_id: int,
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    try:
        return report_payload(service.get(blogger_id, report_id))
    except ReportServiceError as exc:
        raise _error(exc) from exc


@router.post("/bloggers/{blogger_id}/reports/{report_id}/retry", response_model=ReportRead)
def retry_report(
    blogger_id: int,
    report_id: int,
    body: ReportRetryRequest,
    service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    try:
        return report_payload(
            service.retry(
                blogger_id,
                report_id,
                user_instruction=body.user_instruction,
            )
        )
    except ReportServiceError as exc:
        raise _error(exc) from exc


__all__ = ["get_report_agent", "get_report_service", "router"]

"""第三阶段内容产出、排期、模拟发布与指标回收 API。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import AssetPlace, CollectionJob, Metric, Output, PublishEvent, Schedule
from app.schemas.output import (
    CollectionCreateRequest,
    CollectionJobRead,
    CollectionRetryRequest,
    MetricRead,
    OutputDeleteRead,
    OutputListItem,
    OutputRead,
    OutputRevisionRequest,
    PublishRequest,
    RouteGenerateRequest,
    ScheduleCreateRequest,
    ScheduleRead,
    ScheduleUpdateRequest,
    ScriptGenerateRequest,
    StoryboardGenerateRequest,
)
from app.services.collection_service import CollectionService, CollectionServiceError
from app.services.output_agent import DeepSeekOutputAgent, OutputAgent
from app.services.output_service import OutputService, OutputServiceError
from app.services.route_service import RouteService, RouteServiceError
from app.services.schedule_service import ScheduleService, ScheduleServiceError

router = APIRouter(prefix="/api/v1")


def get_output_agent() -> OutputAgent:
    return DeepSeekOutputAgent()


def get_output_service(
    db: Session = Depends(get_db),
    agent: OutputAgent = Depends(get_output_agent),
) -> OutputService:
    return OutputService(db, agent=agent)


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (OutputServiceError, RouteServiceError, ScheduleServiceError, CollectionServiceError)):
        return HTTPException(
            status_code=getattr(exc, "status_code", 422),
            detail={
                "code": getattr(exc, "code", str(exc)),
                "details": getattr(exc, "details", getattr(exc, "detail", None)),
            },
        )
    return HTTPException(status_code=500, detail={"code": "OUTPUT_PERSIST_FAILED"})


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def output_payload(row: Output, *, details: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "task_id": row.task_id,
        "idempotency_key": row.idempotency_key,
        "type": row.type,
        "category": row.category,
        "title": row.title,
        "status": row.status,
        "assessment_id": row.assessment_id,
        "parent_output_id": row.parent_output_id,
        "version": row.version,
        "manual_locked": row.manual_locked,
        "decision_id": row.decision_id,
        "prompt_version": row.prompt_version,
        "model_name": row.model_name,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "deleted_at": row.deleted_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if details:
        payload.update(
            {
                "content_json": _decode(row.content_json, row.content_json),
                "assets": [
                    {
                        "id": item.id,
                        "output_id": item.output_id,
                        "asset_id": item.asset_id,
                        "usage_type": item.usage_type,
                        "claim": item.claim,
                    }
                    for item in row.assets
                ],
                "places": [
                    {
                        "id": item.id,
                        "output_id": item.output_id,
                        "place_id": item.place_id,
                        "role": item.role,
                        "sequence": item.sequence,
                        "claim": item.claim,
                    }
                    for item in row.places
                ],
            }
        )
    return payload


def schedule_payload(row: Schedule) -> dict[str, Any]:
    return {
        "id": row.id,
        "blogger_id": row.blogger_id,
        "output_id": row.output_id,
        "plan_date": row.plan_date,
        "platform": row.platform,
        "content_type": row.content_type,
        "title": row.title,
        "status": row.status,
        "publish_time": row.publish_time,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def metric_payload(row: Metric) -> dict[str, Any]:
    return {
        "id": row.id,
        "output_id": row.output_id,
        "schedule_id": row.schedule_id,
        "source_type": row.source_type,
        "views": row.views,
        "likes": row.likes,
        "comments": row.comments,
        "collects": row.collects,
        "idempotency_key": row.idempotency_key,
        "collected_at": row.collected_at,
        "created_at": row.created_at,
    }


def collection_payload(row: CollectionJob) -> dict[str, Any]:
    return {
        "id": row.id,
        "schedule_id": row.schedule_id,
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "result_json": _decode(row.result_json, row.result_json),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


@router.post("/bloggers/{blogger_id}/outputs/generate/script", response_model=OutputRead)
def generate_script(
    blogger_id: int,
    body: ScriptGenerateRequest,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        if body.assessment_id is None:
            raise OutputServiceError("ASSESSMENT_NOT_READY")
        row = service.start_generation(
            blogger_id,
            "script",
            body.assessment_id,
            body.idempotency_key,
            user_instruction=body.user_instruction or body.topic or "",
            category=body.category or None,
        )
        if row.status == "pending":
            row = service.execute_generation(row.id, blogger_id)
        return output_payload(service.get_output(blogger_id, row.id))
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/outputs/generate/storyboard", response_model=OutputRead)
def generate_storyboard(
    blogger_id: int,
    body: StoryboardGenerateRequest,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        if body.assessment_id is None:
            raise OutputServiceError("ASSESSMENT_NOT_READY")
        row = service.start_generation(
            blogger_id,
            "storyboard",
            body.assessment_id,
            body.idempotency_key,
            user_instruction=body.user_instruction or "",
            parent_output_id=body.script_output_id,
            category=body.category or None,
        )
        if row.status == "pending":
            row = service.execute_generation(row.id, blogger_id)
        return output_payload(service.get_output(blogger_id, row.id))
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/outputs/generate/route", response_model=OutputRead)
def generate_route(
    blogger_id: int,
    body: RouteGenerateRequest,
    db: Session = Depends(get_db),
    agent: OutputAgent = Depends(get_output_agent),
    output_service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        if body.assessment_id is None:
            raise RouteServiceError("ASSESSMENT_NOT_READY")
        row = RouteService(
            db,
            agent=agent,
            output_service=output_service,
            task_service=output_service.task_service,
            context_service=output_service.context_service,
            memory_service=output_service.memory_service,
        ).recommend(
            blogger_id,
            body.assessment_id,
            body.idempotency_key,
            place_ids=body.place_ids or None,
            title=body.title or "收益约束路线推荐",
            category=body.category or "路线",
            user_instruction=body.user_instruction or body.topic or "",
        )
        return output_payload(output_service.get_output(blogger_id, row.id))
    except (RouteServiceError, OutputServiceError) as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/outputs", response_model=list[OutputListItem])
def list_outputs(
    blogger_id: int,
    output_type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
    service: OutputService = Depends(get_output_service),
) -> list[dict[str, Any]]:
    try:
        return [
            output_payload(row, details=False)
            for row in service.list_outputs(
                blogger_id,
                output_type=output_type,
                status=status,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
        ]
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/outputs/{output_id}/evidence")
def get_output_evidence(
    blogger_id: int,
    output_id: int,
    db: Session = Depends(get_db),
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        row = service.get_output(blogger_id, output_id)
        place_ids = [item.place_id for item in row.places]
        relations = []
        if place_ids:
            relations = list(
                db.scalars(select(AssetPlace).where(AssetPlace.place_id.in_(place_ids)).order_by(AssetPlace.id))
            )
        return {
            "output_id": row.id,
            "assets": output_payload(row)["assets"],
            "places": output_payload(row)["places"],
            "asset_places": [
                {
                    "id": item.id,
                    "asset_id": item.asset_id,
                    "place_id": item.place_id,
                    "relation_type": item.relation_type,
                    "source_type": item.source_type,
                }
                for item in relations
            ],
        }
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/outputs/{output_id}/retry", response_model=OutputRead)
def retry_output(
    blogger_id: int,
    output_id: int,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        return output_payload(service.retry_generation(blogger_id, output_id))
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/outputs/{output_id}/revisions", response_model=OutputRead)
def revise_output(
    blogger_id: int,
    output_id: int,
    body: OutputRevisionRequest,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        if not isinstance(body.content_json, dict):
            raise OutputServiceError("OUTPUT_INVALID_JSON")
        row = service.revise_output(blogger_id, output_id, body.content_json, title=body.title)
        return output_payload(row)
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/outputs/{output_id}", response_model=OutputRead)
def get_output(
    blogger_id: int,
    output_id: int,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        return output_payload(service.get_output(blogger_id, output_id))
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


@router.delete("/bloggers/{blogger_id}/outputs/{output_id}", response_model=OutputDeleteRead)
def delete_output(
    blogger_id: int,
    output_id: int,
    service: OutputService = Depends(get_output_service),
) -> dict[str, Any]:
    try:
        row = service.soft_delete_output(blogger_id, output_id)
        return {"id": row.id, "blogger_id": row.blogger_id, "status": "deleted", "deleted_at": row.deleted_at}
    except OutputServiceError as exc:
        raise _api_error(exc) from exc


# 排期静态路径与输出动态路径使用不同前缀，避免 ID 路由冲突。
@router.post("/bloggers/{blogger_id}/schedules", response_model=ScheduleRead)
def create_schedule(
    blogger_id: int,
    body: ScheduleCreateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        return schedule_payload(ScheduleService(db).create_schedule(blogger_id, **body.model_dump()))
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/schedules", response_model=list[ScheduleRead])
def list_schedules(
    blogger_id: int,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        rows = ScheduleService(db).list_schedules(
            blogger_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return [schedule_payload(row) for row in rows]
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/schedules/reminders/scan")
def scan_reminders(
    blogger_id: int,
    on_date: date | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        rows = ScheduleService(db).due_reminders(blogger_id, on_date=on_date)
        return [
            {
                "id": row.id,
                "schedule_id": row.schedule_id,
                "reminder_date": row.reminder_date,
                "status": row.status,
                "dedupe_key": row.dedupe_key,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/schedules/{schedule_id}/publish")
def publish_schedule(
    blogger_id: int,
    schedule_id: int,
    body: PublishRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        schedule = ScheduleService(db).publish(blogger_id, schedule_id, body.idempotency_key)
        event = db.scalar(
            select(PublishEvent)
            .where(PublishEvent.schedule_id == schedule.id, PublishEvent.idempotency_key == body.idempotency_key)
            .order_by(PublishEvent.id.desc())
        )
        return {
            "simulated": True,
            "notice": "本地模拟发布，不代表真实平台发布成功",
            "schedule": schedule_payload(schedule),
            "event": {
                "id": event.id,
                "status": event.status,
                "idempotency_key": event.idempotency_key,
                "published_at": event.published_at,
            }
            if event
            else None,
        }
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/schedules/{schedule_id}/cancel", response_model=ScheduleRead)
def cancel_schedule(blogger_id: int, schedule_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return schedule_payload(ScheduleService(db).cancel_schedule(blogger_id, schedule_id))
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.put("/bloggers/{blogger_id}/schedules/{schedule_id}", response_model=ScheduleRead)
def update_schedule(
    blogger_id: int,
    schedule_id: int,
    body: ScheduleUpdateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        changes = body.model_dump(exclude_unset=True)
        if "status" in changes:
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        return schedule_payload(ScheduleService(db).update_schedule(blogger_id, schedule_id, changes))
    except ScheduleServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/schedules/{schedule_id}/collections", response_model=CollectionJobRead)
def collect_metrics(
    blogger_id: int,
    schedule_id: int,
    body: CollectionCreateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        service = CollectionService(db)
        source_type = body.metrics.source_type if body.metrics is not None else "simulated"
        job = service.start_collection(
            blogger_id,
            schedule_id,
            body.idempotency_key,
            source_type=source_type,
        )
        metric_values = None
        if body.metrics is not None:
            metric_values = body.metrics.model_dump(exclude_none=True)
        if job.status in {"pending", "failed"}:
            job = service.execute_collection(
                job.id,
                metric_values,
                blogger_id=blogger_id,
                source_type=source_type,
            )
        return collection_payload(job)
    except CollectionServiceError as exc:
        raise _api_error(exc) from exc


@router.post("/bloggers/{blogger_id}/collections/{job_id}/retry", response_model=CollectionJobRead)
def retry_collection(
    blogger_id: int,
    job_id: int,
    body: CollectionRetryRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        metrics = None
        source_type = None
        if body.metrics is not None:
            metrics = body.metrics.model_dump(exclude_none=True)
            source_type = body.metrics.source_type
        return collection_payload(
            CollectionService(db).retry_collection(
                blogger_id, job_id, metrics, source_type=source_type
            )
        )
    except CollectionServiceError as exc:
        raise _api_error(exc) from exc


@router.get("/bloggers/{blogger_id}/metrics", response_model=list[MetricRead])
def list_metrics(
    blogger_id: int,
    schedule_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        return [metric_payload(row) for row in CollectionService(db).list_metrics(blogger_id, schedule_id)]
    except CollectionServiceError as exc:
        raise _api_error(exc) from exc


__all__ = ["get_output_agent", "get_output_service", "output_payload", "router"]

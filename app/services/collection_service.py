"""第三阶段手工/模拟指标回收服务。

本服务只写入 ``manual`` 或 ``simulated`` 原始指标，不做反馈判断，也不调用
真实平台。回收失败会保留已发布排期和可重试的 CollectionJob。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Blogger, CollectionJob, DecisionLog, Metric, Schedule


class CollectionServiceError(RuntimeError):
    """回收服务错误，``code``为稳定 API 错误码。"""

    def __init__(self, code: str, *, status_code: int = 422, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail or code


class CollectionService:
    """管理发布后手工/模拟原始指标和回收任务。"""

    VALID_SOURCES = {"manual", "simulated"}

    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        provider: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock or datetime.utcnow
        self.provider = provider

    # ------------------------------------------------------------------
    # 回收任务生命周期
    # ------------------------------------------------------------------
    def start_collection(
        self,
        blogger_id: int,
        schedule_id: int,
        idempotency_key: str,
        *,
        source_type: str = "simulated",
        commit: bool = True,
    ) -> CollectionJob:
        # 先按博主作用域查找既有任务；成功回收后排期会变成 collected，
        # 同一幂等键仍应返回原任务，而不是被状态机误判为新任务。
        self._active_blogger(blogger_id)
        source = self._normalize_source(source_type)
        key = self._required_key(idempotency_key)
        schedule = self.db.scalar(
            select(Schedule).where(
                Schedule.id == schedule_id,
                Schedule.blogger_id == blogger_id,
            )
        )
        if schedule is None:
            raise CollectionServiceError("SCHEDULE_NOT_FOUND", status_code=404)
        existing = self.db.scalar(
            select(CollectionJob).where(
                CollectionJob.schedule_id == schedule.id,
                CollectionJob.idempotency_key == key,
            )
        )
        if existing is not None:
            return existing
        if schedule.status != "published":
            raise CollectionServiceError("COLLECTION_INVALID_STATE")
        try:
            job = CollectionJob(
                schedule_id=schedule.id,
                status="pending",
                idempotency_key=key,
                result_json=json.dumps({"source_type": source}, ensure_ascii=False),
            )
            self.db.add(job)
            self.db.flush()
            if commit:
                self.db.commit()
                self.db.refresh(job)
            return job
        except IntegrityError as exc:
            self.db.rollback()
            duplicate = self.db.scalar(
                select(CollectionJob).where(
                    CollectionJob.schedule_id == schedule.id,
                    CollectionJob.idempotency_key == key,
                )
            )
            if duplicate is not None:
                return duplicate
            raise CollectionServiceError("COLLECTION_PERSIST_FAILED", status_code=500) from exc
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise CollectionServiceError("COLLECTION_PERSIST_FAILED", status_code=500) from exc

    start = start_collection

    def execute_collection(
        self,
        job_id: int,
        metrics: Mapping[str, Any] | int | None = None,
        *args: Any,
        blogger_id: int | None = None,
        source_type: str | None = None,
        commit: bool = True,
    ) -> CollectionJob:
        """执行一次手工/模拟回收，返回任务而不暴露跨博主资源。"""

        # 兼容 ``execute_collection(job_id, blogger_id, metrics)`` 的调用习惯；
        # API 层推荐使用显式 blogger_id 关键字，避免作用域参数错位。
        if isinstance(metrics, int) and not isinstance(metrics, bool):
            if blogger_id is None:
                blogger_id = metrics
            metrics = args[0] if args else None

        job, schedule = self._get_job(job_id, blogger_id)
        stable_job_id = job.id
        if job.status == "succeeded":
            return job
        if job.status == "running":
            raise CollectionServiceError("COLLECTION_ALREADY_RUNNING", status_code=409)
        if schedule.status not in {"published", "collected"}:
            raise CollectionServiceError("COLLECTION_INVALID_STATE")
        now = self._now()
        job.status = "running"
        job.started_at = now
        job.error_code = None
        job.error_message = None
        self.db.commit()
        try:
            saved_config = self._decode_json(job.result_json)
            source = self._normalize_source(source_type or saved_config.get("source_type", "simulated"))
            if metrics is None:
                payload = self._fetch(schedule, source)
            elif hasattr(metrics, "model_dump"):
                payload = dict(metrics.model_dump())
            elif isinstance(metrics, Mapping):
                payload = dict(metrics)
            else:
                raise CollectionServiceError("COLLECTION_METRIC_INVALID")
            payload_source = self._normalize_source(payload.pop("source_type", source))
            collected_at = self._coerce_datetime(payload.pop("collected_at", now))
            values = self._normalise_metrics(payload)
            # 兼容第三阶段自定义归一化器只返回四项旧指标；数据库仍负责最终约束兜底。
            values.setdefault("shares", 0)
            values.setdefault("actual_revenue", None)
            values.setdefault("actual_cost", None)
            values.setdefault("user_confirmed", False)
            has_actual = (
                values["actual_revenue"] is not None or values["actual_cost"] is not None
            )
            if has_actual and (payload_source != "manual" or not values["user_confirmed"]):
                raise CollectionServiceError("COLLECTION_METRIC_INVALID")
            existing_metric = self.db.scalar(
                select(Metric).where(
                    Metric.schedule_id == schedule.id,
                    Metric.idempotency_key == job.idempotency_key,
                )
            )
            if existing_metric is None:
                metric = Metric(
                    output_id=schedule.output_id,
                    schedule_id=schedule.id,
                    source_type=payload_source,
                    views=values["views"],
                    likes=values["likes"],
                    comments=values["comments"],
                    collects=values["collects"],
                    shares=values["shares"],
                    actual_revenue=values["actual_revenue"],
                    actual_cost=values["actual_cost"],
                    user_confirmed=values["user_confirmed"],
                    idempotency_key=job.idempotency_key,
                    collected_at=collected_at,
                )
                self.db.add(metric)
                self.db.flush()
            schedule.status = "collected"
            job.status = "succeeded"
            job.result_json = json.dumps(
                {
                    "source_type": payload_source,
                    "collected_at": collected_at.isoformat(),
                    **self._json_metric_values(values),
                },
                ensure_ascii=False,
            )
            job.error_code = None
            job.error_message = None
            job.finished_at = self._now()
            self._record_decision(
                schedule.blogger_id,
                "collection_simulated" if payload_source == "simulated" else "collection_manual",
                {
                    "schedule_id": schedule.id,
                    "job_id": job.id,
                    "source_type": payload_source,
                    **self._json_metric_values(values),
                },
                "仅记录手工/模拟原始指标，不执行反馈判断或资产更新",
            )
            if commit:
                self.db.commit()
                self.db.refresh(job)
            return job
        except Exception as exc:
            if isinstance(exc, CollectionServiceError):
                code = exc.code
            elif isinstance(exc, SQLAlchemyError):
                code = "COLLECTION_PERSIST_FAILED"
            else:
                code = "COLLECTION_FAILED"
            self.db.rollback()
            self._persist_failure(stable_job_id, code, str(exc))
            if isinstance(exc, CollectionServiceError):
                raise
            raise CollectionServiceError(code, status_code=500) from exc

    execute = execute_collection

    def retry_collection(
        self,
        blogger_id: int,
        job_id: int,
        metrics: Mapping[str, Any] | None = None,
        *,
        source_type: str | None = None,
    ) -> CollectionJob:
        job, schedule = self._get_job(job_id, blogger_id)
        if job.status == "succeeded":
            return job
        if schedule.status not in {"published", "collected"}:
            raise CollectionServiceError("COLLECTION_INVALID_STATE")
        job.status = "pending"
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        self.db.commit()
        return self.execute_collection(job.id, metrics, blogger_id=blogger_id, source_type=source_type)

    retry = retry_collection

    def collect(
        self,
        blogger_id: int,
        schedule_id: int,
        metrics: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str = "manual-default",
        source_type: str = "manual",
    ) -> CollectionJob:
        job = self.start_collection(
            blogger_id,
            schedule_id,
            idempotency_key,
            source_type=source_type,
        )
        return self.execute_collection(job.id, metrics, blogger_id=blogger_id, source_type=source_type)

    def get_collection_job(self, blogger_id: int, job_id: int) -> CollectionJob:
        job, _ = self._get_job(job_id, blogger_id)
        return job

    get = get_collection_job

    def list_collection_jobs(self, blogger_id: int, schedule_id: int | None = None) -> list[CollectionJob]:
        self._active_blogger(blogger_id)
        statement = (
            select(CollectionJob)
            .join(Schedule, Schedule.id == CollectionJob.schedule_id)
            .where(Schedule.blogger_id == blogger_id)
            .order_by(CollectionJob.id.desc())
        )
        if schedule_id is not None:
            statement = statement.where(CollectionJob.schedule_id == schedule_id)
        return list(self.db.scalars(statement))

    def list_metrics(self, blogger_id: int, schedule_id: int | None = None) -> list[Metric]:
        self._active_blogger(blogger_id)
        statement = (
            select(Metric)
            .join(Schedule, Schedule.id == Metric.schedule_id)
            .where(Schedule.blogger_id == blogger_id)
            .order_by(Metric.collected_at.desc(), Metric.id.desc())
        )
        if schedule_id is not None:
            statement = statement.where(Metric.schedule_id == schedule_id)
        return list(self.db.scalars(statement))

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _get_published_schedule(self, blogger_id: int, schedule_id: int) -> Schedule:
        self._active_blogger(blogger_id)
        row = self.db.scalar(
            select(Schedule).where(
                Schedule.id == schedule_id,
                Schedule.blogger_id == blogger_id,
            )
        )
        if row is None:
            raise CollectionServiceError("SCHEDULE_NOT_FOUND", status_code=404)
        if row.status != "published":
            raise CollectionServiceError("COLLECTION_INVALID_STATE")
        return row

    def _get_job(self, job_id: int, blogger_id: int | None) -> tuple[CollectionJob, Schedule]:
        statement = (
            select(CollectionJob, Schedule)
            .join(Schedule, Schedule.id == CollectionJob.schedule_id)
            .join(Blogger, Blogger.id == Schedule.blogger_id)
            .where(CollectionJob.id == job_id, Blogger.deleted_at.is_(None))
        )
        if blogger_id is not None:
            statement = statement.where(Schedule.blogger_id == blogger_id)
        row = self.db.execute(statement).first()
        if row is None:
            raise CollectionServiceError("COLLECTION_NOT_FOUND", status_code=404)
        return row[0], row[1]

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise CollectionServiceError("BLOGGER_NOT_FOUND", status_code=404)
        return row

    def _fetch(self, schedule: Schedule, source_type: str) -> dict[str, Any]:
        if self.provider is not None:
            try:
                provider = self.provider
                if callable(provider):
                    result = provider(schedule=schedule, source_type=source_type)
                elif hasattr(provider, "collect"):
                    result = provider.collect(schedule=schedule, source_type=source_type)
                elif hasattr(provider, "fetch"):
                    result = provider.fetch(schedule=schedule, source_type=source_type)
                else:
                    raise CollectionServiceError("COLLECTION_FAILED", detail="采集器接口不可用")
            except TypeError:
                if callable(self.provider):
                    result = self.provider(schedule, source_type)
                elif hasattr(self.provider, "collect"):
                    result = self.provider.collect(schedule, source_type)
                else:
                    result = self.provider.fetch(schedule, source_type)
            if not isinstance(result, Mapping):
                raise CollectionServiceError("COLLECTION_FAILED", detail="采集器返回结构非法")
            return dict(result)
        # 固定可复现的演示数据，并明确标记为 simulated。
        return {
            "source_type": "simulated",
            "views": 0,
            "likes": 0,
            "comments": 0,
            "collects": 0,
            "shares": 0,
            "user_confirmed": False,
        }

    @classmethod
    def _normalise_metrics(
        cls,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "views",
            "likes",
            "comments",
            "collects",
            "shares",
            "actual_revenue",
            "actual_cost",
            "user_confirmed",
        }
        if set(payload) - allowed:
            raise CollectionServiceError("COLLECTION_METRIC_INVALID")
        result: dict[str, Any] = {}
        for field in ("views", "likes", "comments", "collects", "shares"):
            value = payload.get(field, 0)
            if isinstance(value, bool):
                raise CollectionServiceError("COLLECTION_METRIC_INVALID")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise CollectionServiceError("COLLECTION_METRIC_INVALID") from exc
            if number < 0:
                raise CollectionServiceError("COLLECTION_METRIC_INVALID")
            result[field] = number
        user_confirmed = payload.get("user_confirmed", False)
        if not isinstance(user_confirmed, bool):
            raise CollectionServiceError("COLLECTION_METRIC_INVALID")
        result["user_confirmed"] = user_confirmed
        for field in ("actual_revenue", "actual_cost"):
            value = payload.get(field)
            if value is None:
                result[field] = None
                continue
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise CollectionServiceError("COLLECTION_METRIC_INVALID") from exc
            if not decimal_value.is_finite() or decimal_value < 0:
                raise CollectionServiceError("COLLECTION_METRIC_INVALID")
            result[field] = decimal_value
        return result

    @staticmethod
    def _json_metric_values(values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: float(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
        }

    @classmethod
    def _normalize_source(cls, source: str) -> str:
        source = str(source or "").strip().lower()
        if source not in cls.VALID_SOURCES:
            raise CollectionServiceError("COLLECTION_SOURCE_INVALID")
        return source

    @staticmethod
    def _required_key(value: Any) -> str:
        key = str(value or "").strip()
        if not key:
            raise CollectionServiceError("COLLECTION_IDEMPOTENCY_REQUIRED")
        return key

    def _persist_failure(self, job_id: int, code: str, message: str) -> None:
        self.db.rollback()
        job = self.db.get(CollectionJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_code = code
        job.error_message = str(message)[:1000]
        job.finished_at = self._now()
        self.db.commit()

    def _record_decision(self, blogger_id: int, decision_type: str, decision: dict[str, Any], reason: str) -> None:
        self.db.add(
            DecisionLog(
                blogger_id=blogger_id,
                decision_type=decision_type,
                prompt_version="phase3-collection-v1",
                input_summary=json.dumps(decision, ensure_ascii=False, default=str),
                decision=json.dumps(decision, ensure_ascii=False, default=str),
                reason=reason,
            )
        )
        self.db.flush()

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise CollectionServiceError("COLLECTION_CLOCK_INVALID")
        return value

    @staticmethod
    def _coerce_datetime(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CollectionServiceError("COLLECTION_METRIC_INVALID") from exc

    @staticmethod
    def _decode_json(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            result = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return result if isinstance(result, dict) else {}


CollectionError = CollectionServiceError

__all__ = ["CollectionError", "CollectionService", "CollectionServiceError"]

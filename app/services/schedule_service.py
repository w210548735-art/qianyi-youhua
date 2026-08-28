"""第三阶段排期、提醒和模拟发布服务。

排期层只负责内容执行状态，不调用任何真实平台 API。所有公开方法都把
``blogger_id``作为作用域的一部分，并在读取关联输出时再次校验博主归属，
以避免把排期、输出或发布事件泄漏给其他博主。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Blogger, DecisionLog, Output, PublishEvent, ReminderEvent, Schedule


class ScheduleServiceError(RuntimeError):
    """排期服务错误，``code``可直接作为 API 稳定错误码。"""

    def __init__(self, code: str, *, status_code: int = 422, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail or code


class ScheduleService:
    """管理排期、手工提醒扫描以及模拟发布。"""

    VALID_STATUSES = {"pending", "published", "collected", "cancelled"}
    ACTIVE_STATUS = "pending"
    _DAILY = {"日更", "每天", "每日", "daily", "day", "每天更新"}
    _WEEKLY = {"周更", "每周", "weekly", "week", "每周更新"}
    _MONTHLY = {"月更", "每月", "monthly", "month", "每月更新"}

    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        # ``now``是兼容性别名；正式调用优先使用clock。
        self.clock = clock or now or datetime.utcnow

    # ------------------------------------------------------------------
    # 排期 CRUD
    # ------------------------------------------------------------------
    def create_schedule(
        self,
        blogger_id: int,
        output_id: int | Mapping[str, Any],
        plan_date: date | datetime | str | None = None,
        platform: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        *,
        status: str = "pending",
        publish_time: datetime | str | None = None,
        commit: bool = True,
        **kwargs: Any,
    ) -> Schedule:
        """创建待发布排期，并按博主画像频率校验计划密度。

        ``output_id``必须指向同一有效博主的未删除输出。相同输出、日期、
        平台和内容类型的重复请求按稳定错误处理，不会生成两条排期。
        """

        # 兼容 ``create_schedule(blogger_id, payload)`` 的服务层调用方式。
        if hasattr(output_id, "model_dump"):
            payload = dict(output_id.model_dump())
            output_id = payload.pop("output_id", 0)
            plan_date = payload.pop("plan_date", plan_date)
            platform = payload.pop("platform", platform)
            content_type = payload.pop("content_type", content_type)
            title = payload.pop("title", title)
        elif isinstance(output_id, Mapping):
            payload = dict(output_id)
            output_id = payload.pop("output_id", 0)
            plan_date = payload.pop("plan_date", plan_date)
            platform = payload.pop("platform", platform)
            content_type = payload.pop("content_type", content_type)
            title = payload.pop("title", title)
        if not isinstance(output_id, int) or plan_date is None:
            raise ScheduleServiceError("SCHEDULE_REQUIRED_FIELD")
        self._active_blogger(blogger_id)
        output = self._get_output(blogger_id, output_id)
        plan = self._coerce_date(plan_date)
        platform_text = self._required_text(platform, "platform")
        type_text = self._required_text(content_type or kwargs.get("type"), "content_type")
        title_text = self._required_text(title or getattr(output, "title", None), "title")
        normalized_status = self._normalize_status(status)
        if normalized_status != "pending":
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        # 先判定业务幂等，再做频率限制；重复请求必须返回原排期，而不能被
        # “同一天已有排期”的规则误判为新请求。
        existing = self.db.scalar(
            select(Schedule).where(
                Schedule.blogger_id == blogger_id,
                Schedule.output_id == output.id,
                Schedule.plan_date == plan,
                Schedule.platform == platform_text,
                Schedule.content_type == type_text,
                Schedule.status != "cancelled",
            )
        )
        if existing is not None:
            return existing
        self._validate_frequency(blogger_id, plan)
        try:
            schedule = Schedule(
                blogger_id=blogger_id,
                output_id=output.id,
                plan_date=plan,
                platform=platform_text,
                content_type=type_text,
                title=title_text,
                status="pending",
                publish_time=self._coerce_datetime(publish_time) if publish_time is not None else None,
            )
            self.db.add(schedule)
            self.db.flush()
            self._record_decision(
                blogger_id,
                "schedule_create",
                {"schedule_id": schedule.id, "output_id": output.id, "plan_date": plan.isoformat()},
                "用户确认内容排期，后续发布仅执行模拟状态变更",
            )
            if commit:
                self.db.commit()
                self.db.refresh(schedule)
            return schedule
        except IntegrityError as exc:
            self.db.rollback()
            duplicate = self.db.scalar(
                select(Schedule).where(
                    Schedule.blogger_id == blogger_id,
                    Schedule.output_id == output.id,
                    Schedule.plan_date == plan,
                    Schedule.platform == platform_text,
                    Schedule.content_type == type_text,
                )
            )
            if duplicate is not None:
                return duplicate
            raise ScheduleServiceError("SCHEDULE_PERSIST_FAILED", status_code=500) from exc
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise ScheduleServiceError("SCHEDULE_PERSIST_FAILED", status_code=500) from exc

    # 常见兼容别名。
    create = create_schedule

    def get_schedule(self, blogger_id: int, schedule_id: int, *, include_cancelled: bool = True) -> Schedule:
        self._active_blogger(blogger_id)
        statement = select(Schedule).where(
            Schedule.id == schedule_id,
            Schedule.blogger_id == blogger_id,
        )
        if not include_cancelled:
            statement = statement.where(Schedule.status != "cancelled")
        row = self.db.scalar(statement)
        if row is None:
            raise ScheduleServiceError("SCHEDULE_NOT_FOUND", status_code=404)
        return row

    get = get_schedule

    def list_schedules(
        self,
        blogger_id: int,
        *,
        status: str | None = None,
        start_date: date | datetime | str | None = None,
        end_date: date | datetime | str | None = None,
        include_cancelled: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Schedule]:
        self._active_blogger(blogger_id)
        if limit < 1 or limit > 1000 or offset < 0:
            raise ScheduleServiceError("SCHEDULE_QUERY_INVALID")
        statement = select(Schedule).where(Schedule.blogger_id == blogger_id)
        if status:
            status = self._normalize_status(status)
            statement = statement.where(Schedule.status == status)
        elif not include_cancelled:
            # 取消是排期的可审计软删除语义；历史仍在数据库和事件表中保留，
            # 默认执行列表不再返回已取消记录。
            statement = statement.where(Schedule.status != "cancelled")
        if start_date is not None:
            statement = statement.where(Schedule.plan_date >= self._coerce_date(start_date))
        if end_date is not None:
            statement = statement.where(Schedule.plan_date <= self._coerce_date(end_date))
        return list(
            self.db.scalars(
                statement.order_by(Schedule.plan_date.asc(), Schedule.id.asc()).offset(offset).limit(limit)
            )
        )

    def update_schedule(
        self,
        blogger_id: int,
        schedule_id: int,
        changes: dict[str, Any] | None = None,
        *,
        commit: bool = True,
        **kwargs: Any,
    ) -> Schedule:
        schedule = self.get_schedule(blogger_id, schedule_id)
        if schedule.status != "pending":
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        if changes is None:
            values: dict[str, Any] = {}
        elif hasattr(changes, "model_dump"):
            values = dict(changes.model_dump(exclude_none=True))
        else:
            values = dict(changes)
        values.update(kwargs)
        requested_status = values.pop("status", None)
        if requested_status is not None:
            normalized_status = self._normalize_status(requested_status)
            if normalized_status == "cancelled":
                return self.cancel_schedule(blogger_id, schedule_id, commit=commit)
            if normalized_status != "pending":
                raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        allowed = {"plan_date", "platform", "content_type", "title"}
        if set(values).difference(allowed):
            raise ScheduleServiceError("SCHEDULE_QUERY_INVALID")
        plan = self._coerce_date(values.get("plan_date", schedule.plan_date))
        platform = self._required_text(values.get("platform", schedule.platform), "platform")
        content_type = self._required_text(values.get("content_type", schedule.content_type), "content_type")
        title = self._required_text(values.get("title", schedule.title), "title")
        self._validate_frequency(blogger_id, plan, exclude_id=schedule.id)
        conflict = self.db.scalar(
            select(Schedule).where(
                Schedule.blogger_id == blogger_id,
                Schedule.output_id == schedule.output_id,
                Schedule.plan_date == plan,
                Schedule.platform == platform,
                Schedule.content_type == content_type,
                Schedule.id != schedule.id,
                Schedule.status != "cancelled",
            )
        )
        if conflict is not None:
            raise ScheduleServiceError("SCHEDULE_DUPLICATE")
        schedule.plan_date = plan
        schedule.platform = platform
        schedule.content_type = content_type
        schedule.title = title
        self._record_decision(
            blogger_id,
            "schedule_update",
            {"schedule_id": schedule.id, "changes": values},
            "用户编辑待发布排期",
        )
        if commit:
            self.db.commit()
            self.db.refresh(schedule)
        return schedule

    update = update_schedule

    def cancel_schedule(self, blogger_id: int, schedule_id: int, *, commit: bool = True) -> Schedule:
        schedule = self.get_schedule(blogger_id, schedule_id)
        if schedule.status == "cancelled":
            return schedule
        if schedule.status != "pending":
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        schedule.status = "cancelled"
        self._record_decision(
            blogger_id,
            "schedule_cancel",
            {"schedule_id": schedule.id},
            "用户取消待发布排期",
        )
        if commit:
            self.db.commit()
            self.db.refresh(schedule)
        return schedule

    cancel = cancel_schedule

    # ------------------------------------------------------------------
    # 模拟发布
    # ------------------------------------------------------------------
    def publish(
        self,
        blogger_id: int,
        schedule_id: int,
        idempotency_key: str = "default",
        *,
        commit: bool = True,
    ) -> Schedule:
        """把待发布排期变为已发布并写入一次发布事件。

        这里明确不调用平台；``PublishEvent.status``只表示本地模拟发布结果。
        """

        schedule = self.get_schedule(blogger_id, schedule_id)
        key = self._required_text(idempotency_key, "idempotency_key")
        event = self.db.scalar(
            select(PublishEvent).where(
                PublishEvent.schedule_id == schedule.id,
                PublishEvent.idempotency_key == key,
            )
        )
        if event is not None:
            return schedule
        if schedule.status == "published" or schedule.status == "collected":
            raise ScheduleServiceError("PUBLISH_DUPLICATE", status_code=409)
        if schedule.status != "pending":
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        when = self._now()
        try:
            schedule.status = "published"
            schedule.publish_time = when
            self.db.add(
                PublishEvent(
                    schedule_id=schedule.id,
                    status="published",
                    idempotency_key=key,
                    published_at=when,
                )
            )
            self._record_decision(
                blogger_id,
                "publish_simulated",
                {"schedule_id": schedule.id, "idempotency_key": key},
                "MVP模拟发布：仅本地记录状态，不调用真实平台",
            )
            if commit:
                self.db.commit()
                self.db.refresh(schedule)
            return schedule
        except IntegrityError as exc:
            self.db.rollback()
            existing = self.db.scalar(
                select(PublishEvent).where(
                    PublishEvent.schedule_id == schedule.id,
                    PublishEvent.idempotency_key == key,
                )
            )
            if existing is not None:
                return self.get_schedule(blogger_id, schedule_id)
            raise ScheduleServiceError("PUBLISH_PERSIST_FAILED", status_code=500) from exc
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise ScheduleServiceError("PUBLISH_PERSIST_FAILED", status_code=500) from exc

    publish_schedule = publish

    # ------------------------------------------------------------------
    # 可注入时钟的提醒扫描
    # ------------------------------------------------------------------
    def due_reminders(
        self,
        blogger_id: int | None = None,
        *,
        on_date: date | datetime | str | None = None,
        now: datetime | date | str | None = None,
        today: date | datetime | str | None = None,
        commit: bool = True,
    ) -> list[ReminderEvent]:
        """扫描当天的 pending 排期，每个排期每天最多写一条提醒事件。"""

        if blogger_id is not None:
            self._active_blogger(blogger_id)
        requested_date = on_date if on_date is not None else today if today is not None else now
        scan_date = self._coerce_date(requested_date if requested_date is not None else self._now())
        statement = (
            select(Schedule)
            .join(Blogger, Blogger.id == Schedule.blogger_id)
            .where(
                Blogger.deleted_at.is_(None),
                Schedule.status == "pending",
                Schedule.plan_date == scan_date,
            )
            .order_by(Schedule.plan_date.asc(), Schedule.id.asc())
        )
        if blogger_id is not None:
            statement = statement.where(Schedule.blogger_id == blogger_id)
        result: list[ReminderEvent] = []
        try:
            for schedule in self.db.scalars(statement):
                dedupe_key = self.make_reminder_key(schedule.id, scan_date)
                existing = self.db.scalar(
                    select(ReminderEvent).where(ReminderEvent.dedupe_key == dedupe_key)
                )
                if existing is not None:
                    continue
                event = ReminderEvent(
                    schedule_id=schedule.id,
                    reminder_date=scan_date,
                    status="pending",
                    dedupe_key=dedupe_key,
                )
                self.db.add(event)
                self.db.flush()
                result.append(event)
            if commit:
                self.db.commit()
                for event in result:
                    self.db.refresh(event)
            return result
        except IntegrityError:
            self.db.rollback()
            # 并发扫描只需返回已经存在的当天事件，保证调用幂等。
            return self._existing_reminders(blogger_id, scan_date)
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise ScheduleServiceError("REMINDER_PERSIST_FAILED", status_code=500) from exc

    scan_reminders = due_reminders
    trigger_reminders = due_reminders

    @staticmethod
    def make_reminder_key(schedule_id: int, reminder_date: date) -> str:
        return hashlib.sha256(f"schedule:{schedule_id}:{reminder_date.isoformat()}".encode()).hexdigest()

    # ------------------------------------------------------------------
    # 内部校验
    # ------------------------------------------------------------------
    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise ScheduleServiceError("BLOGGER_NOT_FOUND", status_code=404)
        return row

    def _get_output(self, blogger_id: int, output_id: int) -> Output:
        row = self.db.scalar(
            select(Output).where(
                Output.id == output_id,
                Output.blogger_id == blogger_id,
                Output.deleted_at.is_(None),
            )
        )
        if row is None:
            raise ScheduleServiceError("OUTPUT_NOT_FOUND", status_code=404)
        return row

    def _validate_frequency(self, blogger_id: int, plan: date, *, exclude_id: int | None = None) -> None:
        blogger = self._active_blogger(blogger_id)
        frequency = str(blogger.frequency or "").strip().lower()
        if not frequency or "不定期" in frequency or "irregular" in frequency:
            return
        active = select(Schedule).where(
            Schedule.blogger_id == blogger_id,
            Schedule.status == "pending",
        )
        if exclude_id is not None:
            active = active.where(Schedule.id != exclude_id)
        rows = list(self.db.scalars(active))
        if any(row.plan_date == plan for row in rows):
            # 日更允许每天一条；一周/一月频率也不应同一天重复。
            raise ScheduleServiceError("SCHEDULE_FREQUENCY_MISMATCH")
        if frequency in self._DAILY:
            return
        number_match = re.search(r"(\d+)\s*(?:条|篇|次)?", frequency)
        count = int(number_match.group(1)) if number_match else 1
        if frequency in self._WEEKLY or "周" in frequency or "week" in frequency:
            week_start = plan - timedelta(days=plan.weekday())
            week_end = week_start + timedelta(days=6)
            same_week = [row for row in rows if week_start <= row.plan_date <= week_end]
            if len(same_week) >= count:
                raise ScheduleServiceError("SCHEDULE_FREQUENCY_MISMATCH")
            return
        if frequency in self._MONTHLY or "月" in frequency or "month" in frequency:
            same_month = [row for row in rows if row.plan_date.year == plan.year and row.plan_date.month == plan.month]
            if len(same_month) >= count:
                raise ScheduleServiceError("SCHEDULE_FREQUENCY_MISMATCH")

    def _existing_reminders(self, blogger_id: int | None, scan_date: date) -> list[ReminderEvent]:
        statement = (
            select(ReminderEvent)
            .join(Schedule, Schedule.id == ReminderEvent.schedule_id)
            .where(ReminderEvent.reminder_date == scan_date)
            .order_by(ReminderEvent.schedule_id)
        )
        if blogger_id is not None:
            statement = statement.where(Schedule.blogger_id == blogger_id)
        return list(self.db.scalars(statement))

    def _record_decision(self, blogger_id: int, decision_type: str, decision: dict[str, Any], reason: str) -> None:
        self.db.add(
            DecisionLog(
                blogger_id=blogger_id,
                decision_type=decision_type,
                prompt_version="phase3-schedule-v1",
                input_summary=json.dumps(decision, ensure_ascii=False, default=str),
                decision=json.dumps(decision, ensure_ascii=False, default=str),
                reason=reason,
            )
        )
        self.db.flush()

    def _now(self) -> datetime:
        value = self.clock()
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        raise ScheduleServiceError("SCHEDULE_CLOCK_INVALID")

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ScheduleServiceError("SCHEDULE_REQUIRED_FIELD", detail=field)
        return text

    @classmethod
    def _normalize_status(cls, status: str) -> str:
        aliases = {"待发布": "pending", "已发布": "published", "已回收数据": "collected", "已取消": "cancelled"}
        normalized = aliases.get(str(status).strip(), str(status).strip().lower())
        if normalized not in cls.VALID_STATUSES:
            raise ScheduleServiceError("SCHEDULE_INVALID_STATE")
        return normalized

    @staticmethod
    def _coerce_date(value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ScheduleServiceError("SCHEDULE_DATE_INVALID") from exc

    @staticmethod
    def _coerce_datetime(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScheduleServiceError("SCHEDULE_DATE_INVALID") from exc

    def list(self, blogger_id: int, **filters: Any) -> list[Schedule]:
        """兼容第一阶段服务的简短列表方法名。"""

        return self.list_schedules(blogger_id, **filters)


ScheduleError = ScheduleServiceError

__all__ = ["ScheduleError", "ScheduleService", "ScheduleServiceError"]

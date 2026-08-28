"""排期、提醒和模拟发布服务测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import Blogger, Output, PublishEvent, ReminderEvent
from app.services.schedule_service import ScheduleService, ScheduleServiceError


def _blogger(db, *, name: str, frequency: str = "日更") -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["短视频"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        frequency=frequency,
        profile_state="complete",
    )
    db.add(row)
    db.flush()
    return row


def _output(db, blogger_id: int, *, title: str = "贵州美食脚本") -> Output:
    row = Output(
        blogger_id=blogger_id,
        type="script",
        category="美食",
        title=title,
        content_json='{"category":"美食","title":"贵州美食脚本"}',
        status="succeeded",
        version=1,
        manual_locked=False,
        prompt_version="phase3-test",
        model_name="fake",
    )
    db.add(row)
    db.flush()
    return row


def test_schedule_frequency_state_machine_and_isolation(db) -> None:
    owner = _blogger(db, name="博主A", frequency="周更")
    other = _blogger(db, name="博主B", frequency="日更")
    output = _output(db, owner.id)
    other_output = _output(db, other.id, title="博主B脚本")
    service = ScheduleService(db)

    first = service.create_schedule(owner.id, output.id, date(2026, 9, 1), "抖音", "视频")
    assert first.status == "pending"
    assert service.create_schedule(owner.id, output.id, date(2026, 9, 1), "抖音", "视频").id == first.id
    with pytest.raises(ScheduleServiceError) as duplicate_week:
        service.create_schedule(owner.id, output.id, date(2026, 9, 3), "小红书", "图文")
    assert duplicate_week.value.code == "SCHEDULE_FREQUENCY_MISMATCH"

    edited = service.update_schedule(owner.id, first.id, {"title": "更新后的标题"})
    assert edited.title == "更新后的标题"
    with pytest.raises(ScheduleServiceError) as foreign:
        service.get_schedule(other.id, first.id)
    assert foreign.value.status_code == 404
    second = service.create_schedule(other.id, other_output.id, date(2026, 9, 1), "抖音", "视频")
    assert service.list_schedules(other.id) == [second]

    cancelled = service.cancel_schedule(owner.id, first.id)
    assert cancelled.status == "cancelled"
    assert service.cancel_schedule(owner.id, first.id).status == "cancelled"
    with pytest.raises(ScheduleServiceError) as invalid:
        service.publish(owner.id, first.id, "publish-cancelled")
    assert invalid.value.code == "SCHEDULE_INVALID_STATE"


def test_reminder_scan_is_clock_injected_and_deduplicated(db) -> None:
    owner = _blogger(db, name="博主A", frequency="日更")
    output = _output(db, owner.id)
    service = ScheduleService(db, clock=lambda: datetime(2026, 9, 2, 9, 0))
    schedule = service.create_schedule(owner.id, output.id, date(2026, 9, 2), "抖音", "视频")

    first = service.due_reminders(owner.id)
    second = service.due_reminders(owner.id)
    assert len(first) == 1
    assert first[0].schedule_id == schedule.id
    assert second == []
    assert db.query(ReminderEvent).count() == 1
    third = service.due_reminders(owner.id, on_date=date(2026, 9, 3))
    fourth = service.due_reminders(owner.id, on_date=date(2026, 9, 3))
    assert third == []
    assert fourth == []


def test_simulated_publish_is_idempotent_and_records_event(db) -> None:
    owner = _blogger(db, name="博主A", frequency="日更")
    output = _output(db, owner.id)
    now = datetime(2026, 9, 2, 10, 0)
    service = ScheduleService(db, clock=lambda: now)
    schedule = service.create_schedule(owner.id, output.id, date(2026, 9, 2), "抖音", "视频")

    published = service.publish(owner.id, schedule.id, "publish-1")
    repeated = service.publish(owner.id, schedule.id, "publish-1")
    assert published.id == repeated.id
    assert published.status == "published"
    assert published.publish_time == now
    assert db.query(PublishEvent).count() == 1
    with pytest.raises(ScheduleServiceError) as duplicate:
        service.publish(owner.id, schedule.id, "publish-2")
    assert duplicate.value.code == "PUBLISH_DUPLICATE"


def test_schedule_rejects_invalid_dates_and_frequency_limits(db) -> None:
    owner = _blogger(db, name="博主A", frequency="月更")
    output1 = _output(db, owner.id, title="脚本1")
    output2 = _output(db, owner.id, title="脚本2")
    service = ScheduleService(db)
    service.create_schedule(owner.id, output1.id, "2026-09-01", "抖音", "视频")
    with pytest.raises(ScheduleServiceError) as limit:
        service.create_schedule(owner.id, output2.id, "2026-09-20", "抖音", "视频")
    assert limit.value.code == "SCHEDULE_FREQUENCY_MISMATCH"
    with pytest.raises(ScheduleServiceError) as bad_date:
        service.create_schedule(owner.id, output2.id, "not-a-date", "抖音", "视频")
    assert bad_date.value.code == "SCHEDULE_DATE_INVALID"

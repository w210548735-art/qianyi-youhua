"""手工/模拟指标回收服务测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import Blogger, CollectionJob, Metric, Output, Schedule
from app.services.collection_service import CollectionService, CollectionServiceError
from app.services.schedule_service import ScheduleService


def _blogger(db, *, name: str) -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["短视频"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        frequency="日更",
        profile_state="complete",
    )
    db.add(row)
    db.flush()
    return row


def _schedule(db, blogger_id: int, *, title: str) -> int:
    output = Output(
        blogger_id=blogger_id,
        type="script",
        category="美食",
        title=title,
        content_json="{}",
        status="succeeded",
        version=1,
        manual_locked=False,
        prompt_version="phase3-test",
        model_name="fake",
    )
    db.add(output)
    db.flush()
    schedule = ScheduleService(db).create_schedule(
        blogger_id, output.id, date(2026, 9, 1), "抖音", "视频"
    )
    ScheduleService(db, clock=lambda: datetime(2026, 9, 1, 10, 0)).publish(
        blogger_id, schedule.id, f"publish-{blogger_id}-{output.id}"
    )
    return schedule.id


def test_manual_collection_records_nonnegative_metric_and_is_idempotent(db) -> None:
    owner = _blogger(db, name="博主A")
    schedule_id = _schedule(db, owner.id, title="脚本A")
    service = CollectionService(db, clock=lambda: datetime(2026, 9, 2, 10, 0))

    job = service.start_collection(owner.id, schedule_id, "collect-1", source_type="manual")
    completed = service.execute_collection(
        job.id,
        {"views": 100, "likes": 20, "comments": 3, "collects": 9},
        blogger_id=owner.id,
    )
    assert completed.status == "succeeded"
    metric = db.query(Metric).one()
    assert (metric.views, metric.likes, metric.comments, metric.collects) == (100, 20, 3, 9)
    assert metric.source_type == "manual"
    assert service.start_collection(owner.id, schedule_id, "collect-1").id == job.id
    assert service.execute_collection(job.id, {"views": 1}, blogger_id=owner.id).id == job.id
    assert db.query(Metric).count() == 1


def test_collection_requires_published_schedule_and_isolates_bloggers(db) -> None:
    owner = _blogger(db, name="博主A")
    other = _blogger(db, name="博主B")
    owner_schedule = _schedule(db, owner.id, title="脚本A")
    other_schedule = _schedule(db, other.id, title="脚本B")
    service = CollectionService(db)

    with pytest.raises(CollectionServiceError) as foreign:
        service.start_collection(other.id, owner_schedule, "foreign")
    assert foreign.value.status_code == 404
    foreign_job = service.start_collection(other.id, other_schedule, "other")
    with pytest.raises(CollectionServiceError) as wrong_job:
        service.get_collection_job(owner.id, foreign_job.id)
    assert wrong_job.value.status_code == 404


def test_collection_failure_keeps_schedule_published_and_retry_succeeds(db) -> None:
    owner = _blogger(db, name="博主A")
    schedule_id = _schedule(db, owner.id, title="脚本A")
    calls = {"count": 0}

    def provider(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("模拟采集失败")
        return {"views": 12, "likes": 2, "comments": 1, "collects": 0}

    service = CollectionService(db, provider=provider)
    job = service.start_collection(owner.id, schedule_id, "retry-1")
    with pytest.raises(CollectionServiceError) as failed:
        service.execute_collection(job.id, blogger_id=owner.id)
    assert failed.value.code == "COLLECTION_FAILED"
    assert db.get(CollectionJob, job.id).status == "failed"
    assert db.get(Schedule, schedule_id).status == "published"
    recovered = service.retry_collection(owner.id, job.id)
    assert recovered.status == "succeeded"
    assert db.query(Metric).count() == 1


def test_collection_rejects_negative_and_platform_metrics(db) -> None:
    owner = _blogger(db, name="博主A")
    schedule_id = _schedule(db, owner.id, title="脚本A")
    service = CollectionService(db)
    job = service.start_collection(owner.id, schedule_id, "invalid-1")
    with pytest.raises(CollectionServiceError) as negative:
        service.execute_collection(job.id, {"views": -1}, blogger_id=owner.id)
    assert negative.value.code == "COLLECTION_METRIC_INVALID"
    assert db.query(Metric).count() == 0
    with pytest.raises(CollectionServiceError) as platform:
        service.start_collection(owner.id, schedule_id, "platform-1", source_type="platform")
    assert platform.value.code == "COLLECTION_SOURCE_INVALID"

"""手工/模拟指标回收服务测试。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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


def test_metric_idempotency_is_scoped_to_schedule_and_allows_cross_blogger_reuse(db) -> None:
    owner = _blogger(db, name="博主A")
    other = _blogger(db, name="博主B")
    first_schedule = _schedule(db, owner.id, title="脚本A1")
    second_schedule = _schedule(db, owner.id, title="脚本A2")
    other_schedule = _schedule(db, other.id, title="脚本B1")
    service = CollectionService(db)

    for blogger_id, schedule_id, views in (
        (owner.id, first_schedule, 11),
        (owner.id, second_schedule, 22),
        (other.id, other_schedule, 33),
    ):
        job = service.start_collection(
            blogger_id, schedule_id, "shared-collection-key", source_type="manual"
        )
        completed = service.execute_collection(
            job.id,
            {"source_type": "manual", "views": views},
            blogger_id=blogger_id,
        )
        assert completed.status == "succeeded"

    assert db.query(Metric).count() == 3
    assert sorted(metric.views for metric in db.query(Metric).all()) == [11, 22, 33]
    duplicate = service.start_collection(
        owner.id, first_schedule, "shared-collection-key", source_type="manual"
    )
    assert service.execute_collection(duplicate.id, blogger_id=owner.id).id == duplicate.id
    assert db.query(Metric).count() == 3


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


def test_database_flush_failure_rolls_back_persists_failed_job_and_can_retry(
    db, monkeypatch
) -> None:
    owner = _blogger(db, name="博主A")
    schedule_id = _schedule(db, owner.id, title="脚本A")
    service = CollectionService(db)
    job = service.start_collection(owner.id, schedule_id, "db-retry-key", source_type="manual")
    stable_job_id = job.id
    original_normalise = service._normalise_metrics
    monkeypatch.setattr(
        service,
        "_normalise_metrics",
        lambda _payload: {"views": -1, "likes": 0, "comments": 0, "collects": 0},
    )
    with pytest.raises(CollectionServiceError) as failed:
        service.execute_collection(
            stable_job_id,
            {"source_type": "manual", "views": 9},
            blogger_id=owner.id,
        )
    assert failed.value.code == "COLLECTION_PERSIST_FAILED"
    failed_job = db.get(CollectionJob, stable_job_id)
    assert failed_job is not None
    assert failed_job.status == "failed"
    assert failed_job.error_code == "COLLECTION_PERSIST_FAILED"
    assert "CHECK constraint failed" in (failed_job.error_message or "")
    assert db.get(Schedule, schedule_id).status == "published"
    assert db.query(Metric).count() == 0

    monkeypatch.setattr(service, "_normalise_metrics", original_normalise)
    recovered = service.retry_collection(
        owner.id,
        stable_job_id,
        {"source_type": "manual", "views": 9},
    )
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


def test_manual_collection_persists_confirmed_commercial_values_and_null_semantics(db) -> None:
    owner = _blogger(db, name="商业反馈博主")
    first_schedule = _schedule(db, owner.id, title="商业脚本")
    second_schedule = _schedule(db, owner.id, title="仅流量脚本")
    service = CollectionService(db)

    first_job = service.start_collection(
        owner.id, first_schedule, "manual-commercial", source_type="manual"
    )
    service.execute_collection(
        first_job.id,
        {
            "source_type": "manual",
            "views": 1000,
            "shares": 12,
            "actual_revenue": "500.50",
            "actual_cost": "200.25",
            "user_confirmed": True,
            "collected_at": "2026-09-04T12:30:00",
        },
        blogger_id=owner.id,
    )
    second_job = service.start_collection(
        owner.id, second_schedule, "manual-traffic-only", source_type="manual"
    )
    service.execute_collection(
        second_job.id,
        {"source_type": "manual", "views": 100, "shares": 2},
        blogger_id=owner.id,
    )

    commercial = db.query(Metric).filter(Metric.schedule_id == first_schedule).one()
    assert commercial.shares == 12
    assert commercial.actual_revenue == Decimal("500.50")
    assert commercial.actual_cost == Decimal("200.25")
    assert commercial.user_confirmed is True
    traffic_only = db.query(Metric).filter(Metric.schedule_id == second_schedule).one()
    assert traffic_only.actual_revenue is None
    assert traffic_only.actual_cost is None


def test_simulated_or_unconfirmed_collection_cannot_persist_actual_money(db) -> None:
    owner = _blogger(db, name="模拟边界博主")
    simulated_schedule = _schedule(db, owner.id, title="模拟脚本")
    manual_schedule = _schedule(db, owner.id, title="未确认脚本")
    service = CollectionService(db)

    simulated = service.start_collection(owner.id, simulated_schedule, "sim-money")
    with pytest.raises(CollectionServiceError) as simulated_error:
        service.execute_collection(
            simulated.id,
            {"source_type": "simulated", "actual_revenue": 100, "user_confirmed": True},
            blogger_id=owner.id,
        )
    assert simulated_error.value.code == "COLLECTION_METRIC_INVALID"

    manual = service.start_collection(
        owner.id, manual_schedule, "manual-unconfirmed", source_type="manual"
    )
    with pytest.raises(CollectionServiceError) as unconfirmed_error:
        service.execute_collection(
            manual.id,
            {"source_type": "manual", "actual_cost": 10, "user_confirmed": False},
            blogger_id=owner.id,
        )
    assert unconfirmed_error.value.code == "COLLECTION_METRIC_INVALID"
    assert db.query(Metric).count() == 0

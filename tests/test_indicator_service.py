from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Blogger, IndicatorObservation, Metric, OperationalIndicator, Output, Report, Schedule
from app.services.indicator_service import IndicatorService, IndicatorServiceError


def _blogger(db, name: str = "指标博主") -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        profile_state="complete",
    )
    db.add(row)
    db.flush()
    return row


def _output_metric(
    db,
    blogger_id: int,
    *,
    key: str,
    views: int,
    likes: int = 0,
    shares: int = 0,
    revenue: float | None = None,
    cost: float | None = None,
    confirmed: bool = False,
    source_type: str = "manual",
    at: datetime | None = None,
) -> Metric:
    output = Output(
        blogger_id=blogger_id,
        type="script",
        category="美食",
        title=key,
        content_json="{}",
        status="succeeded",
        version=1,
    )
    db.add(output)
    db.flush()
    schedule = Schedule(
        blogger_id=blogger_id,
        output_id=output.id,
        plan_date=date.today(),
        platform="抖音",
        content_type="视频",
        title=key,
        status="published",
    )
    db.add(schedule)
    db.flush()
    metric = Metric(
        output_id=output.id,
        schedule_id=schedule.id,
        source_type=source_type,
        views=views,
        likes=likes,
        comments=0,
        collects=0,
        shares=shares,
        actual_revenue=revenue,
        actual_cost=cost,
        user_confirmed=confirmed,
        idempotency_key=key,
        collected_at=at or datetime.utcnow(),
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


def test_default_indicators_are_whitelisted_and_formula_injection_is_rejected(db) -> None:
    blogger = _blogger(db)
    service = IndicatorService(db)

    rows = service.initialize_defaults(blogger.id)
    assert len(rows) == 11
    assert {row.formula_key for row in rows} == set(service.formula_registry)
    assert {row.direction for row in rows} <= {"higher_better", "lower_better", "neutral"}
    assert len(service.initialize_defaults(blogger.id)) == 11
    assert db.scalar(select(func.count()).select_from(OperationalIndicator)) == 11

    with pytest.raises(IndicatorServiceError, match="INDICATOR_FORMULA_NOT_ALLOWED"):
        service.create_indicator(
            blogger.id,
            formula_key="__import__('os').system('x')",
            category="money",
            name="注入",
            meaning="禁止",
        )


def test_traffic_and_actual_money_formulas_preserve_null_and_manual_boundary(db) -> None:
    blogger = _blogger(db)
    now = datetime.utcnow()
    _output_metric(
        db, blogger.id, key="confirmed", views=100, likes=10, shares=5, revenue=500, cost=200, confirmed=True, at=now
    )
    _output_metric(db, blogger.id, key="traffic-only", views=50, likes=5, source_type="simulated", at=now)
    service = IndicatorService(db)
    rows = {row.formula_key: row for row in service.initialize_defaults(blogger.id)}

    assert service.evaluate(rows["traffic_views"], now).value == 150
    assert service.evaluate(rows["traffic_engagement_rate"], now).value == pytest.approx(20 / 150)
    assert service.evaluate(rows["money_actual_revenue"], now).value == 500
    assert service.evaluate(rows["money_actual_cost"], now).value == 200
    assert service.evaluate(rows["money_actual_net"], now).value == 300
    assert service.evaluate(rows["money_roi"], now).value == 1.5

    other = _blogger(db, "空商业博主")
    _output_metric(db, other.id, key="null", views=0, confirmed=False, at=now)
    other_rows = {row.formula_key: row for row in service.initialize_defaults(other.id)}
    assert service.evaluate(other_rows["money_actual_net"], now).status == "data_insufficient"
    assert service.evaluate(other_rows["money_actual_net"], now).value is None
    assert service.evaluate(other_rows["traffic_engagement_rate"], now).value is None


def test_trend_requires_two_windows_and_zero_denominator_is_insufficient(db) -> None:
    blogger = _blogger(db)
    now = datetime.utcnow()
    _output_metric(db, blogger.id, key="old", views=100, at=now - timedelta(days=10))
    _output_metric(db, blogger.id, key="new", views=150, at=now - timedelta(days=2))
    service = IndicatorService(db)
    indicator = next(row for row in service.initialize_defaults(blogger.id) if row.formula_key == "traffic_views_trend")
    result = service.evaluate(indicator, now)
    assert result.status == "ok" and result.value == 0.5

    empty = _blogger(db, "不足")
    empty_indicator = next(
        row for row in service.initialize_defaults(empty.id) if row.formula_key == "traffic_views_trend"
    )
    assert service.evaluate(empty_indicator, now).status == "data_insufficient"


def test_observations_are_append_only_and_report_scoped_recompute_is_idempotent(db) -> None:
    blogger = _blogger(db)
    _output_metric(db, blogger.id, key="metric", views=10)
    service = IndicatorService(db)
    indicator = next(row for row in service.initialize_defaults(blogger.id) if row.formula_key == "traffic_views")
    first = service.recompute(blogger.id, indicator_id=indicator.id)[0]
    second = service.recompute(blogger.id, indicator_id=indicator.id)[0]
    assert first.id != second.id

    report = Report(
        blogger_id=blogger.id,
        status="running",
        idempotency_key="indicator-report",
        snapshot_json="{}",
        snapshot_hash="a" * 64,
    )
    db.add(report)
    db.commit()
    scoped_first = service.recompute(blogger.id, indicator_id=indicator.id, report_id=report.id)[0]
    scoped_second = service.recompute(blogger.id, indicator_id=indicator.id, report_id=report.id)[0]
    assert scoped_first.id == scoped_second.id
    request_first = service.recompute(
        blogger.id,
        indicator_id=indicator.id,
        idempotency_key="standalone-recompute",
    )[0]
    request_second = service.recompute(
        blogger.id,
        indicator_id=indicator.id,
        idempotency_key="standalone-recompute",
    )[0]
    assert request_first.id == request_second.id
    assert db.scalar(select(func.count()).select_from(IndicatorObservation)) == 4


def test_indicator_reads_are_blogger_isolated_and_deactivation_keeps_history(db) -> None:
    owner = _blogger(db, "甲")
    other = _blogger(db, "乙")
    service = IndicatorService(db)
    indicator = service.initialize_defaults(owner.id)[0]
    service.recompute(owner.id, indicator_id=indicator.id)
    service.deactivate_indicator(owner.id, indicator.id)

    assert service.get_history(owner.id, indicator.id)
    assert indicator not in service.list_indicators(owner.id, active=True)
    with pytest.raises(IndicatorServiceError, match="INDICATOR_NOT_FOUND"):
        service.get_indicator(other.id, indicator.id)

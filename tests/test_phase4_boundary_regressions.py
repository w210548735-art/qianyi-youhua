from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.report_routes import get_report_service
from app.db.session import get_db
from app.main import app
from app.models import (
    Blogger,
    FeedbackRun,
    Metric,
    Output,
    Place,
    PlaceCommercialRevision,
    Schedule,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.feedback_service import FeedbackService, FeedbackServiceError
from app.services.indicator_service import IndicatorService
from app.services.memory_service import MemoryService
from app.services.report_agent import FakeReportAgent
from app.services.report_data_service import ReportDataService
from app.services.report_service import ReportService
from app.services.route_service import RouteService
from app.services.task_memory_service import TaskMemoryService


class StableAnalysis:
    def build_snapshot(self, *_args, **_kwargs):
        return {"snapshot_hash": "phase4-boundary"}


def _blogger(db, name: str) -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万以下",
        monetization_types_json='["探店"]',
        profile_state="complete",
    )
    db.add(row)
    db.flush()
    return row


def _metric(
    db,
    blogger_id: int,
    *,
    key: str,
    source_type: str,
    views: int,
    likes: int = 0,
    collected_at: datetime | None = None,
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
        status="collected",
    )
    db.add(schedule)
    db.flush()
    row = Metric(
        output_id=output.id,
        schedule_id=schedule.id,
        source_type=source_type,
        views=views,
        likes=likes,
        comments=0,
        collects=0,
        shares=0,
        user_confirmed=source_type == "manual",
        idempotency_key=key,
        collected_at=collected_at or datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _place(
    db,
    blogger_id: int,
    *,
    name: str,
    source_type: str,
    credibility: int,
    origin: str,
    manual_locked: bool,
) -> Place:
    row = Place(
        blogger_id=blogger_id,
        name=name,
        category="美食",
        tags_json="[]",
        source_type=source_type,
        credibility=credibility,
        origin=origin,
        manual_locked=manual_locked,
        dedupe_key=f"boundary-{blogger_id}-{name}",
        est_cost=100,
        est_benefit=1000,
        like_level=4,
        fits_koc=True,
        fits_shoot=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_report_simulated_only_is_never_actual(db) -> None:
    blogger = _blogger(db, "纯模拟流量")
    simulated = _metric(db, blogger.id, key="sim-only", source_type="simulated", views=900, likes=90)

    snapshot = ReportDataService(db).build_snapshot(blogger.id)

    assert snapshot["facts"]["traffic"]["status"] == "simulation_only"
    assert snapshot["facts"]["traffic"]["views"] is None
    assert snapshot["facts"]["traffic"]["source_refs"] == []
    assert snapshot["facts"]["traffic"]["simulation_preview"]["views"] == 900
    assert snapshot["charts"]["traffic_line"]["status"] == "simulation_only"
    assert snapshot["charts"]["traffic_line"]["points"][0]["source_refs"] == [f"metric:{simulated.id}"]


def test_report_mixed_metrics_excludes_simulated_from_actual_totals(db) -> None:
    blogger = _blogger(db, "混合流量")
    manual = _metric(db, blogger.id, key="manual", source_type="manual", views=100, likes=10)
    simulated = _metric(db, blogger.id, key="simulated", source_type="simulated", views=900, likes=900)

    snapshot = ReportDataService(db).build_snapshot(blogger.id)
    traffic = snapshot["facts"]["traffic"]
    chart = snapshot["charts"]["traffic_line"]

    assert traffic["status"] == "actual" and traffic["views"] == 100
    assert traffic["engagement_rate"] == 0.1
    assert traffic["source_refs"] == [f"metric:{manual.id}"]
    assert {ref for point in chart["points"] for ref in point["source_refs"]} == {f"metric:{manual.id}"}
    assert snapshot["charts"]["traffic_simulation_preview"]["status"] == "simulation_only"
    assert snapshot["charts"]["traffic_simulation_preview"]["points"][0]["source_refs"] == [
        f"metric:{simulated.id}"
    ]
    assert snapshot["data_quality"]["simulated_excluded_from_actual_traffic"] == 1


def test_report_api_exposes_simulation_only_without_actual_label(db, tmp_path) -> None:
    blogger = _blogger(db, "模拟报告API")
    _metric(db, blogger.id, key="sim-api", source_type="simulated", views=700, likes=70)
    IndicatorService(db).initialize_defaults(blogger.id)
    service = ReportService(
        db,
        agent=FakeReportAgent(),
        task_service=TaskMemoryService(db, tmp_path / "report-api-tasks"),
    )

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_report_service] = lambda: service
    try:
        response = TestClient(app).post(
            f"/api/v1/bloggers/{blogger.id}/reports",
            json={"idempotency_key": "simulated-report-api", "user_instruction": "仅展示来源边界"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["conclusion"]["traffic"]["status"] == "simulation_only"
        traffic_chart = next(chart for chart in payload["charts"] if chart["type"] == "line")
        assert traffic_chart["status"] == "simulation_only"
        assert "模拟" in traffic_chart["title"] and "实际流量" in traffic_chart["title"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_report_service, None)


def test_indicator_real_traffic_excludes_simulated(db) -> None:
    blogger = _blogger(db, "指标边界")
    now = datetime.utcnow()
    manual = _metric(
        db,
        blogger.id,
        key="indicator-manual",
        source_type="manual",
        views=100,
        likes=10,
        collected_at=now,
    )
    simulated = _metric(
        db,
        blogger.id,
        key="indicator-simulated",
        source_type="simulated",
        views=900,
        likes=900,
        collected_at=now,
    )
    service = IndicatorService(db)
    indicators = {row.formula_key: row for row in service.initialize_defaults(blogger.id)}

    views = service.evaluate(indicators["traffic_views"], now)
    engagement = service.evaluate(indicators["traffic_engagement_rate"], now)
    assert views.value == 100 and views.evidence["metric_ids"] == [manual.id]
    assert views.evidence["simulated_excluded_ids"] == [simulated.id]
    assert engagement.value == 0.1

    simulated_only = _blogger(db, "仅模拟指标")
    only = _metric(
        db,
        simulated_only.id,
        key="indicator-only-sim",
        source_type="simulated",
        views=50,
        collected_at=now,
    )
    only_indicators = {row.formula_key: row for row in service.initialize_defaults(simulated_only.id)}
    result = service.evaluate(only_indicators["traffic_views"], now)
    assert result.status == "data_insufficient" and result.value is None
    assert result.evidence["simulated_available"] is True
    assert result.evidence["simulated_excluded_ids"] == [only.id]


def test_indicator_manual_trend_and_zero_denominator_keep_real_source_boundary(db) -> None:
    blogger = _blogger(db, "趋势边界")
    now = datetime.utcnow()
    _metric(db, blogger.id, key="old-manual", source_type="manual", views=100, collected_at=now - timedelta(days=10))
    _metric(db, blogger.id, key="new-manual", source_type="manual", views=150, collected_at=now - timedelta(days=2))
    _metric(db, blogger.id, key="new-sim", source_type="simulated", views=9999, collected_at=now - timedelta(days=2))
    service = IndicatorService(db)
    indicators = {row.formula_key: row for row in service.initialize_defaults(blogger.id)}
    trend = service.evaluate(indicators["traffic_views_trend"], now)
    assert trend.status == "ok" and trend.value == 0.5
    assert trend.evidence["current_7d_views"] == 150

    zero = _blogger(db, "零分母")
    _metric(db, zero.id, key="zero-manual", source_type="manual", views=0, likes=1, collected_at=now)
    zero_indicators = {row.formula_key: row for row in service.initialize_defaults(zero.id)}
    rate = service.evaluate(zero_indicators["traffic_engagement_rate"], now)
    assert rate.status == "data_insufficient" and rate.value is None


def test_untrusted_place_nonnull_values_are_not_estimated_money(db) -> None:
    blogger = _blogger(db, "低可信地点")
    place = _place(
        db,
        blogger.id,
        name="模型生成店",
        source_type="generated",
        credibility=1,
        origin="generated",
        manual_locked=False,
    )

    snapshot = ReportDataService(db).build_snapshot(blogger.id)

    assert snapshot["facts"]["money"]["status"] == "data_insufficient"
    assert snapshot["facts"]["money"]["source_refs"] == []
    assert snapshot["data_quality"]["untrusted_commercial_place_ids"] == [place.id]


def test_trusted_place_values_are_estimated_not_actual(db) -> None:
    blogger = _blogger(db, "可信地点")
    place = _place(
        db,
        blogger.id,
        name="官方种子店",
        source_type="official",
        credibility=4,
        origin="seed",
        manual_locked=False,
    )

    snapshot = ReportDataService(db).build_snapshot(blogger.id)

    assert snapshot["facts"]["money"]["status"] == "estimated"
    assert snapshot["facts"]["money"]["net"] == 900
    assert snapshot["facts"]["money"]["source_refs"] == [f"place:{place.id}"]


def _feedback_revision(db, blogger: Blogger, place: Place, *, key: str) -> tuple[FeedbackRun, PlaceCommercialRevision]:
    metric = _metric(db, blogger.id, key=f"{key}-metric", source_type="manual", views=100)
    run = FeedbackRun(
        blogger_id=blogger.id,
        output_id=metric.output_id,
        primary_metric_id=metric.id,
        status="analyzed",
        idempotency_key=key,
        snapshot_json="{}",
        snapshot_hash="phase4-boundary",
        analysis_json="{}",
        summary="用户确认地点商业字段",
        prompt_version="fake",
        model_name="fake",
    )
    db.add(run)
    db.flush()
    before = {
        "est_cost": place.est_cost,
        "est_benefit": place.est_benefit,
        "like_level": place.like_level,
        "fits_koc": place.fits_koc,
        "fits_shoot": place.fits_shoot,
    }
    revision = PlaceCommercialRevision(
        run_id=run.id,
        place_id=place.id,
        before_json=json.dumps(before),
        after_json=json.dumps({"simulation_only": False}),
        reason="用户逐字段确认",
        status="pending",
        version=1,
    )
    db.add(revision)
    db.commit()
    return run, revision


def _feedback_service(db) -> FeedbackService:
    embedding = FakeEmbeddingService()
    return FeedbackService(
        db,
        analysis_service=StableAnalysis(),
        embedding_service=embedding,
        memory_service=MemoryService(db, embedding=embedding),
    )


def test_confirmed_place_override_becomes_traceable_for_route_and_report(db) -> None:
    blogger = _blogger(db, "确认地点")
    place = _place(
        db,
        blogger.id,
        name="待确认店",
        source_type="generated",
        credibility=1,
        origin="generated",
        manual_locked=False,
    )
    run, revision = _feedback_revision(db, blogger, place, key="confirm-place-boundary")
    assert RouteService.missing_commercial_data([place])[0]["missing_fields"] == ["commercial_source"]

    service = _feedback_service(db)
    overrides = {
        place.id: {
            "est_cost": 120,
            "est_benefit": 800,
            "like_level": 5,
            "fits_koc": True,
            "fits_shoot": True,
        }
    }
    service.confirm(
        blogger.id,
        run.id,
        candidate_ids=[f"place_commercial:{revision.id}"],
        place_overrides=overrides,
    )
    db.refresh(place)
    db.refresh(revision)

    assert RouteService(db).missing_commercial_data([place]) == []
    snapshot = ReportDataService(db).build_snapshot(blogger.id)
    assert snapshot["facts"]["money"]["status"] == "estimated"
    evidence = next(row for row in snapshot["evidence"] if row["ref"] == f"place:{place.id}")
    assert evidence["snapshot"]["commercial_provenance"]["est_cost"]["revision_id"] == revision.id
    assert revision.status == "applied" and revision.confirmed_at is not None and revision.applied_at is not None


def test_rejected_or_failed_place_override_never_changes_trust_or_values(db, monkeypatch) -> None:
    blogger = _blogger(db, "拒绝地点")
    rejected_place = _place(
        db,
        blogger.id,
        name="拒绝店",
        source_type="generated",
        credibility=1,
        origin="generated",
        manual_locked=False,
    )
    rejected_run, rejected_revision = _feedback_revision(db, blogger, rejected_place, key="reject-place-boundary")
    service = _feedback_service(db)
    service.reject(blogger.id, rejected_run.id)
    db.refresh(rejected_revision)
    assert rejected_revision.status == "rejected"
    assert RouteService.missing_commercial_data([rejected_place])[0]["missing_fields"] == ["commercial_source"]

    failed_place = _place(
        db,
        blogger.id,
        name="失败店",
        source_type="generated",
        credibility=1,
        origin="generated",
        manual_locked=False,
    )
    failed_run, failed_revision = _feedback_revision(db, blogger, failed_place, key="failed-place-boundary")
    before = (failed_place.est_cost, failed_place.est_benefit)

    def fail_memory(*_args, **_kwargs):
        raise RuntimeError("注入确认事务失败")

    monkeypatch.setattr(service.memory_service, "sync_profile", fail_memory)
    with pytest.raises(FeedbackServiceError, match="FEEDBACK_APPLY_FAILED"):
        service.confirm(
            blogger.id,
            failed_run.id,
            candidate_ids=[f"place_commercial:{failed_revision.id}"],
            place_overrides={failed_place.id: {"est_cost": 200, "est_benefit": 700}},
        )
    db.refresh(failed_place)
    db.refresh(failed_revision)
    assert (failed_place.est_cost, failed_place.est_benefit) == before
    assert failed_revision.status == "pending"
    assert RouteService.missing_commercial_data([failed_place])[0]["missing_fields"] == ["commercial_source"]

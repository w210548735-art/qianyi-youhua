from __future__ import annotations

import time
from datetime import date, datetime

import pytest

from app.models import Blogger, FeedbackRun, Metric, Output, Place, Report, Schedule
from app.services.feedback_analysis_service import FeedbackAnalysisService
from app.services.feedback_service import FeedbackService
from app.services.indicator_service import IndicatorService
from app.services.report_data_service import ReportDataService
from app.services.report_service import ReportService
from app.services.route_service import RouteService

pytestmark = pytest.mark.performance


def _blogger(db) -> Blogger:
    row = Blogger(
        name="第四阶段性能博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
        profile_state="complete",
    )
    db.add(row)
    db.flush()
    return row


def _thousand_metrics(db, blogger_id: int) -> tuple[Output, Metric]:
    primary_output = None
    primary_metric = None
    for index in range(1000):
        output = Output(
            blogger_id=blogger_id,
            type="script",
            category="美食",
            title=f"性能内容{index}",
            content_json="{}",
            status="succeeded",
            version=1,
        )
        db.add(output)
        db.flush()
        schedule = Schedule(
            blogger_id=blogger_id,
            output_id=output.id,
            plan_date=date(2026, 8, 1),
            platform="抖音",
            content_type="视频",
            title=output.title,
            status="collected",
        )
        db.add(schedule)
        db.flush()
        metric = Metric(
            output_id=output.id,
            schedule_id=schedule.id,
            source_type="manual",
            views=index + 1,
            likes=index % 20,
            comments=1,
            collects=1,
            shares=1,
            user_confirmed=True,
            idempotency_key=f"performance-metric-{index}",
            collected_at=datetime(2026, 8, 1, 12),
        )
        db.add(metric)
        primary_output = output
        primary_metric = metric
    db.commit()
    assert primary_output is not None and primary_metric is not None
    return primary_output, primary_metric


def test_1000_metric_feedback_and_report_aggregation_under_one_second(db) -> None:
    blogger = _blogger(db)
    output, metric = _thousand_metrics(db, blogger.id)
    IndicatorService(db).initialize_defaults(blogger.id)

    started = time.perf_counter()
    snapshot = FeedbackAnalysisService(db).build_snapshot(blogger.id, output.id, metric.id)
    feedback_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    report_snapshot = ReportDataService(db).build_snapshot(blogger.id)
    report_elapsed = time.perf_counter() - started

    print(f"PHASE4_FEEDBACK_ANALYSIS_1000_SECONDS={feedback_elapsed:.6f}")
    print(f"PHASE4_REPORT_AGGREGATION_1000_SECONDS={report_elapsed:.6f}")
    assert len(snapshot["metric_history"]) == 1000
    assert report_snapshot["facts"]["traffic"]["views"] == 500500
    assert feedback_elapsed < 1.0 and report_elapsed < 1.0


def test_1000_feedback_report_lists_under_500ms_and_crud_under_300ms(db) -> None:
    blogger = _blogger(db)
    output, metric = _thousand_metrics(db, blogger.id)
    db.add_all(
        [
            FeedbackRun(
                blogger_id=blogger.id,
                output_id=output.id,
                primary_metric_id=metric.id,
                status="failed",
                idempotency_key=f"feedback-list-{index}",
                snapshot_json="{}",
                snapshot_hash=f"{index:064x}",
                prompt_version="performance",
                model_name="fake",
            )
            for index in range(1000)
        ]
    )
    db.add_all(
        [
            Report(
                blogger_id=blogger.id,
                status="failed",
                idempotency_key=f"report-list-{index}",
                snapshot_json="{}",
                snapshot_hash=f"{index:064x}",
                prompt_version="performance",
                model_name="fake",
            )
            for index in range(1000)
        ]
    )
    db.commit()
    feedback = FeedbackService(db)
    reports = ReportService(db)
    started = time.perf_counter()
    feedback_rows = feedback.list_runs(blogger.id, limit=1000)
    feedback_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    report_rows = reports.list_reports(blogger.id, limit=1000)
    report_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    loaded = feedback.get(blogger.id, feedback_rows[0].id)
    crud_elapsed = time.perf_counter() - started

    print(f"PHASE4_FEEDBACK_LIST_1000_SECONDS={feedback_elapsed:.6f}")
    print(f"PHASE4_REPORT_LIST_1000_SECONDS={report_elapsed:.6f}")
    print(f"PHASE4_FEEDBACK_CRUD_SECONDS={crud_elapsed:.6f}")
    assert len(feedback_rows) == len(report_rows) == 1000
    assert loaded.id == feedback_rows[0].id
    assert feedback_elapsed < 0.5 and report_elapsed < 0.5 and crud_elapsed < 0.3


def test_1000_place_commercial_policy_is_batched_and_under_one_second(db) -> None:
    blogger = _blogger(db)
    places = [
        Place(
            blogger_id=blogger.id,
            name=f"可信地点{index}",
            category="美食",
            tags_json="[]",
            source_type="official",
            credibility=4,
            origin="seed",
            manual_locked=False,
            dedupe_key=f"phase4-policy-{index}",
            est_cost=100,
            est_benefit=200,
            like_level=4,
            fits_koc=True,
            fits_shoot=True,
        )
        for index in range(1000)
    ]
    db.add_all(places)
    db.commit()

    started = time.perf_counter()
    missing = RouteService.missing_commercial_data(places)
    policy_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    snapshot = ReportDataService(db).build_snapshot(blogger.id)
    report_elapsed = time.perf_counter() - started

    print(f"PHASE4_PLACE_POLICY_1000_SECONDS={policy_elapsed:.6f}")
    print(f"PHASE4_PLACE_REPORT_1000_SECONDS={report_elapsed:.6f}")
    assert missing == []
    assert snapshot["facts"]["money"]["status"] == "estimated"
    assert policy_elapsed < 1.0 and report_elapsed < 1.0

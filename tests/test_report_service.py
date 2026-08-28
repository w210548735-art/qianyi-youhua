from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Blogger,
    DecisionLog,
    IndicatorObservation,
    MemoryRecord,
    Metric,
    Output,
    OutputPlace,
    Place,
    Report,
    ReportEvidence,
    Schedule,
    TaskCheckpoint,
    TaskSession,
)
from app.services.indicator_service import IndicatorService
from app.services.report_agent import FakeReportAgent, ReportAgentError
from app.services.report_data_service import ReportDataService
from app.services.report_service import ReportService, ReportServiceError
from app.services.task_memory_service import TaskMemoryService


def _blogger(db, name: str = "报告博主") -> Blogger:
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


def _metric(db, blogger_id: int, *, suffix: str, views: int, revenue: float, cost: float) -> tuple[Metric, Place]:
    output = Output(
        blogger_id=blogger_id,
        type="script",
        category="美食探店",
        title=f"脚本{suffix}",
        content_json="{}",
        status="succeeded",
        version=1,
    )
    place = Place(
        blogger_id=blogger_id,
        name=f"店铺{suffix}",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key=f"place-{blogger_id}-{suffix}",
    )
    db.add_all([output, place])
    db.flush()
    db.add(OutputPlace(output_id=output.id, place_id=place.id, role="primary", sequence=1, claim="明确关联"))
    schedule = Schedule(
        blogger_id=blogger_id,
        output_id=output.id,
        plan_date=date.today(),
        platform="抖音",
        content_type="视频",
        title=output.title,
        status="published",
    )
    db.add(schedule)
    db.flush()
    metric = Metric(
        output_id=output.id,
        schedule_id=schedule.id,
        source_type="manual",
        views=views,
        likes=10,
        comments=2,
        collects=3,
        shares=1,
        actual_revenue=revenue,
        actual_cost=cost,
        user_confirmed=True,
        idempotency_key=f"metric-{suffix}",
        collected_at=datetime.utcnow(),
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric, place


def _service(db, tmp_path: Path, agent: FakeReportAgent | None = None) -> ReportService:
    return ReportService(
        db,
        agent=agent or FakeReportAgent(),
        task_service=TaskMemoryService(db, tmp_path / "tasks"),
    )


def test_report_generation_persists_deterministic_facts_charts_evidence_and_candidate(db, tmp_path: Path) -> None:
    blogger = _blogger(db)
    metric, place = _metric(db, blogger.id, suffix="A", views=1000, revenue=500, cost=200)
    IndicatorService(db).initialize_defaults(blogger.id)
    service = _service(db, tmp_path)

    report = service.generate(blogger.id, "report-success")
    same = service.generate(blogger.id, "report-success")

    assert report.status == "succeeded" and same.id == report.id
    conclusion = json.loads(report.conclusion_json)
    assert conclusion["money"]["status"] == "actual"
    assert conclusion["money"]["net"] == 300
    charts = json.loads(report.charts_json)
    assert {chart["type"] for chart in charts} == {"line", "bar"}
    assert len(charts) == 4
    traffic_point = next(chart for chart in charts if chart["title"] == "流量趋势")["points"][0]
    assert traffic_point == {
        "label": metric.collected_at.isoformat(),
        "source_refs": [f"metric:{metric.id}"],
        "unit": "views",
        "value": 1000.0,
    }
    supplier = next(chart for chart in charts if chart["title"] == "地点确认净收益Top")
    assert supplier["points"][0]["label"] == place.name
    assert supplier["points"][0]["value"] == 300
    assert db.scalar(select(func.count()).select_from(ReportEvidence).where(ReportEvidence.report_id == report.id)) > 0
    assert (
        db.scalar(
            select(func.count()).select_from(IndicatorObservation).where(IndicatorObservation.report_id == report.id)
        )
        == 11
    )
    assert (
        db.scalar(select(func.count()).select_from(DecisionLog).where(DecisionLog.decision_type == "report_generation"))
        == 1
    )
    assert db.scalar(select(func.count()).select_from(MemoryRecord).where(MemoryRecord.status == "candidate")) == 1
    task = db.get(TaskSession, report.task_id)
    assert task is not None and task.status == "completed"
    assert db.scalar(select(func.count()).select_from(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)) >= 1


def test_report_money_estimated_and_data_insufficient_are_never_actual(db, tmp_path: Path) -> None:
    estimated_blogger = _blogger(db, "估算")
    place = Place(
        blogger_id=estimated_blogger.id,
        name="估算店",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        est_cost=100,
        est_benefit=150,
        origin="manual",
        manual_locked=True,
        dedupe_key="estimated-place",
    )
    db.add(place)
    db.commit()
    IndicatorService(db).initialize_defaults(estimated_blogger.id)
    estimated = _service(db, tmp_path).generate(estimated_blogger.id, "estimated-report")
    assert json.loads(estimated.conclusion_json)["money"]["status"] == "estimated"

    empty = _blogger(db, "空")
    db.commit()
    IndicatorService(db).initialize_defaults(empty.id)
    insufficient = _service(db, tmp_path).generate(empty.id, "insufficient-report")
    money = json.loads(insufficient.conclusion_json)["money"]
    assert money["status"] == "data_insufficient"
    assert money["revenue"] is None and money["cost"] is None and money["net"] is None


def test_report_agent_failure_has_no_half_evidence_and_retry_is_idempotent(db, tmp_path: Path) -> None:
    blogger = _blogger(db)
    _metric(db, blogger.id, suffix="A", views=10, revenue=5, cost=2)
    IndicatorService(db).initialize_defaults(blogger.id)
    failing = _service(
        db,
        tmp_path,
        FakeReportAgent(fail_with=ReportAgentError("REPORT_INVALID_JSON", "非法JSON")),
    )
    with pytest.raises(ReportServiceError, match="REPORT_INVALID_JSON"):
        failing.generate(blogger.id, "failed-report")
    report = db.scalar(select(Report).where(Report.idempotency_key == "failed-report"))
    assert report is not None and report.status == "failed"
    assert report.conclusion_json is None and report.charts_json is None
    assert db.scalar(select(func.count()).select_from(ReportEvidence).where(ReportEvidence.report_id == report.id)) == 0
    assert (
        db.scalar(
            select(func.count()).select_from(IndicatorObservation).where(IndicatorObservation.report_id == report.id)
        )
        == 0
    )

    failing.agent = FakeReportAgent()
    recovered = failing.retry(blogger.id, report.id)
    assert recovered.id == report.id and recovered.status == "succeeded"
    assert failing.retry(blogger.id, report.id).id == report.id


class ChangingReportDataService(ReportDataService):
    def __init__(self, db) -> None:
        super().__init__(db)
        self.calls = 0

    def build_snapshot(self, blogger_id: int, *, observed_at=None):
        result = super().build_snapshot(blogger_id, observed_at=observed_at)
        self.calls += 1
        if self.calls >= 2:
            result["snapshot_hash"] = "f" * 64
        return result


def test_report_snapshot_conflict_is_409_and_does_not_persist_success(db, tmp_path: Path) -> None:
    blogger = _blogger(db)
    _metric(db, blogger.id, suffix="A", views=10, revenue=5, cost=2)
    IndicatorService(db).initialize_defaults(blogger.id)
    service = ReportService(
        db,
        agent=FakeReportAgent(),
        data_service=ChangingReportDataService(db),
        task_service=TaskMemoryService(db, tmp_path / "tasks"),
    )
    with pytest.raises(ReportServiceError) as conflict:
        service.generate(blogger.id, "snapshot-conflict")
    assert conflict.value.code == "REPORT_SNAPSHOT_CONFLICT" and conflict.value.status_code == 409
    report = db.scalar(select(Report).where(Report.idempotency_key == "snapshot-conflict"))
    assert report is not None and report.status == "failed"
    assert not service.list_evidence(blogger.id, report.id)


def test_report_compare_and_cross_blogger_isolation(db, tmp_path: Path) -> None:
    owner = _blogger(db, "甲")
    other = _blogger(db, "乙")
    _metric(db, owner.id, suffix="A", views=10, revenue=5, cost=2)
    IndicatorService(db).initialize_defaults(owner.id)
    first = _service(db, tmp_path).generate(owner.id, "compare-first")
    _metric(db, owner.id, suffix="B", views=30, revenue=20, cost=4)
    second = _service(db, tmp_path).generate(owner.id, "compare-second")

    comparison = _service(db, tmp_path).compare(owner.id, first.id, second.id)
    assert comparison["left_id"] == first.id and comparison["right_id"] == second.id
    assert comparison["conclusion_changes"]["money"]["net"]["delta"] == 16
    assert comparison["chart_changes"]["traffic_line"]
    with pytest.raises(ReportServiceError) as hidden:
        _service(db, tmp_path).get(other.id, first.id)
    assert hidden.value.status_code == 404

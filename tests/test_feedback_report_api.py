from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.feedback_routes import get_feedback_service
from app.api.report_routes import get_report_service
from app.db.session import get_db
from app.main import app
from app.models import Blogger, Metric, Output, Schedule
from app.services.embedding_service import FakeEmbeddingService
from app.services.feedback_agent import FakeFeedbackAgent
from app.services.feedback_service import FeedbackService
from app.services.memory_service import MemoryService
from app.services.report_agent import FakeReportAgent
from app.services.report_service import ReportService
from app.services.task_memory_service import TaskMemoryService

pytestmark = pytest.mark.daily


def _seed(db):
    owner = Blogger(
        name="反馈报告API甲",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
        profile_state="complete",
        suit_type="综合",
    )
    other = Blogger(
        name="反馈报告API乙",
        platform="抖音",
        content_types_json='["风景"]',
        style="记录",
        follower_band="1万以下",
        monetization_types_json="[]",
        profile_state="complete",
    )
    db.add_all([owner, other])
    db.flush()
    current = None
    primary = None
    for index, views in enumerate((100, 120, 500), start=1):
        current = Output(
            blogger_id=owner.id,
            type="script",
            category="酸汤美食",
            title=f"API内容{index}",
            content_json="{}",
            status="succeeded",
            version=1,
        )
        db.add(current)
        db.flush()
        schedule = Schedule(
            blogger_id=owner.id,
            output_id=current.id,
            plan_date=date(2026, 8, index),
            platform="抖音",
            content_type="视频",
            title=current.title,
            status="collected",
        )
        db.add(schedule)
        db.flush()
        primary = Metric(
            output_id=current.id,
            schedule_id=schedule.id,
            source_type="manual",
            views=views,
            likes=views // 4,
            comments=5,
            collects=2,
            shares=1,
            actual_revenue=500 if index == 3 else None,
            actual_cost=200 if index == 3 else None,
            user_confirmed=True,
            idempotency_key=f"api-metric-{index}",
            collected_at=datetime(2026, 8, index, 12),
        )
        db.add(primary)
    db.commit()
    return owner, other, current, primary


def test_feedback_indicator_report_api_and_cross_blogger_isolation(db, tmp_path: Path) -> None:
    owner, other, output, metric = _seed(db)
    assert output is not None and metric is not None
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding)
    feedback_service = FeedbackService(
        db,
        agent=FakeFeedbackAgent(),
        embedding_service=embedding,
        memory_service=memory,
        task_service=TaskMemoryService(db, tmp_path / "feedback-tasks"),
    )
    report_service = ReportService(
        db,
        agent=FakeReportAgent(),
        task_service=TaskMemoryService(db, tmp_path / "report-tasks"),
    )

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_feedback_service] = lambda: feedback_service
    app.dependency_overrides[get_report_service] = lambda: report_service
    client = TestClient(app)
    try:
        created = client.post(
            f"/api/v1/bloggers/{owner.id}/feedback-runs",
            json={
                "output_id": output.id,
                "primary_metric_id": metric.id,
                "idempotency_key": "api-feedback-key",
                "user_instruction": "只提出候选",
            },
        )
        assert created.status_code == 200, created.text
        run = created.json()
        assert run["status"] == "analyzed"
        evidence = client.get(
            f"/api/v1/bloggers/{owner.id}/feedback-runs/{run['id']}/evidence"
        )
        candidates = client.get(
            f"/api/v1/bloggers/{owner.id}/feedback-runs/{run['id']}/candidates"
        )
        assert evidence.status_code == 200 and evidence.json()
        assert candidates.status_code == 200 and candidates.json()
        profile_ids = [
            item["id"] for item in candidates.json() if item["candidate_type"] == "profile"
        ]
        confirmed = client.post(
            f"/api/v1/bloggers/{owner.id}/feedback-runs/{run['id']}/confirm",
            json={"candidate_ids": profile_ids, "place_overrides": {}},
        )
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "applied"
        hidden = client.get(
            f"/api/v1/bloggers/{other.id}/feedback-runs/{run['id']}"
        )
        assert hidden.status_code == 404

        defaults = client.post(f"/api/v1/bloggers/{owner.id}/indicators/defaults")
        assert defaults.status_code == 200 and len(defaults.json()) == 11
        recomputed = client.post(
            f"/api/v1/bloggers/{owner.id}/indicators/recompute",
            json={"idempotency_key": "api-indicator-key", "feedback_run_id": run["id"]},
        )
        assert recomputed.status_code == 200 and len(recomputed.json()) == 11
        indicator_id = defaults.json()[0]["id"]
        observations = client.get(
            f"/api/v1/bloggers/{owner.id}/indicators/{indicator_id}/observations"
        )
        assert observations.status_code == 200 and observations.json()
        injection = client.post(
            f"/api/v1/bloggers/{owner.id}/indicators",
            json={
                "category": "traffic",
                "name": "非法公式",
                "meaning": "不能执行",
                "formula_key": "__import__('os').system('whoami')",
                "source_tables": ["metric"],
                "unit": "count",
                "direction": "neutral",
            },
        )
        assert injection.status_code == 422

        left = client.post(
            f"/api/v1/bloggers/{owner.id}/reports",
            json={"idempotency_key": "api-report-left", "user_instruction": "生成报告"},
        )
        right = client.post(
            f"/api/v1/bloggers/{owner.id}/reports",
            json={"idempotency_key": "api-report-right", "user_instruction": "再次生成"},
        )
        assert left.status_code == right.status_code == 200
        comparison = client.get(
            f"/api/v1/bloggers/{owner.id}/reports/compare",
            params={"left_id": left.json()["id"], "right_id": right.json()["id"]},
        )
        assert comparison.status_code == 200
        report_evidence = client.get(
            f"/api/v1/bloggers/{owner.id}/reports/{left.json()['id']}/evidence"
        )
        assert report_evidence.status_code == 200 and report_evidence.json()
        hidden_report = client.get(
            f"/api/v1/bloggers/{other.id}/reports/{left.json()['id']}"
        )
        assert hidden_report.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_feedback_service, None)
        app.dependency_overrides.pop(get_report_service, None)

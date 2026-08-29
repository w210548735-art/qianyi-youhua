from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.daily


def test_phase4_demo_preserves_legacy_wiring_but_hides_paused_mvp_modules() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    page = response.text
    for control in (
        'id="startFeedback"',
        'id="loadFeedbackEvidence"',
        'id="loadFeedbackCandidates"',
        'id="confirmFeedback"',
        'id="rejectFeedback"',
        'id="initializeIndicators"',
        'id="recomputeIndicators"',
        'id="generateReport"',
        'id="compareReports"',
        'id="reportCharts"',
    ):
        assert control in page
    for path in (
        "/feedback-runs",
        "/evidence",
        "/candidates",
        "/confirm",
        "/reject",
        "/indicators/defaults",
        "/indicators/recompute",
        "/reports/compare",
    ):
        assert path in page
    assert 'id="feedbackReportSection" hidden' in page
    assert 'id="mvpPausedModulesNotice"' in page
    assert "反馈闭环、经营指标、历史观察、经营报告、图表和报告对比暂不参与当前 MVP" in page
    assert "manual" in page and "simulated" in page
    assert "actual" in page and "estimated" in page and "data_insufficient" in page
    assert "simulation_only" in page and "模拟预览" in page and "不计入实际流量" in page
    assert "textContent" in page
    assert "innerHTML" not in page


def test_phase4_openapi_exposes_static_routes_without_dynamic_collision() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    expected = {
        "/api/v1/bloggers/{blogger_id}/feedback-runs",
        "/api/v1/bloggers/{blogger_id}/feedback-runs/{run_id}/evidence",
        "/api/v1/bloggers/{blogger_id}/feedback-runs/{run_id}/candidates",
        "/api/v1/bloggers/{blogger_id}/feedback-runs/{run_id}/confirm",
        "/api/v1/bloggers/{blogger_id}/feedback-runs/{run_id}/reject",
        "/api/v1/bloggers/{blogger_id}/indicators/defaults",
        "/api/v1/bloggers/{blogger_id}/indicators/recompute",
        "/api/v1/bloggers/{blogger_id}/reports/compare",
    }
    assert expected <= set(paths)
    ordered = [route.path for route in app.routes]
    assert ordered.index("/api/v1/bloggers/{blogger_id}/reports/compare") < ordered.index(
        "/api/v1/bloggers/{blogger_id}/reports/{report_id}"
    )

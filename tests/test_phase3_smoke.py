from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_phase3_demo_page_has_real_wired_controls_and_simulation_boundary():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    body = response.text
    required_controls = {
        'id="outputAssessmentSelect"',
        'id="generateScript"',
        'id="generateStoryboard"',
        'id="generateRoute"',
        'id="outputEvidence"',
        'id="createSchedule"',
        'id="scanReminders"',
        'id="simulatePublish"',
        'id="collectMetrics"',
        'id="metricStatus"',
    }
    assert required_controls <= {item for item in required_controls if item in body}
    for path in (
        "/outputs/generate/script",
        "/outputs/generate/storyboard",
        "/outputs/generate/route",
        "/evidence",
        "/schedules/reminders/scan",
        "/publish",
        "/collections",
        "/metrics",
    ):
        assert path in body
    assert "不代表真实平台发布" in body
    assert "不会自动修改画像、资产或地点收益" in body
    assert "经营报告、反馈学习和真实平台发布仍未实现" in body


def test_phase3_openapi_exposes_static_generation_and_execution_paths_before_dynamic_ids():
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    required = {
        "/api/v1/bloggers/{blogger_id}/outputs/generate/script",
        "/api/v1/bloggers/{blogger_id}/outputs/generate/storyboard",
        "/api/v1/bloggers/{blogger_id}/outputs/generate/route",
        "/api/v1/bloggers/{blogger_id}/outputs/{output_id}/evidence",
        "/api/v1/bloggers/{blogger_id}/outputs/{output_id}/retry",
        "/api/v1/bloggers/{blogger_id}/outputs/{output_id}/revisions",
        "/api/v1/bloggers/{blogger_id}/schedules/reminders/scan",
        "/api/v1/bloggers/{blogger_id}/schedules/{schedule_id}/publish",
        "/api/v1/bloggers/{blogger_id}/schedules/{schedule_id}/collections",
        "/api/v1/bloggers/{blogger_id}/collections/{job_id}/retry",
        "/api/v1/bloggers/{blogger_id}/metrics",
    }
    assert required <= paths.keys()

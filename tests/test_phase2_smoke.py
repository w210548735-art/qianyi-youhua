from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.daily


def test_phase2_demo_page_contains_assessment_controls_and_routes():
    """第二阶段最简演示页必须暴露完整体检操作和 API 入口。"""
    with TestClient(app) as client:
        page = client.get("/")
        openapi = client.get("/openapi.json")

    assert page.status_code == 200
    assert "知识库体检与指标评估" in page.text
    for control in (
        "startAssessment",
        "assessmentStatus",
        "retryAssessment",
        "assessmentStructure",
        "assessmentCoreAssets",
        "assessmentWeakPoints",
        "assessmentIndicators",
        "assessmentOverallScore",
        "assessmentReadiness",
        "assessmentMissing",
        "assessmentSuggestions",
        "assessmentHistory",
        "compareAssessments",
        "assessmentEvidence",
    ):
        assert f'id="{control}"' in page.text

    for text in (
        "三库结构",
        "核心资产",
        "薄弱点",
        "指标",
        "综合分",
        "功能就绪",
        "缺失项",
        "改进建议",
        "历史体检",
        "证据详情",
        "仍未实现",
    ):
        assert text in page.text

    expected_routes = {
        ("/api/v1/bloggers/{blogger_id}/assessments", "post"),
        ("/api/v1/bloggers/{blogger_id}/assessments", "get"),
        ("/api/v1/bloggers/{blogger_id}/assessments/{assessment_id}", "get"),
        ("/api/v1/bloggers/{blogger_id}/assessments/{assessment_id}/retry", "post"),
        ("/api/v1/bloggers/{blogger_id}/assessments/compare", "get"),
        ("/api/v1/bloggers/{blogger_id}/assessments/{assessment_id}/evidence", "get"),
    }
    openapi_body = openapi.json()
    paths = openapi_body["paths"]
    for path, method in expected_routes:
        assert method in paths.get(path, {})

    schemas = openapi_body["components"]["schemas"]
    assessment_schema = schemas["AssessmentRead"]
    assert assessment_schema["properties"]["input_snapshot"]
    assert assessment_schema["properties"]["library_analysis"]
    assert assessment_schema["properties"]["feature_readiness"]
    assert assessment_schema["properties"]["suggestions"]
    assert assessment_schema["properties"]["evidence"]
    assert "input_snapshot_json" not in assessment_schema["properties"]
    assert "evidence_json" not in schemas["AssessmentIndicatorRead"]["properties"]

    assert (
        list(paths).index("/api/v1/bloggers/{blogger_id}/assessments/compare")
        < list(paths).index("/api/v1/bloggers/{blogger_id}/assessments/{assessment_id}")
    )
    assert paths["/api/v1/bloggers/{blogger_id}/assessments"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/AssessmentRead"}
    assert paths["/api/v1/bloggers/{blogger_id}/assessments/compare"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/AssessmentCompareRead"}
    assert paths["/api/v1/bloggers/{blogger_id}/assessments/{assessment_id}/evidence"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/AssessmentEvidenceRead"
    }


def test_phase2_demo_page_keeps_assessment_api_when_phase_three_is_enabled():
    """进入第三阶段后，页面仍保留第二阶段接口并明确下一阶段边界。"""
    with TestClient(app) as client:
        page = client.get("/")

    html = page.text
    for route in (
        "/assessments",
        "/retry",
        "/evidence",
        "/assessments/compare",
    ):
        assert route in html
    assert 'id="generateScript"' in html
    assert "经营报告、反馈学习和真实平台发布仍未实现" in html


def test_phase2_profile_switch_clears_all_assessment_displays():
    """切换或清空博主时不应继续展示上一位博主的二期结果。"""
    with TestClient(app) as client:
        html = client.get("/").text

    start = html.index("function clearAssessmentDisplay()")
    end = html.index("function setActiveProfile", start)
    reset_block = html[start:end]
    for element_id in (
        "assessmentIdempotencyKey",
        "assessmentId",
        "assessmentStatus",
        "assessmentSummary",
        "assessmentStructure",
        "assessmentCoreAssets",
        "assessmentWeakPoints",
        "assessmentOverallScore",
        "assessmentIndicators",
        "assessmentReadiness",
        "assessmentMissing",
        "assessmentSuggestions",
        "assessmentHistory",
        "assessmentComparison",
        "assessmentEvidence",
    ):
        assert f"$('{element_id}')" in reset_block
    for element_id in ("assessmentSelect", "assessmentLeftSelect", "assessmentRightSelect"):
        assert f"'{element_id}'" in reset_block
    assert "select.replaceChildren(new Option" in reset_block
    assert "clearAssessmentDisplay();" in html

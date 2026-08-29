from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.output_routes import get_output_service
from app.db.session import get_db
from app.main import app
from app.models import Blogger, Place
from app.services.assessment_agent import FakeAssessmentAgent
from app.services.assessment_service import AssessmentService
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.build_service import LibraryBuildService
from app.services.context_service import ContextService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.output_agent import FakeOutputAgent
from app.services.output_service import OutputService
from app.services.output_validation_service import OutputValidationService
from app.services.task_memory_service import TaskMemoryService

pytestmark = pytest.mark.daily


def setup_api(db, tmp_path: Path):
    owner = Blogger(
        name="输出API甲",
        platform="抖音",
        content_types_json=json.dumps(["美食", "景区", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        routes="贵阳",
        frequency="周更",
        profile_state="complete",
    )
    other = Blogger(
        name="输出API乙",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万以下",
        monetization_types_json="[]",
        frequency="周更",
        profile_state="complete",
    )
    db.add_all([owner, other])
    db.commit()
    db.refresh(owner)
    db.refresh(other)
    embedding = FakeEmbeddingService()
    build = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = build.start_build(owner.id, "output-api-build")
    assert build.execute_build(run.id).status == "succeeded"
    memory = MemoryService(db, embedding)
    assessment_service = AssessmentService(
        db,
        agent=FakeAssessmentAgent(),
        analysis_service=LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=TaskMemoryService(db, tmp_path / "assessment"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )
    pending = assessment_service.start_assessment(owner.id, "output-api-assessment")
    assessment = assessment_service.execute_assessment(pending.id, owner.id)
    output_service = OutputService(
        db,
        agent=FakeOutputAgent(),
        validation_service=OutputValidationService(),
        task_service=TaskMemoryService(db, tmp_path / "output"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
        analysis_service=LibraryAnalysisService(db, embedding),
    )

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_output_service] = lambda: output_service
    return owner, other, assessment, output_service, TestClient(app)


def clear_api() -> None:
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_output_service, None)


def test_output_schedule_publish_collection_full_api_and_isolation(db, tmp_path: Path):
    owner, other, assessment, _, client = setup_api(db, tmp_path)
    try:
        script = client.post(
            f"/api/v1/bloggers/{owner.id}/outputs/generate/script",
            json={
                "assessment_id": assessment.id,
                "idempotency_key": "output-api-script",
                "topic": "贵州酸汤鱼",
            },
        )
        assert script.status_code == 200, script.text
        script_body = script.json()
        assert script_body["status"] == "succeeded" and script_body["assets"]

        evidence = client.get(
            f"/api/v1/bloggers/{owner.id}/outputs/{script_body['id']}/evidence"
        )
        assert evidence.status_code == 200 and evidence.json()["assets"]

        storyboard = client.post(
            f"/api/v1/bloggers/{owner.id}/outputs/generate/storyboard",
            json={
                "assessment_id": assessment.id,
                "script_output_id": script_body["id"],
                "idempotency_key": "output-api-storyboard",
            },
        )
        assert storyboard.status_code == 200, storyboard.text
        assert storyboard.json()["content_json"]["shots"]

        history = client.get(f"/api/v1/bloggers/{owner.id}/outputs")
        assert history.status_code == 200 and len(history.json()) == 2
        hidden = client.get(f"/api/v1/bloggers/{other.id}/outputs/{script_body['id']}")
        assert hidden.status_code == 404

        revision_content = dict(script_body["content_json"])
        revision_content["title"] = "API人工修订"
        revision = client.post(
            f"/api/v1/bloggers/{owner.id}/outputs/{script_body['id']}/revisions",
            json={"content_json": revision_content},
        )
        assert revision.status_code == 200 and revision.json()["version"] == 2

        planned = (date.today() + timedelta(days=1)).isoformat()
        schedule = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules",
            json={
                "output_id": revision.json()["id"],
                "plan_date": planned,
                "platform": "抖音",
                "content_type": "script",
                "title": "酸汤鱼排期",
            },
        )
        assert schedule.status_code == 200, schedule.text
        schedule_id = schedule.json()["id"]
        scanned = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/reminders/scan",
            params={"on_date": planned},
        )
        assert scanned.status_code == 200 and len(scanned.json()) == 1
        duplicate_scan = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/reminders/scan",
            params={"on_date": planned},
        )
        assert duplicate_scan.status_code == 200 and duplicate_scan.json() == []

        published = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/publish",
            json={"idempotency_key": "publish-api-key"},
        )
        assert published.status_code == 200 and published.json()["simulated"] is True
        assert "不代表真实平台" in published.json()["notice"]
        same_publish = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/publish",
            json={"idempotency_key": "publish-api-key"},
        )
        assert same_publish.status_code == 200

        collected = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/collections",
            json={
                "idempotency_key": "collection-api-key",
                "metrics": {
                    "source_type": "manual",
                    "views": 120,
                    "likes": 20,
                    "comments": 3,
                    "collects": 8,
                    "shares": 6,
                    "actual_revenue": 500.5,
                    "actual_cost": 200.25,
                    "user_confirmed": True,
                    "collected_at": "2026-09-03T08:09:10",
                },
            },
        )
        assert collected.status_code == 200, collected.text
        assert collected.json()["status"] == "succeeded"
        metrics = client.get(f"/api/v1/bloggers/{owner.id}/metrics")
        assert metrics.status_code == 200
        assert metrics.json()[0]["source_type"] == "manual" and metrics.json()[0]["views"] == 120
        assert metrics.json()[0]["idempotency_key"] == "collection-api-key"
        assert metrics.json()[0]["shares"] == 6
        assert metrics.json()[0]["actual_revenue"] == 500.5
        assert metrics.json()[0]["actual_cost"] == 200.25
        assert metrics.json()[0]["user_confirmed"] is True
        assert datetime.fromisoformat(metrics.json()[0]["collected_at"]) == datetime(
            2026, 9, 3, 8, 9, 10
        )

        ignored_nested_key = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/collections",
            json={
                "idempotency_key": "another-collection-key",
                "metrics": {
                    "idempotency_key": "must-not-be-silently-ignored",
                    "source_type": "manual",
                    "views": 1,
                },
            },
        )
        assert ignored_nested_key.status_code == 422
        ignored_outer_source = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/collections",
            json={
                "idempotency_key": "another-collection-key",
                "source_type": "manual",
                "metrics": {"source_type": "manual", "views": 1},
            },
        )
        assert ignored_outer_source.status_code == 422
        simulated_money = client.post(
            f"/api/v1/bloggers/{owner.id}/schedules/{schedule_id}/collections",
            json={
                "idempotency_key": "simulated-money-key",
                "metrics": {
                    "source_type": "simulated",
                    "actual_revenue": 10,
                    "user_confirmed": True,
                },
            },
        )
        assert simulated_money.status_code == 422
    finally:
        clear_api()


def test_route_api_null_details_success_and_openapi_static_paths(db, tmp_path: Path):
    owner, _, assessment, _, client = setup_api(db, tmp_path)
    incomplete = Place(
        blogger_id=owner.id,
        name="商业数据未知地点",
        category="景区",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key="route-api-incomplete",
    )
    db.add(incomplete)
    db.commit()
    db.refresh(incomplete)
    try:
        blocked = client.post(
            f"/api/v1/bloggers/{owner.id}/outputs/generate/route",
            json={
                "assessment_id": assessment.id,
                "idempotency_key": "route-api-blocked",
                "place_ids": [incomplete.id],
            },
        )
        assert blocked.status_code == 422
        assert blocked.json()["detail"]["code"] == "ROUTE_COMMERCIAL_DATA_INCOMPLETE"
        assert "est_cost" in blocked.json()["detail"]["details"][0]["missing_fields"]

        incomplete.est_cost = 50
        incomplete.est_benefit = 150
        incomplete.like_level = 5
        incomplete.fits_koc = True
        incomplete.fits_shoot = True
        db.commit()
        route = client.post(
            f"/api/v1/bloggers/{owner.id}/outputs/generate/route",
            json={
                "assessment_id": assessment.id,
                "idempotency_key": "route-api-success",
                "place_ids": [incomplete.id],
            },
        )
        assert route.status_code == 200, route.text
        assert route.json()["places"][0]["place_id"] == incomplete.id

        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        paths = openapi.json()["paths"]
        required = {
            "/api/v1/bloggers/{blogger_id}/outputs/generate/script",
            "/api/v1/bloggers/{blogger_id}/outputs/generate/storyboard",
            "/api/v1/bloggers/{blogger_id}/outputs/generate/route",
            "/api/v1/bloggers/{blogger_id}/schedules/reminders/scan",
            "/api/v1/bloggers/{blogger_id}/schedules/{schedule_id}/publish",
            "/api/v1/bloggers/{blogger_id}/metrics",
        }
        assert required <= paths.keys()
    finally:
        clear_api()

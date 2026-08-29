from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.assessment_routes import get_assessment_service
from app.db.session import get_db
from app.main import app
from app.models import Blogger
from app.services.assessment_agent import AssessmentAgentError, FakeAssessmentAgent
from app.services.assessment_service import AssessmentService
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.build_service import LibraryBuildService
from app.services.context_service import ContextService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService

pytestmark = pytest.mark.daily


def make_blogger(db, name: str) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食", "景区", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def build_three_libraries(db, blogger: Blogger, key: str) -> None:
    build = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    run = build.start_build(blogger.id, key)
    assert build.execute_build(run.id).status == "succeeded"


def api_service(db, tasks_root: Path, agent: FakeAssessmentAgent | None = None) -> AssessmentService:
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding)
    return AssessmentService(
        db,
        agent=agent or FakeAssessmentAgent(),
        analysis_service=LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=TaskMemoryService(db, tasks_root),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )


def client_for(db, service: AssessmentService) -> TestClient:
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_assessment_service] = lambda: service
    return TestClient(app)


def clear_overrides() -> None:
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_assessment_service, None)


def assert_no_raw_embedding(value: object) -> None:
    """API 体检快照只返回向量摘要，不得泄露原始向量数组。"""
    if isinstance(value, dict):
        assert "embedding" not in value
        for nested in value.values():
            assert_no_raw_embedding(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_raw_embedding(nested)


def test_assessment_api_success_detail_evidence_history_compare_and_cross_blogger_404(db, tmp_path: Path):
    owner = make_blogger(db, "API体检甲")
    other = make_blogger(db, "API体检乙")
    build_three_libraries(db, owner, "assessment-api-build")
    service = api_service(db, tmp_path / "tasks")
    client = client_for(db, service)
    try:
        first = client.post(
            f"/api/v1/bloggers/{owner.id}/assessments",
            json={"idempotency_key": "assessment-api-first"},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "succeeded"
        assert len(first_body["indicators"]) >= 3
        assert first_body["evidence"]
        assert_no_raw_embedding(first_body["input_snapshot"])
        assert_no_raw_embedding(first_body["library_analysis"])
        first_id = first_body["id"]

        detail = client.get(f"/api/v1/bloggers/{owner.id}/assessments/{first_id}")
        assert detail.status_code == 200
        assert detail.json()["indicators"] and detail.json()["evidence"]
        evidence = client.get(
            f"/api/v1/bloggers/{owner.id}/assessments/{first_id}/evidence"
        )
        assert evidence.status_code == 200 and evidence.json()

        duplicate = client.post(
            f"/api/v1/bloggers/{owner.id}/assessments",
            json={"idempotency_key": "assessment-api-first"},
        )
        assert duplicate.status_code == 200 and duplicate.json()["id"] == first_id

        second = client.post(
            f"/api/v1/bloggers/{owner.id}/assessments",
            json={"idempotency_key": "assessment-api-second"},
        )
        assert second.status_code == 200
        second_id = second.json()["id"]
        history = client.get(f"/api/v1/bloggers/{owner.id}/assessments")
        assert history.status_code == 200
        assert [item["id"] for item in history.json()] == [second_id, first_id]

        compared = client.get(
            f"/api/v1/bloggers/{owner.id}/assessments/compare",
            params={"left_id": first_id, "right_id": second_id},
        )
        assert compared.status_code == 200
        comparison_body = compared.json()
        assert "left_id" in comparison_body, comparison_body
        assert comparison_body["left_id"] == first_id
        assert "indicators" in comparison_body or "indicator_changes" in comparison_body

        hidden = client.get(f"/api/v1/bloggers/{other.id}/assessments/{first_id}")
        assert hidden.status_code == 404
        assert hidden.json()["detail"] == "ASSESSMENT_NOT_FOUND"
        hidden_evidence = client.get(
            f"/api/v1/bloggers/{other.id}/assessments/{first_id}/evidence"
        )
        assert hidden_evidence.status_code == 404
        hidden_compare = client.get(
            f"/api/v1/bloggers/{other.id}/assessments/compare",
            params={"left_id": first_id, "right_id": second_id},
        )
        assert hidden_compare.status_code == 404
    finally:
        clear_overrides()


def test_assessment_api_failure_and_retry_use_safe_error_code_without_partial_payload(db, tmp_path: Path):
    blogger = make_blogger(db, "API重试")
    build_three_libraries(db, blogger, "assessment-api-retry-build")
    service = api_service(
        db,
        tmp_path / "tasks",
        FakeAssessmentAgent(
            fail_with=AssessmentAgentError("AGENT_INVALID_JSON", "格式错误", retryable=True)
        ),
    )
    client = client_for(db, service)
    try:
        failed_response = client.post(
            f"/api/v1/bloggers/{blogger.id}/assessments",
            json={"idempotency_key": "assessment-api-retry"},
        )
        assert failed_response.status_code == 422
        assert failed_response.json() == {"detail": "AGENT_INVALID_JSON"}
        failed = service.list_assessments(blogger.id)[0]
        assert failed.status == "failed" and failed.overall_score is None

        service.agent = FakeAssessmentAgent()
        retried = client.post(
            f"/api/v1/bloggers/{blogger.id}/assessments/{failed.id}/retry"
        )
        assert retried.status_code == 200
        assert retried.json()["id"] == failed.id
        assert retried.json()["status"] == "succeeded"
        assert retried.json()["evidence"]
    finally:
        clear_overrides()

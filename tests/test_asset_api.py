from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models import Blogger
from app.services.build_service import LibraryBuildService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService

pytestmark = pytest.mark.daily


def create_blogger(db) -> Blogger:
    blogger = Blogger(
        name="资产 API 测试博主",
        platform="抖音",
        content_types_json=json.dumps(["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def test_asset_api_update_delete_search_and_pagination(db):
    blogger = create_blogger(db)
    embedding = FakeEmbeddingService()
    service = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = service.start_build(blogger.id, "asset-api-build")
    assert service.execute_build(run.id).status == "succeeded"

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        first_page = client.get(
            f"/api/v1/bloggers/{blogger.id}/assets",
            params={"page": 1, "page_size": 5},
        )
        second_page = client.get(
            f"/api/v1/bloggers/{blogger.id}/assets",
            params={"page": 2, "page_size": 5},
        )
        assert first_page.status_code == second_page.status_code == 200
        assert len(first_page.json()) == len(second_page.json()) == 5
        assert not {item["id"] for item in first_page.json()}.intersection(item["id"] for item in second_page.json())
        filtered = client.get(
            f"/api/v1/bloggers/{blogger.id}/assets",
            params={
                "q": "酸汤鱼",
                "lib_type": "knowledge",
                "category": "美食",
                "tags": "酸汤鱼",
                "source_type": "official",
                "source": "民政厅",
                "min_credibility": 5,
                "max_credibility": 5,
            },
        )
        assert filtered.status_code == 200
        assert [item["title"] for item in filtered.json()] == ["凯里酸汤鱼"]
        invalid_range = client.get(
            f"/api/v1/bloggers/{blogger.id}/assets",
            params={"min_credibility": 5, "max_credibility": 2},
        )
        assert invalid_range.status_code == 422

        asset_id = first_page.json()[2]["id"]
        updated = client.put(
            f"/api/v1/assets/{asset_id}",
            json={"title": "API 人工修正标题", "tags": ["人工", "锁定"]},
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == asset_id
        assert updated.json()["title"] == "API 人工修正标题"

        deleted = client.delete(f"/api/v1/assets/{asset_id}")
        assert deleted.status_code == 200
        results = client.get(
            f"/api/v1/bloggers/{blogger.id}/assets",
            params={"page_size": 100},
        )
        assert results.status_code == 200
        assert asset_id not in {item["id"] for item in results.json()}
    finally:
        app.dependency_overrides.clear()


def test_scoped_manual_asset_complete_crud_validation_and_isolation(db):
    first = create_blogger(db)
    second = create_blogger(db)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    payload = {
        "lib_type": "knowledge",
        "category": "非遗",
        "title": "手工苗绣知识",
        "content": "用户录入的苗绣资料。",
        "tags": ["苗绣", "非遗"],
        "source_type": "official",
        "source_url": "https://example.com/manual-miao",
        "source_title": "苗绣官方资料",
        "publisher": "测试文旅部门",
        "verified_at": "2026-08-28",
        "credibility": 5,
        "idempotency_key": "manual-asset-api-1",
    }
    try:
        created = client.post(f"/api/v1/bloggers/{first.id}/assets", json=payload)
        repeated = client.post(f"/api/v1/bloggers/{first.id}/assets", json=payload)
        assert created.status_code == repeated.status_code == 200
        asset_id = created.json()["id"]
        assert repeated.json()["id"] == asset_id
        assert created.json()["origin"] == "manual"
        assert created.json()["manual_locked"] is True
        assert client.get(f"/api/v1/bloggers/{first.id}/assets/{asset_id}").status_code == 200
        assert client.get(f"/api/v1/bloggers/{second.id}/assets/{asset_id}").status_code == 404

        updated = client.put(
            f"/api/v1/bloggers/{first.id}/assets/{asset_id}",
            json={"title": "修正后的苗绣知识", "tags": ["苗绣", "人工修正"]},
        )
        assert updated.status_code == 200
        assert updated.json()["title"] == "修正后的苗绣知识"
        deleted = client.delete(f"/api/v1/bloggers/{first.id}/assets/{asset_id}")
        repeated_delete = client.delete(f"/api/v1/bloggers/{first.id}/assets/{asset_id}")
        assert deleted.status_code == repeated_delete.status_code == 200
        assert repeated_delete.json()["deleted_at"] == deleted.json()["deleted_at"]
        assert client.get(f"/api/v1/bloggers/{first.id}/assets/{asset_id}").status_code == 404
        assert client.post(f"/api/v1/bloggers/{first.id}/assets", json=payload).status_code == 409

        invalid = dict(payload)
        invalid["source_url"] = None
        invalid["idempotency_key"] = "invalid-source-asset"
        assert client.post(f"/api/v1/bloggers/{first.id}/assets", json=invalid).status_code == 422
    finally:
        app.dependency_overrides.clear()

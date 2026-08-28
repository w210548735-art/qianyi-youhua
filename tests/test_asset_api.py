from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models import Blogger
from app.services.build_service import LibraryBuildService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService


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

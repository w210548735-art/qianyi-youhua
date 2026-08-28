from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.api.place_routes import router as place_router
from app.db.session import get_db
from app.main import app
from app.models import Blogger

if not any(getattr(route, "path", None) == "/api/v1/bloggers/{blogger_id}/places" for route in app.routes):
    app.include_router(place_router)


def make_blogger(db, name: str = "地点 API 博主") -> Blogger:
    blogger = Blogger(
        name=name,
        platform="小红书",
        content_types_json=json.dumps(["探店"], ensure_ascii=False),
        style="vlog",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def client_for(db) -> TestClient:
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_place_api_complete_crud_filters_and_isolation(db):
    blogger = make_blogger(db)
    other = make_blogger(db, "其他 API 博主")
    client = client_for(db)
    try:
        created = client.post(
            f"/api/v1/bloggers/{blogger.id}/places",
            json={
                "name": "梵净山",
                "category": "景区",
                "location": "铜仁",
                "specialty": "生态景观",
                "tags": ["铜仁", "世界遗产"],
                "source": "official",
                "source_url": "https://example.com/fanjingshan",
                "credibility": 5,
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["blogger_id"] == blogger.id
        assert body["source"] == "official"
        assert body["est_cost"] is None
        place_id = body["id"]

        duplicate = client.post(
            f"/api/v1/bloggers/{blogger.id}/places",
            json={
                "name": "梵净山",
                "category": "景区",
                "location": "铜仁",
                "source": "official",
                "credibility": 1,
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == place_id

        detail = client.get(f"/api/v1/bloggers/{blogger.id}/places/{place_id}")
        assert detail.status_code == 200
        assert detail.json()["name"] == "梵净山"
        assert client.get(f"/api/v1/bloggers/{other.id}/places/{place_id}").status_code == 404

        updated = client.put(
            f"/api/v1/bloggers/{blogger.id}/places/{place_id}",
            json={"specialty": "用户手工修正", "fits_shoot": True},
        )
        assert updated.status_code == 200
        assert updated.json()["specialty"] == "用户手工修正"
        assert updated.json()["fits_shoot"] is True

        assert client.get(
            f"/api/v1/bloggers/{blogger.id}/places",
            params={"tags": "世界遗产", "source": "official", "min_credibility": 5},
        ).json()[0]["id"] == place_id
        assert client.get(
            f"/api/v1/bloggers/{blogger.id}/places",
            params={"tags": "不存在"},
        ).json() == []

        deleted = client.delete(f"/api/v1/bloggers/{blogger.id}/places/{place_id}")
        assert deleted.status_code == 200
        deleted_again = client.delete(f"/api/v1/bloggers/{blogger.id}/places/{place_id}")
        assert deleted_again.status_code == 200
        assert client.get(f"/api/v1/bloggers/{blogger.id}/places").json() == []
        assert client.get(f"/api/v1/bloggers/{blogger.id}/places/{place_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_place_api_validates_credibility_and_preserves_null_semantics(db):
    blogger = make_blogger(db)
    client = client_for(db)
    try:
        invalid = client.post(
            f"/api/v1/bloggers/{blogger.id}/places",
            json={
                "name": "无效地点",
                "category": "景区",
                "source": "manual",
                "credibility": 6,
            },
        )
        assert invalid.status_code == 422
        valid = client.post(
            f"/api/v1/bloggers/{blogger.id}/places",
            json={
                "name": "贵州博物馆",
                "category": "景区",
                "source": "manual",
                "credibility": 0,
            },
        )
        assert valid.status_code == 200
        result = valid.json()
        assert result["like_level"] is None
        assert result["est_cost"] is None
        assert result["est_benefit"] is None
        assert result["fits_koc"] is None
        assert result["fits_shoot"] is None
    finally:
        app.dependency_overrides.clear()


def test_place_sync_endpoint_uses_trusted_seeds_idempotently(db):
    blogger = make_blogger(db, "种子地点 API 博主")
    client = client_for(db)
    try:
        first = client.post(f"/api/v1/bloggers/{blogger.id}/places/sync")
        assert first.status_code == 200
        payload = first.json()
        assert payload["inserted"] >= 15
        assert payload["places"]
        assert all(item["origin"] == "seed" for item in payload["places"])
        assert all(item["source"] == "official" for item in payload["places"])
        assert all(item["est_cost"] is None for item in payload["places"])
        assert all(item["est_benefit"] is None for item in payload["places"])
        assert all(item["like_level"] is None for item in payload["places"])
        assert all(item["fits_koc"] is None for item in payload["places"])
        assert all(item["fits_shoot"] is None for item in payload["places"])

        second = client.post(f"/api/v1/bloggers/{blogger.id}/places/sync")
        assert second.status_code == 200
        assert second.json()["inserted"] == 0
    finally:
        app.dependency_overrides.clear()

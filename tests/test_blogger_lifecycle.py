from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_db
from app.main import app
from app.models import Blogger, DecisionLog, MemoryRecord
from app.services.blogger_service import BloggerNotFoundError, BloggerService
from app.services.embedding_service import FakeEmbeddingService
from app.services.memory_service import MemoryNotFoundError, MemoryService


def _blogger(db, name: str) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def test_confirmed_profile_update_creates_memory_version_and_isolates_other_blogger(db):
    first = _blogger(db, "第一位")
    second = _blogger(db, "第二位")
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding=embedding)
    first_v1 = memory.sync_profile(first.id, user_confirmed=True)
    second_v1 = memory.sync_profile(second.id, user_confirmed=True)

    updated = BloggerService(db, memory_service=memory).update_confirmed_profile(
        first.id,
        {"style": "vlog", "content_types": ["美食", "非遗"]},
    )

    assert updated.style == "vlog"
    versions = list(
        db.scalars(
            select(MemoryRecord)
            .where(
                MemoryRecord.blogger_id == first.id,
                MemoryRecord.memory_type == "profile_fact",
            )
            .order_by(MemoryRecord.version)
        )
    )
    assert [(item.version, item.status) for item in versions] == [
        (1, "superseded"),
        (2, "active"),
    ]
    assert versions[1].parent_memory_id is None
    assert db.get(MemoryRecord, second_v1.id).status == "active"
    assert db.get(MemoryRecord, first_v1.id).status == "superseded"
    decision = db.scalar(
        select(DecisionLog)
        .where(DecisionLog.blogger_id == first.id, DecisionLog.decision_type == "profile_update")
        .order_by(DecisionLog.id.desc())
    )
    assert decision is not None and "vlog" in decision.decision


def test_soft_delete_is_idempotent_and_hides_blogger_without_deleting_history(db):
    first = _blogger(db, "待删除")
    second = _blogger(db, "保留")
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding=embedding)
    profile_memory = memory.sync_profile(first.id, user_confirmed=True)
    service = BloggerService(db, memory_service=memory)

    deleted = service.soft_delete(first.id)
    repeated = service.soft_delete(first.id)

    assert deleted.deleted_at is not None
    assert repeated.deleted_at == deleted.deleted_at
    assert [item.id for item in service.list_active()] == [second.id]
    with pytest.raises(BloggerNotFoundError):
        service.get_active(first.id)
    with pytest.raises(MemoryNotFoundError):
        memory.semantic_search(first.id, "画像")
    assert db.get(MemoryRecord, profile_memory.id) is not None
    assert db.scalar(
        select(DecisionLog).where(
            DecisionLog.blogger_id == first.id,
            DecisionLog.decision_type == "profile_delete",
        )
    ) is not None


def test_update_missing_or_deleted_blogger_returns_not_found(db):
    blogger = _blogger(db, "删除后不可编辑")
    service = BloggerService(
        db,
        memory_service=MemoryService(db, embedding=FakeEmbeddingService()),
    )
    service.soft_delete(blogger.id)

    with pytest.raises(BloggerNotFoundError):
        service.update_confirmed_profile(blogger.id, {"style": "测评"})
    with pytest.raises(BloggerNotFoundError):
        service.update_confirmed_profile(999999, {"style": "测评"})


def test_blogger_lifecycle_api_and_deleted_entry_isolation(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        created = client.post(
            "/api/v1/bloggers",
            json={
                "name": "生命周期博主",
                "platform": "抖音",
                "content_types": ["美食"],
                "style": "口播",
                "follower_band": "1万-10万",
                "monetization_types": ["商单"],
            },
        )
        assert created.status_code == 200
        blogger_id = created.json()["id"]
        assert client.get(f"/api/v1/bloggers/{blogger_id}").status_code == 200

        updated = client.put(
            f"/api/v1/bloggers/{blogger_id}",
            json={"style": "vlog", "routes": "黔东南"},
        )
        assert updated.status_code == 200
        assert updated.json()["style"] == "vlog"

        deleted = client.delete(f"/api/v1/bloggers/{blogger_id}")
        repeated = client.delete(f"/api/v1/bloggers/{blogger_id}")
        assert deleted.status_code == repeated.status_code == 200
        assert repeated.json()["deleted_at"] == deleted.json()["deleted_at"]
        assert client.get(f"/api/v1/bloggers/{blogger_id}").status_code == 404
        assert all(item["id"] != blogger_id for item in client.get("/api/v1/bloggers").json())
        assert client.get(f"/api/v1/bloggers/{blogger_id}/assets").status_code == 404
        assert (
            client.post(
                f"/api/v1/bloggers/{blogger_id}/build-runs",
                json={"idempotency_key": "deleted-blogger-build"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/memory/bloggers/{blogger_id}/tasks",
                json={"task_type": "demo", "title": "不可创建"},
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/v1/memory/bloggers/{blogger_id}/memories").status_code
            == 404
        )
        assert client.get("/api/v1/bloggers/999999").status_code == 404
    finally:
        app.dependency_overrides.clear()

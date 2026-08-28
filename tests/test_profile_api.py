from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.session import get_db
from app.main import app
from app.models import (
    Blogger,
    ConversationMessage,
    ConversationSession,
    MemoryEmbedding,
    MemoryRecord,
)


def test_profile_conversation_and_confirmation(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        response = client.post("/api/v1/profile-sessions")
        assert response.status_code == 200
        session_id = response.json()["session_id"]

        answers = [
            "阿黔",
            "抖音",
            "美食，非遗",
            "口播",
            "1万-10万",
            "商单，探店",
            "黔东南",
            "酸汤鱼探店",
            "周更",
        ]
        payload = None
        for answer in answers:
            payload = client.post(
                f"/api/v1/profile-sessions/{session_id}/messages",
                json={"message": answer},
            )
            assert payload.status_code == 200
        assert payload.json()["status"] == "confirming"

        confirmed = client.post(f"/api/v1/profile-sessions/{session_id}/confirm")
        assert confirmed.status_code == 200
        profile = confirmed.json()
        assert profile["name"] == "阿黔"
        assert profile["content_types"] == ["美食", "非遗"]
        assert profile["memory_sync"]["status"] == "succeeded"
        memories = list(db.scalars(select(MemoryRecord).where(MemoryRecord.blogger_id == profile["id"])))
        assert {item.memory_type for item in memories} == {"profile_fact", "decision_summary"}
        assert all(item.status == "active" and item.embedding is not None for item in memories)
        assert db.scalar(select(func.count()).select_from(MemoryEmbedding)) == 2

        repeated = client.post(f"/api/v1/profile-sessions/{session_id}/confirm")
        assert repeated.status_code == 200
        assert repeated.json()["id"] == profile["id"]
    finally:
        app.dependency_overrides.clear()


def test_ambiguous_answer_is_clarified_only_once(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        session_id = client.post("/api/v1/profile-sessions").json()["session_id"]
        first = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json={"message": "不知道"},
        )
        assert first.status_code == 200
        assert "尽量具体" in first.json()["question"]
        assert db.get(ConversationSession, session_id).current_question == "name"

        second = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json={"message": "不知道"},
        )
        assert second.status_code == 200
        assert db.get(ConversationSession, session_id).current_question == "platform"
    finally:
        app.dependency_overrides.clear()


def test_profile_can_be_corrected_before_confirmation(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        session_id = client.post("/api/v1/profile-sessions").json()["session_id"]
        answers = ["旧名称", "抖音", "美食", "口播", "1万-10万", "商单", "无", "无", "周更"]
        for answer in answers:
            response = client.post(
                f"/api/v1/profile-sessions/{session_id}/messages",
                json={"message": answer},
            )
            assert response.status_code == 200
        correction = client.put(
            f"/api/v1/profile-sessions/{session_id}/profile",
            json={"field": "name", "value": "新名称"},
        )
        assert correction.status_code == 200
        assert correction.json()["collected_profile"]["name"] == "新名称"
        confirmed = client.post(f"/api/v1/profile-sessions/{session_id}/confirm")
        assert confirmed.json()["name"] == "新名称"
        assert db.scalar(select(func.count()).select_from(Blogger)) == 1
        assert db.scalar(select(func.count()).select_from(ConversationMessage)) >= 19
    finally:
        app.dependency_overrides.clear()


def test_incomplete_profile_cannot_be_confirmed(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        session = ConversationSession(status="confirming", collected_profile_json='{"name":"仅名称"}')
        db.add(session)
        db.commit()
        response = client.post(f"/api/v1/profile-sessions/{session.id}/confirm")
        assert response.status_code == 422
        assert db.scalar(select(func.count()).select_from(Blogger)) == 0
    finally:
        app.dependency_overrides.clear()

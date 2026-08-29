from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routes import QUESTIONS, get_profile_agent
from app.db.session import get_db
from app.main import app
from app.models import (
    Blogger,
    ConversationMessage,
    ConversationSession,
    DecisionLog,
    MemoryEmbedding,
    MemoryRecord,
)
from app.services.profile_agent import FakeProfileAgent, ProfileAgentError


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
        assert "博主名称" in first.json()["question"]
        assert db.get(ConversationSession, session_id).current_question == "name"

        second = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json={"message": "不知道"},
        )
        assert second.status_code == 200
        assert second.json()["status"] == "collecting"
        assert "name" not in second.json()["collected_profile"]
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


def test_profile_agent_extracts_multiple_fields_and_request_is_idempotent(db):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_profile_agent] = FakeProfileAgent
    client = TestClient(app)
    try:
        session_id = client.post("/api/v1/profile-sessions").json()["session_id"]
        body = {
            "request_id": "profile-multi-request-1",
            "message": (
                "我叫阿黔，主要在抖音做贵州美食和非遗，风格口播，"
                "粉丝1万到10万，变现方式商单和探店，常跑黔东南，"
                "爆款是酸汤鱼，周更。"
            ),
        }
        first = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json=body,
        )
        assert first.status_code == 200
        assert first.json()["status"] == "confirming"
        assert first.json()["collected_profile"]["platform"] == "抖音"
        message_count = db.scalar(select(func.count()).select_from(ConversationMessage))
        repeated = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json=body,
        )
        assert repeated.status_code == 200
        assert repeated.json() == first.json()
        assert db.scalar(select(func.count()).select_from(ConversationMessage)) == message_count
        assert db.scalar(select(func.count()).select_from(Blogger)) == 0
        assert db.scalar(select(func.count()).select_from(MemoryRecord)) == 0
        assert db.scalar(
            select(func.count())
            .select_from(DecisionLog)
            .where(DecisionLog.decision_type == "profile_agent_turn")
        ) == 1
    finally:
        app.dependency_overrides.clear()


def test_profile_agent_failure_preserves_state_and_same_request_can_retry(db):
    def override_db():
        yield db

    failure = ProfileAgentError(
        "PROFILE_AGENT_REQUEST_FAILED",
        "模拟网络失败",
        retryable=True,
        request_id="profile-retry-request-1",
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_profile_agent] = lambda: FakeProfileAgent(fail_with=failure)
    client = TestClient(app)
    try:
        session_id = client.post("/api/v1/profile-sessions").json()["session_id"]
        body = {"request_id": "profile-retry-request-1", "message": "我叫阿黔"}
        failed = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json=body,
        )
        assert failed.status_code == 503
        assert failed.json()["detail"]["retryable"] is True
        session = db.get(ConversationSession, session_id)
        assert session.status == "collecting"
        assert session.collected_profile_json == "{}"
        assert db.scalar(select(func.count()).select_from(Blogger)) == 0
        assert db.scalar(select(func.count()).select_from(MemoryRecord)) == 0

        app.dependency_overrides[get_profile_agent] = FakeProfileAgent
        recovered = client.post(
            f"/api/v1/profile-sessions/{session_id}/messages",
            json=body,
        )
        assert recovered.status_code == 200
        assert recovered.json()["collected_profile"]["name"] == "阿黔"
    finally:
        app.dependency_overrides.clear()


def test_profile_next_question_is_bound_to_backend_next_field(db):
    def override_db():
        yield db

    profile = {
        "name": "逗逗的雀巢",
        "platform": "B站",
        "content_types": ["搞笑娱乐"],
        "style": "搞笑、吐槽",
        "follower_band": "2w",
        "monetization_types": ["广告", "带货"],
    }
    session = ConversationSession(
        status="collecting",
        current_question="routes",
        collected_profile_json=json.dumps(profile, ensure_ascii=False),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_profile_agent] = lambda: FakeProfileAgent(
        response={
            "fields": {"routes": "黔东南"},
            "follow_up_question": "你一般多久更新一个视频？",
        }
    )
    client = TestClient(app)
    try:
        response = client.post(
            f"/api/v1/profile-sessions/{session.id}/messages",
            json={"message": "黔东南"},
        )
        assert response.status_code == 200
        assert response.json()["question"] == QUESTIONS["viral_topic"]
        assert db.get(ConversationSession, session.id).current_question == "viral_topic"
    finally:
        app.dependency_overrides.clear()


def test_profile_answer_is_not_copied_into_an_unrelated_current_field(db):
    def override_db():
        yield db

    profile = {
        "name": "逗逗的雀巢",
        "platform": "B站",
        "content_types": ["搞笑娱乐"],
        "style": "搞笑、吐槽",
        "follower_band": "2w",
        "monetization_types": ["广告", "带货"],
        "routes": "黔东南",
    }
    session = ConversationSession(
        status="collecting",
        current_question="viral_topic",
        collected_profile_json=json.dumps(profile, ensure_ascii=False),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_profile_agent] = lambda: FakeProfileAgent(
        response={
            "fields": {"frequency": "2天"},
            "follow_up_question": "信息已经完整，可以确认画像。",
        }
    )
    client = TestClient(app)
    try:
        response = client.post(
            f"/api/v1/profile-sessions/{session.id}/messages",
            json={"message": "2天"},
        )
        assert response.status_code == 200
        collected = response.json()["collected_profile"]
        assert collected["frequency"] == "2天"
        assert "viral_topic" not in collected
        assert response.json()["question"] == QUESTIONS["viral_topic"]
        assert db.get(ConversationSession, session.id).current_question == "viral_topic"
        decision = db.scalars(
            select(DecisionLog)
            .where(DecisionLog.decision_type == "profile_agent_turn")
            .order_by(DecisionLog.id.desc())
        ).first()
        assert decision is not None
        payload = json.loads(decision.decision)
        assert payload["follow_up_question"] == "信息已经完整，可以确认画像。"
        assert payload["next_field"] == "viral_topic"
        assert payload["adopted_question"] == QUESTIONS["viral_topic"]
    finally:
        app.dependency_overrides.clear()


def test_profile_unknown_route_remains_missing(db):
    def override_db():
        yield db

    profile = {
        "name": "阿黔",
        "platform": "B站",
        "content_types": ["贵州文旅"],
        "style": "口播",
        "follower_band": "2万",
        "monetization_types": ["广告"],
    }
    session = ConversationSession(
        status="collecting",
        current_question="routes",
        collected_profile_json=json.dumps(profile, ensure_ascii=False),
    )
    db.add(session)
    db.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_profile_agent] = lambda: FakeProfileAgent(
        response={"fields": {"routes": "不知道"}}
    )
    client = TestClient(app)
    try:
        response = client.post(
            f"/api/v1/profile-sessions/{session.id}/messages",
            json={"message": "不知道"},
        )
        assert response.status_code == 200
        assert "routes" not in response.json()["collected_profile"]
        assert response.json()["question"] == _fixed_route_clarification()
        assert db.get(ConversationSession, session.id).current_question == "routes"
    finally:
        app.dependency_overrides.clear()


def _fixed_route_clarification() -> str:
    return f"请尽量具体说明“{QUESTIONS['routes']}”；如仍不确定，可再次回答原内容。"

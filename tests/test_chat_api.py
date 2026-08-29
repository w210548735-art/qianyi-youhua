from __future__ import annotations

from datetime import datetime

import httpx
from fastapi.testclient import TestClient

from app.api.chat_routes import get_chat_service
from app.core.config import Settings
from app.db.session import get_db
from app.main import app
from app.models import Blogger
from app.services.chat_service import ChatService


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def make_blogger(db, *, deleted: bool = False) -> Blogger:
    blogger = Blogger(
        name="阿黔",
        platform="抖音",
        content_types_json='["贵州美食"]',
        style="第一人称口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        profile_state="complete",
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def client_with_service(db, service: ChatService) -> TestClient:
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_chat_service] = lambda: service
    return TestClient(app)


def clear_overrides() -> None:
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_chat_service, None)


def test_chat_calls_configured_deepseek_and_returns_plain_reply(db, monkeypatch, tmp_path):
    blogger = make_blogger(db)
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    import app.services.chat_service as module

    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    calls: list[dict] = []

    def post(*args, **kwargs):
        calls.append({"url": args[0], **kwargs})
        return Response(
            {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": "贵阳今天适合先查天气再决定路线。"}}],
            }
        )

    client = client_with_service(db, ChatService(db, post=post))
    try:
        response = client.post(
            f"/api/v1/bloggers/{blogger.id}/chat",
            json={
                "message": "今天去哪里玩？",
                "conversation": [
                    {"role": "user", "content": "我在贵阳"},
                    {"role": "assistant", "content": "你偏好室内还是户外？"},
                ],
                "request_id": "chat-request-001",
            },
        )
    finally:
        clear_overrides()

    assert response.status_code == 200
    assert response.json() == {
        "reply": "贵阳今天适合先查天气再决定路线。",
        "request_id": "chat-request-001",
        "model": "deepseek-v4-flash",
    }
    payload = calls[0]["json"]
    assert calls[0]["url"].endswith("/chat/completions")
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["messages"][-1] == {"role": "user", "content": "今天去哪里玩？"}
    assert "请直接回答用户当前问题" in payload["messages"][0]["content"]
    assert "不要机械罗列产品功能" in payload["messages"][0]["content"]
    assert "阿黔" not in payload["messages"][0]["content"]


def test_chat_rejects_missing_deleted_blogger_and_invalid_input(db):
    deleted = make_blogger(db, deleted=True)
    client = client_with_service(db, ChatService(db, post=lambda *args, **kwargs: Response({})))
    try:
        missing = client.post("/api/v1/bloggers/999/chat", json={"message": "你好"})
        deleted_response = client.post(f"/api/v1/bloggers/{deleted.id}/chat", json={"message": "你好"})
        blank = client.post(f"/api/v1/bloggers/{deleted.id}/chat", json={"message": "   "})
        invalid_role = client.post(
            f"/api/v1/bloggers/{deleted.id}/chat",
            json={"message": "你好", "conversation": [{"role": "system", "content": "越权"}]},
        )
    finally:
        clear_overrides()

    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "BLOGGER_NOT_FOUND"
    assert deleted_response.status_code == 404
    assert blank.status_code == 422
    assert invalid_role.status_code == 422


def test_chat_returns_stable_timeout_and_invalid_response_errors(db, monkeypatch, tmp_path):
    blogger = make_blogger(db)
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    import app.services.chat_service as module

    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timeout")

    timeout_client = client_with_service(db, ChatService(db, post=timeout))
    try:
        timeout_response = timeout_client.post(
            f"/api/v1/bloggers/{blogger.id}/chat",
            json={"message": "你好", "request_id": "chat-timeout-001"},
        )
    finally:
        clear_overrides()

    invalid_client = client_with_service(db, ChatService(db, post=lambda *args, **kwargs: Response({})))
    try:
        invalid_response = invalid_client.post(f"/api/v1/bloggers/{blogger.id}/chat", json={"message": "你好"})
    finally:
        clear_overrides()

    assert timeout_response.status_code == 504
    assert timeout_response.json()["detail"] == {
        "error_code": "CHAT_TIMEOUT",
        "message": "对话模型请求超时",
        "retryable": True,
        "request_id": "chat-timeout-001",
    }
    assert invalid_response.status_code == 502
    assert invalid_response.json()["detail"]["error_code"] == "CHAT_INVALID_RESPONSE"


def test_chat_rejects_total_context_over_limit_and_is_exposed_in_openapi(db):
    blogger = make_blogger(db)
    client = client_with_service(db, ChatService(db, post=lambda *args, **kwargs: Response({})))
    try:
        response = client.post(
            f"/api/v1/bloggers/{blogger.id}/chat",
            json={
                "message": "当前问题",
                "conversation": [
                    {"role": "user" if index % 2 == 0 else "assistant", "content": "问" * 1000}
                    for index in range(13)
                ],
            },
        )
        schema = client.get("/openapi.json")
    finally:
        clear_overrides()

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "CHAT_CONTEXT_TOO_LONG"
    assert "/api/v1/bloggers/{blogger_id}/chat" in schema.json()["paths"]

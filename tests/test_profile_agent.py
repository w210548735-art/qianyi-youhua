from __future__ import annotations

import json

import httpx
import pytest

from app.services.profile_agent import (
    DeepSeekProfileAgent,
    FakeProfileAgent,
    ProfileAgentError,
)


def test_fake_agent_extracts_multiple_fields_and_returns_targeted_question():
    agent = FakeProfileAgent()
    result = agent.extract(
        "我叫阿黔，主要在抖音发布贵州美食和非遗内容，风格口播，粉丝1万到10万，变现方式商单和探店，周更。",
        request_id="multi-1",
    )

    assert result.extracted_fields["name"] == "阿黔"
    assert result.fields["platform"] == "抖音"
    assert result.fields["content_types"] == ["美食", "非遗"]
    assert result.fields["style"] == "口播"
    assert result.fields["follower_band"] == "1万-10万"
    assert result.fields["monetization_types"] == ["商单", "探店"]
    assert result.fields["frequency"] == "周更"
    assert result.follow_up_question is not None
    assert "路线" in result.follow_up_question or "表现较好" in result.follow_up_question


def test_fake_agent_asks_once_for_ambiguous_current_field_and_keeps_profile():
    agent = FakeProfileAgent()
    first = agent.extract(
        "不知道",
        {"name": "阿黔"},
        request_id="ambiguous-1",
        current_field="platform",
    )
    assert first.fields == {"name": "阿黔"}
    assert first.ambiguous_fields == ("platform",)
    assert "平台" in (first.follow_up_question or "")

    second = agent.extract(
        "不知道",
        {"name": "阿黔"},
        request_id="ambiguous-2",
        current_field="platform",
    )
    assert second.ambiguous_fields == ("platform",)
    assert second.follow_up_question == first.follow_up_question


def test_same_request_id_is_idempotent_and_does_not_call_agent_twice():
    agent = FakeProfileAgent()
    first = agent.extract("我叫小黔，在抖音做美食", request_id="same-request")
    first.fields["name"] = "调用方修改不应污染缓存"
    second = agent.extract("完全不同的新输入", request_id="same-request")

    assert agent.call_count == 1
    assert second.fields["name"] == "小黔"
    assert second.request_id == "same-request"


def test_agent_failure_is_explicit_retryable_and_not_cached():
    error = ProfileAgentError("PROFILE_AGENT_REQUEST_FAILED", "网络暂时不可用", request_id="retry-1")
    agent = FakeProfileAgent(fail_with=error)
    with pytest.raises(ProfileAgentError, match="PROFILE_AGENT_REQUEST_FAILED") as caught:
        agent.extract("我叫阿黔", request_id="retry-1")
    assert caught.value.retryable is True
    assert agent.call_count == 0

    agent.fail_with = None
    recovered = agent.extract("我叫阿黔", request_id="retry-1")
    assert recovered.fields["name"] == "阿黔"
    assert agent.call_count == 1


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.raise_called = False

    def raise_for_status(self) -> None:
        self.raise_called = True

    def json(self) -> dict:
        return self.payload


def test_deepseek_profile_agent_uses_configured_model_and_parses_json(tmp_path, monkeypatch):
    key_file = tmp_path / "deepseek-key.txt"
    key_file.write_text("test-secret", encoding="utf-8")
    # Settings 是 frozen dataclass，替换模块对象以注入测试 key 路径。
    from dataclasses import replace

    from app.core.config import settings
    from app.services import profile_agent as module

    monkeypatch.setattr(module, "settings", replace(settings, deepseek_key_file=key_file))
    response = _FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "fields": {
                                    "name": "阿黔",
                                    "platform": "抖音",
                                    "content_types": ["美食", "非遗"],
                                },
                                "ambiguous_fields": ["style"],
                                "follow_up_question": "请具体说明你的创作风格。",
                                "confidence": {"name": 0.99, "platform": 0.95},
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
    )
    captured: dict = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return response

    agent = DeepSeekProfileAgent(timeout_seconds=12.0, post=fake_post)
    result = agent.extract("我叫阿黔，在抖音做美食和非遗", request_id="deepseek-1")

    assert response.raise_called is True
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert result.fields["content_types"] == ["美食", "非遗"]
    assert result.ambiguous_fields == ("style",)

    repeated = agent.extract("不同输入", request_id="deepseek-1")
    assert agent.call_count == 1
    assert repeated.to_dict() == result.to_dict()


def test_deepseek_failure_is_retryable_and_does_not_cache(tmp_path):
    key_file = tmp_path / "deepseek-key.txt"
    key_file.write_text("test-secret", encoding="utf-8")

    def failed_post(*args, **kwargs):
        raise httpx.ConnectError("offline")

    from dataclasses import replace

    from app.core.config import settings
    from app.services import profile_agent as module

    original = module.settings
    module.settings = replace(settings, deepseek_key_file=key_file)
    try:
        agent = DeepSeekProfileAgent(post=failed_post)
        with pytest.raises(ProfileAgentError) as caught:
            agent.extract("我叫阿黔", request_id="network-1")
        assert caught.value.code == "PROFILE_AGENT_REQUEST_FAILED"
        assert caught.value.retryable is True
        assert agent.call_count == 1
    finally:
        module.settings = original


def test_invalid_request_id_is_rejected_without_model_call():
    agent = FakeProfileAgent()
    with pytest.raises(ProfileAgentError, match="PROFILE_REQUEST_ID_REQUIRED"):
        agent.extract("我叫阿黔", request_id=" ")
    assert agent.call_count == 0

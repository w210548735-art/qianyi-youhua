from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.services.output_agent as module
from app.core.config import Settings
from app.services.output_agent import (
    DeepSeekOutputAgent,
    FakeOutputAgent,
    OutputAgentError,
)


def snapshot() -> dict:
    return {
        "blogger_id": 1,
        "profile": {
            "id": 1,
            "platform": "抖音",
            "style": "口播",
            "content_types": ["贵州美食"],
            "frequency": "周更",
        },
        "assessment": {"id": 4, "status": "succeeded", "summary": "三库可用于内容生产"},
        "sources": [{"id": 101, "title": "官方来源"}],
        "assets": [
            {
                "id": 11,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "category": "美食",
                "title": "凯里酸汤鱼",
                "content": "贵州地方传统美食事实",
                "credibility": 5,
                "source_document_ids": [101],
            },
            {
                "id": 12,
                "blogger_id": 1,
                "lib_type": "material",
                "category": "口播",
                "title": "探店口播模板",
                "content": "开场、事实、收束模板",
                "credibility": 1,
            },
            {
                "id": 13,
                "blogger_id": 1,
                "lib_type": "algorithm",
                "category": "抖音",
                "title": "抖音结构检查",
                "content": "检查内容结构",
                "credibility": 1,
            },
        ],
        "places": [
            {
                "id": 21,
                "blogger_id": 1,
                "name": "酸汤体验点",
                "category": "美食",
                "location": "黔东南",
                "like_level": None,
                "est_cost": None,
                "est_benefit": None,
                "fits_koc": None,
                "fits_shoot": None,
            }
        ],
        "task_memory": {"task_id": "output-1", "status": "running"},
        "active_memories": [{"title": "偏好", "content": "口播优先"}],
    }


def test_fake_agent_generates_script_storyboard_and_schedule_with_real_refs():
    agent = FakeOutputAgent()
    context = [{"role": "system", "content": "只使用快照"}]
    script = agent.generate_script(context, snapshot(), "介绍酸汤鱼", request_id="script-1")

    assert {
        "category",
        "title",
        "hook",
        "body",
        "ending",
        "tags",
        "style",
        "platform",
        "source_refs",
    }.issubset(script)
    assert script["platform"] == "抖音"
    assert script["style"] == "口播"
    assert script["source_refs"][0]["asset_id"] == 11

    script["output_id"] = 100
    script["version"] = 2
    storyboard = agent.generate_storyboard(context, snapshot(), script, request_id="storyboard-1")
    assert storyboard["script_id"] == 100
    assert storyboard["script_version"] == 2
    assert storyboard["shots"] and all(shot["source_refs"] for shot in storyboard["shots"])
    assert {"sequence", "visual", "dialogue", "duration", "bgm", "transition", "source_refs"}.issubset(
        storyboard["shots"][0]
    )

    schedule = agent.generate_schedule(context, snapshot(), script, request_id="schedule-1")
    assert schedule["items"][0]["platform"] == "抖音"
    assert schedule["items"][0]["content_type"] == "script"
    assert agent.call_count == 3


def test_fake_agent_is_idempotent_and_failure_is_explicit():
    agent = FakeOutputAgent()
    first = agent.generate_script([], snapshot(), request_id="same")
    second = agent.generate_script([], {**snapshot(), "snapshot_hash": "changed"}, request_id="same")
    assert first == second
    assert agent.call_count == 1

    failing = FakeOutputAgent(fail_with=OutputAgentError("AGENT_TIMEOUT", "超时", retryable=True))
    with pytest.raises(OutputAgentError) as error:
        failing.generate_script([], snapshot(), request_id="failed")
    assert error.value.code == "AGENT_TIMEOUT"
    assert error.value.retryable is True


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


def _valid_script() -> dict:
    return {
        "type": "script",
        "category": "美食",
        "title": "酸汤鱼",
        "hook": "你知道酸汤鱼的故事吗？",
        "body": "只介绍已核验的贵州美食事实。",
        "ending": "关注贵州文旅内容。",
        "tags": ["美食"],
        "style": "口播",
        "platform": "抖音",
        "source_refs": [{"asset_id": 11, "source_document_id": 101}],
    }


def test_deepseek_uses_v4_flash_and_repairs_json_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    responses = ["not-json", json.dumps(_valid_script(), ensure_ascii=False)]
    calls: list[dict] = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return _Response(responses.pop(0))

    agent = DeepSeekOutputAgent(post=fake_post)
    result = agent.generate_script(
        [{"role": "system", "content": "严格引用"}], snapshot(), "介绍酸汤鱼", request_id="repair"
    )
    assert result["title"] == "酸汤鱼"
    assert len(calls) == 2
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert "input_snapshot" in json.loads(calls[0]["messages"][1]["content"])
    assert agent.call_count == 2


def test_deepseek_second_invalid_response_fails_without_third_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _Response("still-not-json")

    agent = DeepSeekOutputAgent(post=fake_post)
    with pytest.raises(OutputAgentError) as error:
        agent.generate_script([], snapshot(), request_id="invalid")
    assert error.value.code == "OUTPUT_INVALID_JSON"
    assert calls == 2
    assert agent.call_count == 2

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.services.feedback_agent as module
from app.core.config import Settings
from app.services.feedback_agent import (
    DeepSeekFeedbackAgent,
    FakeFeedbackAgent,
    FeedbackAgentError,
)


def snapshot(*, source_type: str = "manual", quality: str = "ok") -> dict:
    return {
        "blogger_id": 1,
        "snapshot_hash": "frozen-hash",
        "profile": {
            "id": 1,
            "platform": "抖音",
            "style": "口播",
            "suit_type": None,
            "knowledge_focus": None,
        },
        "output": {"id": 10, "blogger_id": 1, "category": "美食探店", "title": "酸汤鱼"},
        "primary_metric": {
            "id": 20,
            "output_id": 10,
            "source_type": source_type,
            "user_confirmed": source_type == "manual",
        },
        "assets": [
            {
                "id": 30,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "title": "酸汤鱼知识",
                "credibility": 5,
                "source_document_ids": [300],
            },
            {
                "id": 31,
                "blogger_id": 1,
                "lib_type": "material",
                "title": "探店模板",
                "credibility": 4,
                "source_document_ids": [301],
            },
            {
                "id": 32,
                "blogger_id": 1,
                "lib_type": "algorithm",
                "title": "前三秒策略",
                "credibility": 4,
                "source_document_ids": [302],
            },
        ],
        "places": [
            {
                "id": 40,
                "blogger_id": 1,
                "name": "凯里酸汤鱼店",
                "association_source": "output_place",
                "association_confidence": "high",
                "est_benefit": None,
                "est_cost": None,
            }
        ],
        "deterministic_analysis": {
            "overall_status": quality,
            "sample_quality": {"status": quality, "historical_sample_count": 3},
            "performance": {"trend": "up", "category": "美食探店"},
            "business": {"status": "data_insufficient"},
        },
        "evidence_whitelist": [
            {"evidence_type": "metric", "ref_id": 20, "claim": "用户确认的手工指标"},
            {"evidence_type": "output", "ref_id": 10, "claim": "当前产出"},
            {"evidence_type": "asset", "ref_id": 30, "claim": "当前产出引用知识"},
            {"evidence_type": "asset", "ref_id": 31, "claim": "当前产出引用素材"},
            {"evidence_type": "asset", "ref_id": 32, "claim": "当前产出引用算法"},
            {"evidence_type": "place", "ref_id": 40, "claim": "显式关联地点"},
        ],
        "task_memory": {"task_id": "feedback-1", "status": "running"},
        "active_memories": [{"id": 50, "blogger_id": 1, "status": "active", "content": "口播优先"}],
    }


def test_fake_feedback_agent_is_deterministic_complete_and_never_applies_changes():
    agent = FakeFeedbackAgent()
    first = agent.analyze([], snapshot(), "请分析", request_id="same")
    second = agent.analyze([], {**snapshot(), "snapshot_hash": "changed"}, request_id="same")

    assert first == second
    assert agent.call_count == 1
    assert {
        "suit_type_candidates",
        "knowledge_focus_candidates",
        "pitfalls",
        "asset_effects",
        "place_effects",
        "library_evolution",
        "main_direction_candidates",
        "summary",
    }.issubset(first)
    assert {item["lib_type"] for item in first["library_evolution"]} == {
        "knowledge",
        "material",
        "algorithm",
    }
    assert all(item["reason"] and item["evidence_refs"] for item in first["library_evolution"])
    assert first["place_effects"][0]["after"] is None
    assert first["place_effects"][0]["applicable"] is False


def test_fake_feedback_agent_marks_simulated_as_simulation_only_and_insufficient_as_empty():
    simulated = FakeFeedbackAgent().analyze([], snapshot(source_type="simulated"), request_id="sim")
    assert all(item["simulation_only"] for item in simulated["place_effects"])
    assert all(item["after"] is None for item in simulated["place_effects"])

    insufficient = FakeFeedbackAgent().analyze([], snapshot(quality="data_insufficient"), request_id="few")
    for field in (
        "suit_type_candidates",
        "knowledge_focus_candidates",
        "pitfalls",
        "asset_effects",
        "place_effects",
        "library_evolution",
        "main_direction_candidates",
    ):
        assert insufficient[field] == []
    assert insufficient["data_quality"]["status"] == "data_insufficient"


def test_agent_rejects_cross_blogger_or_wrong_metric_chain_before_prompting():
    cross = snapshot()
    cross["output"]["blogger_id"] = 2
    with pytest.raises(FeedbackAgentError) as error:
        FakeFeedbackAgent().analyze([], cross, request_id="cross")
    assert error.value.code == "FEEDBACK_SNAPSHOT_INVALID"


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


def _valid_payload() -> dict:
    return FakeFeedbackAgent().analyze([], snapshot(), request_id="payload")


def test_deepseek_uses_v4_flash_and_repairs_invalid_or_incomplete_json_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    responses = [
        json.dumps({"summary": "字段不完整"}, ensure_ascii=False),
        json.dumps(_valid_payload(), ensure_ascii=False),
    ]
    calls: list[dict] = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return _Response(responses.pop(0))

    agent = DeepSeekFeedbackAgent(post=fake_post)
    result = agent.analyze(
        [{"role": "system", "content": "只使用冻结证据"}],
        snapshot(),
        "分析反馈",
        request_id="repair-request",
    )

    assert result["summary"]
    assert len(calls) == 2
    assert calls[0]["model"] == "deepseek-v4-flash"
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert sent["input_snapshot"]["blogger_id"] == 1
    assert sent["input_snapshot"]["task_memory"]
    assert sent["input_snapshot"]["active_memories"]
    assert agent.prompt_version == "phase4-feedback-v1"
    assert agent.last_request_id == "repair-request"


def test_deepseek_second_invalid_response_has_stable_error_without_third_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("test-key", encoding="utf-8")
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _Response("not-json")

    agent = DeepSeekFeedbackAgent(post=fake_post)
    with pytest.raises(FeedbackAgentError) as error:
        agent.analyze([], snapshot(), request_id="invalid")

    assert error.value.code == "FEEDBACK_INVALID_JSON"
    assert error.value.request_id == "invalid"
    assert calls == 2
    assert agent.call_count == 2

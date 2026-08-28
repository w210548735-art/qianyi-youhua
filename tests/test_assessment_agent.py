from __future__ import annotations

import json

from app.core.config import Settings
from app.services import assessment_agent as module
from app.services.assessment_agent import (
    AssessmentAgentError,
    DeepSeekAssessmentAgent,
    FakeAssessmentAgent,
)


def analysis_fixture() -> dict:
    return {
        "blogger_id": 1,
        "snapshot_hash": "snapshot-1",
        "libraries": {
            "knowledge": {"count": 2},
            "material": {"count": 2},
            "algorithm": {"count": 1},
        },
        "counts": {"knowledge": 2, "material": 2, "algorithm": 1, "total": 5},
        "assets": [
            {
                "id": 11,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "category": "美食",
                "title": "贵州酸汤",
                "content": "权威来源事实",
                "source_document_ids": [101],
                "sources": [{"id": 101, "title": "官方来源"}],
            },
            {
                "id": 12,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "category": "美食",
                "title": "地方风味",
                "content": "待补充来源",
                "source_document_ids": [],
                "sources": [],
            },
            {
                "id": 21,
                "blogger_id": 1,
                "lib_type": "material",
                "category": "口播模板",
                "title": "探店开场",
                "content": "素材模板",
                "source_document_ids": [],
                "sources": [],
            },
            {
                "id": 31,
                "blogger_id": 1,
                "lib_type": "algorithm",
                "category": "抖音策略",
                "title": "标题结构",
                "content": "算法策略",
                "source_document_ids": [],
                "sources": [],
            },
            {
                "id": 999,
                "blogger_id": 2,
                "lib_type": "knowledge",
                "category": "不应引用",
                "title": "其他博主资产",
                "content": "不可泄露",
                "source_document_ids": [9991],
                "sources": [{"id": 9991, "title": "其他来源"}],
            },
        ],
        "source_coverage": {"with_source": 1, "without_source": 3, "ratio": 0.25},
        "relations": [
            {"from_asset_id": 11, "to_asset_id": 21, "from_lib_type": "knowledge", "to_lib_type": "material"}
        ],
        "core_assets": [{"asset_id": 11, "title": "贵州酸汤", "lib_type": "knowledge"}],
        "weak_categories": [{"category": "景区", "count": 0, "reason": "画像方向缺少对应资产"}],
        "profile_direction_coverage": {"missing": ["景区", "非遗"]},
        "feature_readiness": {
            "script_generation": {"ready": True, "missing_items": []},
            "publishing": {"ready": False, "missing_items": ["暂无平台发布数据与授权"]},
        },
    }


def test_fake_assessment_produces_complete_indicators_and_only_valid_evidence():
    result = FakeAssessmentAgent().assess([], analysis_fixture(), request_id="assessment-1")

    assert len(result["indicators"]) >= 3
    assert round(sum(item["weight"] for item in result["indicators"]), 4) == 100
    assert 0 <= result["overall_score"] <= 100
    assert result["library_structure"]["libraries"]["knowledge"]["count"] == 2
    assert result["core_assets"][0]["asset_id"] == 11
    assert 999 not in result["library_structure"]["libraries"]["knowledge"]["asset_ids"]
    assert result["feature_readiness"]["publishing"]["ready"] is False
    for indicator in result["indicators"]:
        assert {
            "name",
            "meaning",
            "score_logic",
            "business_meaning",
            "weight",
            "weight_reason",
            "score",
            "reason",
            "evidence_refs",
        }.issubset(indicator)
        for ref in indicator["evidence_refs"]:
            assert (
                ref.get("asset_id") in {11, 12, 21, 31}
                or ref.get("source_document_id") == 101
                or (
                    ref.get("evidence_type") == "relation"
                    and ref.get("from_asset_id") in {11, 12, 21, 31}
                    and ref.get("to_asset_id") in {11, 12, 21, 31}
                )
            )
            assert ref.get("asset_id") != 999


def test_fake_assessment_uses_deterministic_fallback_and_is_idempotent():
    agent = FakeAssessmentAgent()
    first = agent.assess([{"role": "system", "content": "规则"}], analysis_fixture(), request_id="same")
    second = agent.assess([{"role": "user", "content": "不同输入但相同任务"}], analysis_fixture(), request_id="same")

    assert first == second
    assert agent.call_count == 1


def test_fake_agent_failure_has_explicit_error():
    agent = FakeAssessmentAgent(
        fail_with=AssessmentAgentError("AGENT_TIMEOUT", "测试超时", retryable=True)
    )
    try:
        agent.assess([], analysis_fixture(), request_id="failed")
    except AssessmentAgentError as exc:
        assert exc.code == "AGENT_TIMEOUT"
        assert exc.retryable is True
    else:  # pragma: no cover - 仅用于让断言失败信息明确
        raise AssertionError("expected AssessmentAgentError")


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


def valid_model_payload() -> dict:
    return {
        "indicators": [
            {
                "name": "结构完整度",
                "meaning": "三库覆盖",
                "score_logic": "按库是否非空",
                "business_meaning": "后续输入基础",
                "weight": 40,
                "weight_reason": "基础门槛",
                "score": 80,
                "reason": "三个库均有数据",
                "evidence_refs": [{"asset_id": 11}],
            },
            {
                "name": "来源覆盖度",
                "meaning": "来源可追溯",
                "score_logic": "来源占比",
                "business_meaning": "降低事实风险",
                "weight": 30,
                "weight_reason": "可信度重要",
                "score": 25,
                "reason": "仅一条有来源",
                "evidence_refs": [{"source_document_id": 101}],
            },
            {
                "name": "跨库关联度",
                "meaning": "库间语义关系",
                "score_logic": "关系数量",
                "business_meaning": "支撑协同使用",
                "weight": 30,
                "weight_reason": "能力衔接",
                "score": 20,
                "reason": "关系较少",
                "evidence_refs": [{"asset_id": 11}],
            },
        ],
        "summary": "模型摘要",
        "overall_score": 999,
    }


def test_deepseek_invalid_json_is_repaired_at_most_once(monkeypatch):
    key_file = module.settings.deepseek_key_file
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    responses = ["not-json", json.dumps(valid_model_payload(), ensure_ascii=False)]
    calls: list[dict] = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return FakeResponse(responses.pop(0))

    agent = DeepSeekAssessmentAgent(post=fake_post)
    result = agent.assess(
        [{"role": "system", "content": "系统规则"}],
        analysis_fixture(),
        request_id="repair-1",
    )

    assert len(calls) == 2
    assert agent.call_count == 2
    assert result["summary"] == "模型摘要"
    assert result["overall_score"] != 999
    assert calls[1]["model"] == "deepseek-v4-flash"


def test_deepseek_normalizes_common_indicator_alias_before_strict_validation(monkeypatch):
    key_file = module.settings.deepseek_key_file
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))
    payload = valid_model_payload()
    payload["assessment_indicators"] = payload.pop("indicators")

    def fake_post(*args, **kwargs):
        return FakeResponse(json.dumps(payload, ensure_ascii=False))

    agent = DeepSeekAssessmentAgent(post=fake_post)
    result = agent.assess(
        [{"role": "system", "content": "系统规则"}],
        analysis_fixture(),
        request_id="alias-1",
    )

    assert len(result["indicators"]) == 3
    assert agent.call_count == 1


def test_deepseek_second_invalid_json_fails_without_cache(monkeypatch):
    key_file = module.settings.deepseek_key_file
    monkeypatch.setattr(module, "settings", Settings(deepseek_key_file=key_file))

    def fake_post(*args, **kwargs):
        return FakeResponse("not-json")

    agent = DeepSeekAssessmentAgent(post=fake_post)
    try:
        agent.assess([{"role": "system", "content": "规则"}], analysis_fixture(), request_id="invalid-1")
    except AssessmentAgentError as exc:
        assert exc.code == "AGENT_INVALID_JSON"
        assert "修复" in exc.message
    else:  # pragma: no cover
        raise AssertionError("expected invalid JSON error")
    assert agent.call_count == 2

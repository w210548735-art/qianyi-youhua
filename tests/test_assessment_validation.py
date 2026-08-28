from __future__ import annotations

import pytest

from app.services.assessment_validation_service import (
    AssessmentValidationError,
    AssessmentValidationService,
)


def _snapshot() -> dict:
    return {
        "blogger_id": 1,
        "blogger": {"id": 1, "name": "测试博主"},
        "libraries": {
            "knowledge": {"count": 1},
            "material": {"count": 1},
            "algorithm": {"count": 1},
        },
        "assets": [
            {
                "id": 11,
                "blogger_id": 1,
                "title": "知识事实",
                "lib_type": "knowledge",
                "source_document_ids": [101],
            },
            {"id": 12, "blogger_id": 1, "title": "素材", "lib_type": "material"},
            {"id": 13, "blogger_id": 1, "title": "算法", "lib_type": "algorithm"},
        ],
        "sources": [{"id": 101, "title": "官方来源"}],
        "relations": [
            {
                "from_asset_id": 11,
                "from_lib_type": "knowledge",
                "to_asset_id": 12,
                "to_lib_type": "material",
                "similarity": 0.8,
            }
        ],
        "missing_items": ["暂无平台发布数据"],
        "feature_readiness": {
            "script_generation": {"ready": True, "missing_items": []},
            "publishing": {"ready": False, "missing_items": ["暂无平台授权"]},
        },
    }


def _report() -> dict:
    return {
        "library_analysis": {"future_data": {"output": "暂无数据", "effect": "暂无数据"}},
        "feature_readiness": {
            "script_generation": {"ready": True, "missing_items": []},
            "publishing": {"ready": False, "missing_items": ["暂无平台授权"]},
        },
        "missing_items": ["暂无平台授权"],
        "indicators": [
            {
                "name": "三库关系覆盖度",
                "meaning": "衡量知识库与素材库、算法库的跨库关系",
                "score_logic": "按关系证据覆盖计算",
                "business_meaning": "判断后续内容生产的基础完整性",
                "weight": 0.5,
                "weight_reason": "跨库关系是核心风险",
                "score": 80,
                "reason": "知识与素材有真实关系证据",
                "evidence": [{"evidence_type": "relation", "from_asset_id": 11, "to_asset_id": 12}],
            },
            {
                "name": "知识可信度",
                "meaning": "衡量知识资产可信度",
                "score_logic": "按来源和可信度评分",
                "business_meaning": "减少内容事实风险",
                "weight": 0.3,
                "weight_reason": "事实可信是发布前提",
                "score": 70,
                "reason": "有官方来源",
                "evidence": [{"evidence_type": "asset", "asset_id": 11}],
            },
            {
                "name": "来源覆盖度",
                "meaning": "衡量来源文档覆盖",
                "score_logic": "有来源资产占比",
                "business_meaning": "支持事实追溯",
                "weight": 0.2,
                "weight_reason": "来源可追溯",
                "score": 60,
                "reason": "来源记录可追溯",
                "evidence": [{"evidence_type": "source_document", "source_document_id": 101}],
            },
        ],
        "overall_score": 1,
    }


def test_validation_normalizes_weights_and_recomputes_score_from_backend(db=None):
    service = AssessmentValidationService()
    snapshot = _snapshot()
    normalized = service.validate_and_normalize(_report(), snapshot)

    assert sum(item["weight"] for item in normalized["indicators"]) == pytest.approx(100)
    assert [item["weight_ratio"] for item in normalized["indicators"]] == pytest.approx([0.5, 0.3, 0.2])
    assert normalized["overall_score"] == pytest.approx(73)
    assert normalized["overall_score"] != _report()["overall_score"]
    assert normalized["validation"]["valid"] is True


def test_validation_rejects_false_evidence_and_missing_indicator_fields():
    service = AssessmentValidationService()
    snapshot = _snapshot()
    invalid = _report()
    invalid["indicators"][1]["evidence"] = [{"evidence_type": "asset", "asset_id": 999}]
    with pytest.raises(AssessmentValidationError) as error:
        service.validate_and_normalize(invalid, snapshot)
    assert error.value.code == "EVIDENCE_REFERENCE_INVALID"

    too_few = _report()
    too_few["indicators"] = too_few["indicators"][:2]
    with pytest.raises(AssessmentValidationError) as error:
        service.validate_and_normalize(too_few, snapshot)
    assert error.value.code == "INDICATOR_RULE_VIOLATION"


def test_validation_rejects_mixed_asset_and_source_reference_not_linked_in_snapshot():
    service = AssessmentValidationService()
    report = _report()
    report["indicators"][1]["evidence"] = [
        {
            "evidence_type": "asset",
            "asset_id": 12,
            "source_document_id": 101,
        }
    ]

    with pytest.raises(AssessmentValidationError) as error:
        service.validate_and_normalize(report, _snapshot())

    assert error.value.code == "EVIDENCE_REFERENCE_INVALID"


def test_validation_requires_missing_items_for_unready_features():
    service = AssessmentValidationService()
    snapshot = _snapshot()
    report = _report()
    report["feature_readiness"]["publishing"] = {"ready": False, "missing_items": []}
    report["missing_items"] = []

    with pytest.raises(AssessmentValidationError) as error:
        service.validate_and_normalize(report, snapshot)

    assert error.value.code == "INDICATOR_RULE_VIOLATION"


def test_validation_calculates_weighted_score_for_ratio_or_percentage_weights():
    service = AssessmentValidationService()
    indicators = [
        {"weight": 50, "score": 80},
        {"weight": 30, "score": 70},
        {"weight": 20, "score": 60},
    ]
    assert service.calculate_overall_score(indicators) == pytest.approx(73)
    assert service.calculate_overall_score(
        {"indicators": [{"weight": 0.5, "score": 80}, {"weight": 0.5, "score": 60}]}
    ) == pytest.approx(70)

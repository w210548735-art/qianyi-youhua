from __future__ import annotations

import json

import pytest

from app.models import Assessment, AssessmentIndicator, Blogger
from app.services.assessment_comparison_service import (
    AssessmentComparisonError,
    AssessmentComparisonService,
)


def create_blogger(db, name: str) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def assessment_payload(counts: dict[str, int], weak: list[str]) -> dict:
    return {
        "libraries": {
            lib_type: {"count": count}
            for lib_type, count in counts.items()
        },
        "weak_points": weak,
    }


def add_assessment(
    db,
    blogger_id: int,
    key: str,
    score: int,
    counts: dict[str, int],
    weak: list[str],
    indicator_names: list[str],
) -> Assessment:
    assessment = Assessment(
        blogger_id=blogger_id,
        status="succeeded",
        idempotency_key=key,
        snapshot_hash=f"hash-{key}",
        input_snapshot_json="{}",
        library_analysis_json=json.dumps(assessment_payload(counts, weak), ensure_ascii=False),
        feature_readiness_json=json.dumps(
            {
                "script_generation": {"ready": counts["material"] > 0, "missing_items": []},
                "publishing": {"ready": False, "missing_items": ["暂无平台授权"]},
            },
            ensure_ascii=False,
        ),
        summary=f"体检{key}",
        overall_score=score,
    )
    db.add(assessment)
    db.flush()
    for ordinal, name in enumerate(indicator_names, start=1):
        db.add(
            AssessmentIndicator(
                assessment_id=assessment.id,
                ordinal=ordinal,
                name=name,
                meaning=f"{name}含义",
                score_logic="按真实快照评分",
                business_meaning="判断后续功能输入",
                weight=100 / len(indicator_names),
                weight_reason="均衡覆盖",
                score=min(100, score + ordinal),
                reason="有快照证据",
                evidence_json="[]",
            )
        )
    db.commit()
    db.refresh(assessment)
    return assessment


def test_compare_scores_libraries_weak_points_and_indicator_changes(db):
    blogger = create_blogger(db, "比较博主")
    left = add_assessment(
        db,
        blogger.id,
        "left",
        50,
        {"knowledge": 2, "material": 1, "algorithm": 1},
        ["景区", "无来源资产"],
        ["结构完整度", "来源覆盖度", "旧指标"],
    )
    right = add_assessment(
        db,
        blogger.id,
        "right",
        75,
        {"knowledge": 3, "material": 4, "algorithm": 1},
        ["无来源资产", "非遗"],
        ["结构完整度", "来源覆盖度", "新指标"],
    )

    result = AssessmentComparisonService(db).compare(blogger.id, left.id, right.id)

    assert result["blogger_id"] == blogger.id
    assert result["overall_score"]["delta"] == 25
    assert result["library_metrics"]["material"]["count_delta"] == 3
    assert result["weak_points"]["added"] == ["非遗"]
    assert result["weak_points"]["removed"] == ["景区"]
    changes = result["indicator_changes"]
    assert [item["name"] for item in changes["added"]] == ["新指标"]
    assert [item["name"] for item in changes["removed"]] == ["旧指标"]
    assert changes["matched"][0]["name"] == "来源覆盖度"


def test_compare_does_not_expose_other_blogger_assessment(db):
    owner = create_blogger(db, "拥有体检的博主")
    other = create_blogger(db, "无权访问的博主")
    owner_assessment = add_assessment(
        db,
        owner.id,
        "owner",
        60,
        {"knowledge": 1, "material": 1, "algorithm": 1},
        [],
        ["结构", "来源", "关系"],
    )
    other_assessment = add_assessment(
        db,
        other.id,
        "other",
        99,
        {"knowledge": 9, "material": 9, "algorithm": 9},
        ["不应泄露"],
        ["别人的指标", "别人的来源", "别人的关系"],
    )

    with pytest.raises(AssessmentComparisonError) as exc_info:
        AssessmentComparisonService(db).compare(owner.id, owner_assessment.id, other_assessment.id)

    assert exc_info.value.code == "ASSESSMENT_NOT_FOUND"
    assert "不应泄露" not in str(exc_info.value)


def test_compare_deleted_blogger_is_not_available(db):
    blogger = create_blogger(db, "待删除博主")
    blogger.deleted_at = __import__("datetime").datetime.utcnow()
    db.commit()

    with pytest.raises(AssessmentComparisonError) as exc_info:
        AssessmentComparisonService(db).compare(blogger.id, 1, 2)

    assert exc_info.value.code == "BLOGGER_NOT_FOUND"

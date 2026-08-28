from __future__ import annotations

import hashlib
from datetime import date, datetime

import pytest

from app.models.entities import (
    Asset,
    AssetPlace,
    Blogger,
    MemoryRecord,
    Metric,
    Output,
    OutputAsset,
    OutputPlace,
    Place,
    Schedule,
)
from app.services.feedback_analysis_service import (
    FeedbackAnalysisError,
    FeedbackAnalysisService,
)


def _blogger(name: str) -> Blogger:
    return Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食探店"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
    )


def _output(owner: Blogger, title: str, category: str = "美食探店") -> Output:
    return Output(
        blogger_id=owner.id,
        type="script",
        category=category,
        title=title,
        content_json=f'{{"body":"{title} 内容"}}',
        status="succeeded",
        version=1,
    )


def _metric(db, owner: Blogger, output: Output, day: int, views: int, *, actual=False) -> Metric:
    schedule = Schedule(
        blogger_id=owner.id,
        output_id=output.id,
        plan_date=date(2026, 8, day),
        platform="抖音",
        content_type="script",
        title=output.title,
        status="collected",
    )
    db.add(schedule)
    db.flush()
    metric = Metric(
        output_id=output.id,
        schedule_id=schedule.id,
        source_type="manual",
        user_confirmed=True,
        views=views,
        likes=views // 10,
        comments=5,
        collects=2,
        shares=3,
        actual_revenue=1000.0 if actual else None,
        actual_cost=400.0 if actual else None,
        idempotency_key=f"metric-{output.id}",
        collected_at=datetime(2026, 8, day, 12),
    )
    db.add(metric)
    db.flush()
    return metric


@pytest.fixture()
def feedback_rows(db):
    owner = _blogger("当前博主")
    other = _blogger("其他博主")
    db.add_all([owner, other])
    db.flush()

    prior_one = _output(owner, "历史酸汤鱼一")
    prior_two = _output(owner, "历史酸汤鱼二")
    current = _output(owner, "凯里酸汤鱼店探店")
    landscape = _output(owner, "单条风景", "纯风景")
    other_output = _output(other, "跨博主产出")
    db.add_all([prior_one, prior_two, current, landscape, other_output])
    db.flush()
    _metric(db, owner, prior_one, 1, 50)
    _metric(db, owner, prior_two, 2, 80)
    primary = _metric(db, owner, current, 3, 100, actual=True)
    landscape_metric = _metric(db, owner, landscape, 4, 999)
    other_metric = _metric(db, other, other_output, 5, 100)

    trusted = Asset(
        blogger_id=owner.id,
        lib_type="knowledge",
        category="美食",
        title="酸汤鱼知识",
        content="用户确认知识",
        tags_json="[]",
        source_type="user_confirmed",
        credibility=4,
        origin="manual",
        dedupe_key=hashlib.sha256(b"trusted").hexdigest(),
        manual_locked=True,
    )
    weak = Asset(
        blogger_id=owner.id,
        lib_type="material",
        category="模板",
        title="无来源低可信模板",
        content="弱来源",
        tags_json="[]",
        source_type="generated_template",
        credibility=1,
        origin="seed",
        dedupe_key=hashlib.sha256(b"weak").hexdigest(),
    )
    db.add_all([trusted, weak])
    db.flush()
    db.add_all(
        [
            OutputAsset(output_id=current.id, asset_id=trusted.id, usage_type="knowledge", claim="引用"),
            OutputAsset(output_id=current.id, asset_id=weak.id, usage_type="material", claim="引用"),
        ]
    )

    explicit = Place(
        blogger_id=owner.id,
        name="显式地点",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key=hashlib.sha256(b"explicit").hexdigest(),
    )
    reverse = Place(
        blogger_id=owner.id,
        name="资产反查地点",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key=hashlib.sha256(b"reverse").hexdigest(),
    )
    matched = Place(
        blogger_id=owner.id,
        name="凯里酸汤鱼店",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key=hashlib.sha256(b"matched").hexdigest(),
    )
    db.add_all([explicit, reverse, matched])
    db.flush()
    explicit_relation = OutputPlace(
        output_id=current.id,
        place_id=explicit.id,
        role="visit",
        sequence=1,
        claim="显式地点",
    )
    reverse_relation = AssetPlace(
        asset_id=trusted.id,
        place_id=reverse.id,
        relation_type="地点知识",
        source_type="manual",
    )
    db.add_all([explicit_relation, reverse_relation])
    db.add(
        MemoryRecord(
            blogger_id=owner.id,
            memory_type="preference",
            title="长期偏好",
            content="口播优先",
            source_type="user_confirmed",
            confidence=1.0,
            status="active",
            version=1,
            content_hash=hashlib.sha256(b"memory").hexdigest(),
        )
    )
    db.commit()
    return {
        "owner": owner,
        "current": current,
        "primary": primary,
        "landscape": landscape,
        "landscape_metric": landscape_metric,
        "other_metric": other_metric,
        "trusted": trusted,
        "weak": weak,
        "explicit": explicit,
        "reverse": reverse,
        "matched": matched,
        "explicit_relation": explicit_relation,
        "reverse_relation": reverse_relation,
    }


def test_build_snapshot_computes_relative_facts_actual_net_and_freezes_evidence(db, feedback_rows):
    rows = feedback_rows
    snapshot = FeedbackAnalysisService(db).build_snapshot(
        rows["owner"].id, rows["current"].id, rows["primary"].id
    )

    assert snapshot["deterministic_analysis"]["overall_status"] == "ok"
    assert snapshot["deterministic_analysis"]["engagement"]["rate"] == pytest.approx(0.2)
    comparison = snapshot["deterministic_analysis"]["historical_comparison"]
    assert comparison["views_median"] == 65.0 and comparison["trend"] == "up"
    business = snapshot["deterministic_analysis"]["business"]
    assert business["status"] == "actual" and business["actual_net"] == 600.0
    assert [item["id"] for item in snapshot["assets"]] == [rows["trusted"].id]
    assert [item["id"] for item in snapshot["places"]] == [rows["explicit"].id]
    assert snapshot["places"][0]["association_source"] == "output_place"
    assert snapshot["active_memories"][0]["status"] == "active"
    assert snapshot["snapshot_hash"] == FeedbackAnalysisService.hash_snapshot(snapshot)
    evidence_keys = {
        (item["evidence_type"], item["ref_id"]) for item in snapshot["evidence_whitelist"]
    }
    assert ("asset", rows["weak"].id) not in evidence_keys
    assert ("metric", rows["primary"].id) in evidence_keys


def test_snapshot_hash_ignores_task_context_but_changes_for_business_facts(db, feedback_rows):
    rows = feedback_rows
    snapshot = FeedbackAnalysisService(db).build_snapshot(
        rows["owner"].id, rows["current"].id, rows["primary"].id
    )
    original = snapshot["snapshot_hash"]

    snapshot["task_memory"] = {
        "id": "task-1",
        "status": "running",
        "updated_at": "2099-01-01T00:00:00",
        "recovery_state": {"checkpoint": 99},
    }
    snapshot["active_memories"].append(
        {"id": 999, "blogger_id": rows["owner"].id, "status": "candidate"}
    )
    snapshot["user_instruction"] = "追加一条任务消息"
    assert FeedbackAnalysisService.hash_snapshot(snapshot) == original

    for field, value in (
        ("profile", {**snapshot["profile"], "suit_type": "新方向"}),
        ("output", {**snapshot["output"], "version": 2}),
        ("primary_metric", {**snapshot["primary_metric"], "views": 101}),
        ("assets", [{**snapshot["assets"][0], "effect_weight": 0.9}]),
        ("places", [{**snapshot["places"][0], "est_benefit": 10.0}]),
    ):
        changed = dict(snapshot)
        changed[field] = value
        assert FeedbackAnalysisService.hash_snapshot(changed) != original


def test_place_resolution_priority_falls_back_to_asset_then_low_confidence_name(db, feedback_rows):
    rows = feedback_rows
    service = FeedbackAnalysisService(db)
    db.delete(rows["explicit_relation"])
    db.commit()
    reverse = service.build_snapshot(rows["owner"].id, rows["current"].id, rows["primary"].id)
    assert [item["id"] for item in reverse["places"]] == [rows["reverse"].id]
    assert reverse["places"][0]["association_source"] == "asset_place"

    db.delete(rows["reverse_relation"])
    db.commit()
    matched = service.build_snapshot(rows["owner"].id, rows["current"].id, rows["primary"].id)
    assert [item["id"] for item in matched["places"]] == [rows["matched"].id]
    assert matched["places"][0]["association_confidence"] == "low"


def test_single_category_sample_is_data_insufficient_despite_high_absolute_views(db, feedback_rows):
    rows = feedback_rows
    snapshot = FeedbackAnalysisService(db).build_snapshot(
        rows["owner"].id, rows["landscape"].id, rows["landscape_metric"].id
    )
    analysis = snapshot["deterministic_analysis"]
    assert analysis["overall_status"] == "data_insufficient"
    assert analysis["sample_quality"]["historical_sample_count"] == 0
    assert analysis["historical_comparison"]["trend"] == "unknown"


def test_cross_output_metric_and_unrelated_place_update_fail_stably(db, feedback_rows):
    rows = feedback_rows
    service = FeedbackAnalysisService(db)
    with pytest.raises(FeedbackAnalysisError) as error:
        service.build_snapshot(rows["owner"].id, rows["current"].id, rows["other_metric"].id)
    assert error.value.code == "METRIC_NOT_FOUND"

    with pytest.raises(FeedbackAnalysisError) as error:
        service.build_snapshot(
            rows["owner"].id,
            rows["current"].id,
            rows["primary"].id,
            user_confirmed_place_updates={rows["matched"].id: {"est_benefit": 200.0}},
        )
    assert error.value.code == "FEEDBACK_INVALID_PLACE_UPDATE"

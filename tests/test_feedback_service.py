from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Asset,
    Blogger,
    DecisionLog,
    FeedbackEvidence,
    FeedbackRun,
    MemoryRecord,
    Metric,
    Output,
    OutputAsset,
    OutputPlace,
    Place,
    Schedule,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.feedback_agent import FakeFeedbackAgent
from app.services.feedback_service import FeedbackService, FeedbackServiceError
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService


def _seed_feedback(db, *, source_type: str = "manual") -> dict[str, object]:
    owner = Blogger(
        name="反馈博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
        profile_state="complete",
        suit_type="贵州综合",
    )
    other = Blogger(
        name="其他博主",
        platform="抖音",
        content_types_json='["风景"]',
        style="记录",
        follower_band="1万以下",
        monetization_types_json='["无"]',
        profile_state="complete",
    )
    db.add_all([owner, other])
    db.flush()
    outputs: list[Output] = []
    metrics: list[Metric] = []
    for index, views in enumerate((100, 120, 500), start=1):
        output = Output(
            blogger_id=owner.id,
            type="script",
            category="酸汤美食",
            title=f"酸汤内容{index}",
            content_json="{}",
            status="succeeded",
            version=1,
        )
        db.add(output)
        db.flush()
        schedule = Schedule(
            blogger_id=owner.id,
            output_id=output.id,
            plan_date=date(2026, 8, index),
            platform="抖音",
            content_type="视频",
            title=output.title,
            status="collected",
        )
        db.add(schedule)
        db.flush()
        metric = Metric(
            output_id=output.id,
            schedule_id=schedule.id,
            source_type=source_type,
            views=views,
            likes=views // 5,
            comments=10,
            collects=5,
            shares=3,
            user_confirmed=source_type == "manual",
            actual_revenue=800 if source_type == "manual" and index == 3 else None,
            actual_cost=300 if source_type == "manual" and index == 3 else None,
            idempotency_key=f"metric-{source_type}-{index}",
            collected_at=datetime(2026, 8, index, 12),
        )
        db.add(metric)
        outputs.append(output)
        metrics.append(metric)
    assets: list[Asset] = []
    for lib_type in ("knowledge", "material", "algorithm"):
        asset = Asset(
            blogger_id=owner.id,
            lib_type=lib_type,
            category="酸汤美食",
            title=f"{lib_type}资产",
            content="用户确认的可信内容",
            tags_json='["酸汤"]',
            source_type="user_confirmed",
            credibility=5,
            origin="manual",
            dedupe_key=hashlib.sha256(f"{owner.id}-{lib_type}".encode()).hexdigest(),
            manual_locked=lib_type == "knowledge",
        )
        db.add(asset)
        db.flush()
        db.add(
            OutputAsset(
                output_id=outputs[-1].id,
                asset_id=asset.id,
                usage_type=lib_type,
                claim="显式引用",
            )
        )
        assets.append(asset)
    place = Place(
        blogger_id=owner.id,
        name="酸汤店",
        category="美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key=hashlib.sha256(b"feedback-place").hexdigest(),
        est_cost=200,
        est_benefit=600,
    )
    db.add(place)
    db.flush()
    db.add(
        OutputPlace(
            output_id=outputs[-1].id,
            place_id=place.id,
            role="primary",
            sequence=1,
            claim="显式关联",
        )
    )
    db.commit()
    return {
        "owner": owner,
        "other": other,
        "output": outputs[-1],
        "metric": metrics[-1],
        "assets": assets,
        "place": place,
    }


def _service(db, tmp_path: Path, agent: FakeFeedbackAgent | None = None) -> FeedbackService:
    embedding = FakeEmbeddingService()
    return FeedbackService(
        db,
        agent=agent or FakeFeedbackAgent(),
        embedding_service=embedding,
        memory_service=MemoryService(db, embedding=embedding),
        task_service=TaskMemoryService(db, tmp_path / "tasks"),
    )


def test_analysis_only_persists_pending_candidates_and_auditable_evidence(db, tmp_path: Path) -> None:
    rows = _seed_feedback(db)
    owner = rows["owner"]
    output = rows["output"]
    metric = rows["metric"]
    assets = rows["assets"]
    assert isinstance(owner, Blogger) and isinstance(output, Output) and isinstance(metric, Metric)
    service = _service(db, tmp_path)

    run = service.start(owner.id, output.id, metric.id, "feedback-analysis-key")
    same = service.start(owner.id, output.id, metric.id, "feedback-analysis-key")
    analyzed = service.analyze(owner.id, run.id)

    assert same.id == run.id and analyzed.status == "analyzed"
    assert owner.suit_type == "贵州综合" and owner.knowledge_focus is None
    assert all(asset.effect is None for asset in assets)
    candidates = service.get_candidates(owner.id, run.id)
    assert candidates and {item["status"] for item in candidates} == {"pending"}
    assert service.get_evidence(owner.id, run.id)
    assert db.scalar(
        select(func.count()).select_from(MemoryRecord).where(MemoryRecord.status == "active")
    ) == 0
    assert db.scalar(
        select(func.count()).select_from(MemoryRecord).where(MemoryRecord.status == "candidate")
    ) >= 1


def test_selective_confirm_is_atomic_versioned_and_idempotent(db, tmp_path: Path) -> None:
    rows = _seed_feedback(db)
    owner = rows["owner"]
    output = rows["output"]
    metric = rows["metric"]
    assert isinstance(owner, Blogger) and isinstance(output, Output) and isinstance(metric, Metric)
    service = _service(db, tmp_path)
    run = service.start(owner.id, output.id, metric.id, "feedback-confirm-key")
    service.analyze(owner.id, run.id)
    selected = [
        item["id"]
        for item in service.get_candidates(owner.id, run.id)
        if item["candidate_type"] in {"profile", "asset_effect"}
    ]

    applied = service.confirm(owner.id, run.id, candidate_ids=selected)
    repeated = service.confirm(owner.id, run.id, candidate_ids=selected)

    assert applied.status == "applied" and repeated.id == applied.id
    assert owner.suit_type == "酸汤美食" and owner.knowledge_focus == "酸汤美食"
    assert any(asset.effect == "effective" for asset in rows["assets"])
    assert db.scalar(
        select(func.count()).select_from(DecisionLog).where(
            DecisionLog.blogger_id == owner.id,
            DecisionLog.decision_type == "feedback_confirm",
        )
    ) == 1
    assert db.scalar(
        select(func.count()).select_from(MemoryRecord).where(
            MemoryRecord.blogger_id == owner.id,
            MemoryRecord.status == "active",
        )
    ) >= 2


def test_reject_and_snapshot_conflict_never_change_business_data(db, tmp_path: Path) -> None:
    rows = _seed_feedback(db)
    owner = rows["owner"]
    output = rows["output"]
    metric = rows["metric"]
    assert isinstance(owner, Blogger) and isinstance(output, Output) and isinstance(metric, Metric)
    service = _service(db, tmp_path)
    run = service.start(owner.id, output.id, metric.id, "feedback-reject-key")
    service.analyze(owner.id, run.id)
    rejected = service.reject(owner.id, run.id)
    assert rejected.status == "rejected" and owner.suit_type == "贵州综合"
    with pytest.raises(FeedbackServiceError) as cannot_apply:
        service.confirm(owner.id, run.id)
    assert cannot_apply.value.code == "FEEDBACK_CONFIRM_INVALID_STATE"

    second = service.start(owner.id, output.id, metric.id, "feedback-conflict-key")
    service.analyze(owner.id, second.id)
    metric.views += 1
    db.commit()
    with pytest.raises(FeedbackServiceError) as conflict:
        service.confirm(owner.id, second.id)
    assert conflict.value.code == "FEEDBACK_SNAPSHOT_CHANGED"
    assert owner.suit_type == "贵州综合"


def test_cross_blogger_and_restart_recovery_are_safe(db, tmp_path: Path) -> None:
    rows = _seed_feedback(db)
    owner = rows["owner"]
    other = rows["other"]
    output = rows["output"]
    metric = rows["metric"]
    assert all(isinstance(item, Blogger) for item in (owner, other))
    assert isinstance(output, Output) and isinstance(metric, Metric)
    service = _service(db, tmp_path)
    run = service.start(owner.id, output.id, metric.id, "feedback-recovery-key")
    run.status = "running"
    db.commit()

    recovered = service.recover_unfinished()
    assert [item.id for item in recovered] == [run.id]
    assert run.status == "failed" and run.error_code == "FEEDBACK_INTERRUPTED"
    with pytest.raises(FeedbackServiceError) as hidden:
        service.get(other.id, run.id)
    assert hidden.value.status_code == 404
    retried = service.retry(owner.id, run.id)
    assert retried.status == "analyzed"
    assert db.scalar(select(func.count()).select_from(FeedbackRun)) == 1
    assert db.scalar(select(func.count()).select_from(FeedbackEvidence)) > 0


def test_confirm_failure_rolls_back_all_business_changes_and_stays_retryable(
    db, tmp_path: Path, monkeypatch
) -> None:
    rows = _seed_feedback(db)
    owner = rows["owner"]
    output = rows["output"]
    metric = rows["metric"]
    assert isinstance(owner, Blogger) and isinstance(output, Output) and isinstance(metric, Metric)
    service = _service(db, tmp_path)
    run = service.start(owner.id, output.id, metric.id, "feedback-rollback-key")
    service.analyze(owner.id, run.id)
    selected = [
        item["id"]
        for item in service.get_candidates(owner.id, run.id)
        if item["candidate_type"] == "profile"
    ]

    def fail_memory(*_args, **_kwargs):
        raise RuntimeError("向量写入失败")

    monkeypatch.setattr(service.memory_service, "sync_profile", fail_memory)
    with pytest.raises(FeedbackServiceError) as failed:
        service.confirm(owner.id, run.id, candidate_ids=selected)

    assert failed.value.code == "FEEDBACK_APPLY_FAILED"
    db.refresh(owner)
    db.refresh(run)
    assert owner.suit_type == "贵州综合" and owner.knowledge_focus is None
    assert run.status == "analyzed"
    assert {
        item["status"] for item in service.get_candidates(owner.id, run.id)
    } == {"pending"}
    assert db.scalar(
        select(func.count()).select_from(DecisionLog).where(
            DecisionLog.decision_type == "feedback_confirm"
        )
    ) == 0

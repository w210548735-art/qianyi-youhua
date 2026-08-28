from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from sqlalchemy import func, select

from app.models import (
    Asset,
    AssetEmbedding,
    Blogger,
    FeedbackRun,
    LibraryEvolutionRevision,
    Metric,
    Output,
    Schedule,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.feedback_service import FeedbackService
from app.services.memory_service import MemoryService


class StableAnalysis:
    def build_snapshot(self, *_args, **_kwargs):
        return {"snapshot_hash": "stable"}


def test_confirmed_three_library_evolution_is_idempotent_and_preserves_manual_content(db) -> None:
    blogger = Blogger(
        name="三库反馈博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
        profile_state="complete",
    )
    db.add(blogger)
    db.flush()
    locked = Asset(
        blogger_id=blogger.id,
        lib_type="knowledge",
        category="美食",
        title="人工知识",
        content="人工正文不可覆盖",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        dedupe_key=hashlib.sha256(b"manual-locked").hexdigest(),
        manual_locked=True,
    )
    db.add(locked)
    db.flush()
    output = Output(
        blogger_id=blogger.id,
        type="script",
        category="美食",
        title="反馈脚本",
        content_json="{}",
        status="succeeded",
        version=1,
    )
    db.add(output)
    db.flush()
    schedule = Schedule(
        blogger_id=blogger.id,
        output_id=output.id,
        plan_date=date.today(),
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
        source_type="manual",
        views=10,
        likes=1,
        comments=0,
        collects=0,
        shares=0,
        user_confirmed=True,
        idempotency_key="library-metric",
        collected_at=datetime.utcnow(),
    )
    db.add(metric)
    db.flush()
    run = FeedbackRun(
        blogger_id=blogger.id,
        output_id=output.id,
        primary_metric_id=metric.id,
        status="analyzed",
        idempotency_key="library-evolution",
        snapshot_json="{}",
        snapshot_hash="stable",
        analysis_json="{}",
        summary="用户确认三库进化",
        prompt_version="fake",
        model_name="fake",
    )
    db.add(run)
    db.flush()
    revisions = [
        LibraryEvolutionRevision(
            run_id=run.id,
            lib_type="knowledge",
            action="reinforce",
            target_asset_id=locked.id,
            candidate_json="{}",
            reason="强化已确认人工知识，但不改正文",
            status="pending",
            version=1,
        )
    ]
    for lib_type in ("material", "algorithm"):
        revisions.append(
            LibraryEvolutionRevision(
                run_id=run.id,
                lib_type=lib_type,
                action="add",
                candidate_json=json.dumps(
                    {
                        "title": f"{lib_type}反馈资产",
                        "content": "用户确认后创建",
                        "category": "反馈进化",
                    },
                    ensure_ascii=False,
                ),
                reason="补齐三库候选",
                status="pending",
                version=1,
            )
        )
    db.add_all(revisions)
    db.commit()
    embedding = FakeEmbeddingService()
    service = FeedbackService(
        db,
        analysis_service=StableAnalysis(),
        embedding_service=embedding,
        memory_service=MemoryService(db, embedding=embedding),
    )
    selected = [f"library_evolution:{item.id}" for item in revisions]

    first = service.confirm(blogger.id, run.id, candidate_ids=selected)
    second = service.confirm(blogger.id, run.id, candidate_ids=selected)

    assert first.status == "applied" and second.id == first.id
    assert locked.content == "人工正文不可覆盖" and locked.effect is None
    feedback_assets = list(
        db.scalars(
            select(Asset).where(Asset.blogger_id == blogger.id, Asset.origin == "feedback")
        )
    )
    assert {item.lib_type for item in feedback_assets} == {"material", "algorithm"}
    assert all(item.source_type == "user_confirmed" for item in feedback_assets)
    assert db.scalar(select(func.count()).select_from(AssetEmbedding)) == 2
    assert db.scalar(
        select(func.count()).select_from(Asset).where(Asset.origin == "feedback")
    ) == 2

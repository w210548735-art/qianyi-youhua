from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models import (
    Asset,
    AssetSource,
    Blogger,
    DecisionLog,
    MemoryEmbedding,
    MemoryRecord,
    SourceDocument,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.memory_service import (
    MemoryConfirmationRequiredError,
    MemoryEmbeddingError,
    MemoryService,
)


def make_blogger(db, name: str) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        routes="黔东南",
        viral_topic="酸汤鱼探店",
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def make_verified_asset(db, blogger: Blogger, title: str = "凯里酸汤鱼") -> Asset:
    source = SourceDocument(
        title="贵州文旅官方资料",
        url=f"https://example.com/source/{blogger.id}/{title}",
        publisher="贵州省文化和旅游厅",
        source_type="official",
        verified_at="2026-08-28",
        content_excerpt=f"{title}的官方介绍。",
        content_hash=f"hash-{blogger.id}-{title}",
    )
    db.add(source)
    db.flush()
    asset = Asset(
        blogger_id=blogger.id,
        lib_type="knowledge",
        category="美食",
        title=title,
        content=f"{title}是贵州代表性地方风味。",
        tags_json=json.dumps(["贵州", title], ensure_ascii=False),
        source_type="official",
        credibility=5,
        origin="seed",
        dedupe_key=f"dedupe-{blogger.id}-{title}",
    )
    db.add(asset)
    db.flush()
    db.add(AssetSource(asset_id=asset.id, source_document_id=source.id))
    db.commit()
    db.refresh(asset)
    return asset


class FailingEmbeddingService(FakeEmbeddingService):
    def encode_documents(self, texts: list[str]):
        raise RuntimeError("模拟向量模型失败")


def test_memory_fuses_confirmed_profile_verified_asset_source_and_decision(db):
    blogger = make_blogger(db, "阿黔")
    asset = make_verified_asset(db, blogger)
    decision = DecisionLog(
        blogger_id=blogger.id,
        decision_type="formal",
        prompt_version="test-v1",
        input_summary="用户确认路线策略",
        decision=json.dumps({"route": "黔东南美食线"}, ensure_ascii=False),
        reason="用户确认后采用该路线",
    )
    db.add(decision)
    db.commit()

    service = MemoryService(db, embedding=FakeEmbeddingService())
    profile = service.sync_profile(blogger.id)
    memories = service.sync_verified_assets(blogger.id)
    decisions = service.sync_decisions(blogger.id, user_confirmed=True)

    assert profile.memory_type == "profile_fact"
    assert profile.status == "active"
    assert profile.embedding is not None
    assert any(item.memory_type == "verified_knowledge" for item in memories)
    assert any(item.source_type == "source_document" for item in memories)
    assert decisions[0].memory_type == "decision_summary"
    assert decisions[0].status == "active"
    assert db.scalar(select(func.count()).select_from(MemoryEmbedding)) == db.scalar(
        select(func.count()).select_from(MemoryRecord)
    )
    assert asset.id in {
        int(item.source_id)
        for item in db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.blogger_id == blogger.id,
                MemoryRecord.source_type == "asset",
            )
        )
    }


def test_candidate_requires_user_confirmation_and_promotion(db):
    blogger = make_blogger(db, "候选博主")
    service = MemoryService(db, embedding=FakeEmbeddingService())
    candidate = service.create_memory(
        blogger.id,
        "user_preference",
        "临时偏好",
        "模型从单次对话猜测用户喜欢夜游。",
        "agent",
        "turn-1",
        confidence=0.4,
        status="active",
        user_confirmed=False,
    )
    assert candidate.status == "candidate"
    with pytest.raises(MemoryConfirmationRequiredError):
        service.promote_memory(candidate.id)
    assert db.get(MemoryRecord, candidate.id).status == "candidate"

    promoted = service.promote_memory(candidate.id, user_confirmed=True)
    assert promoted.status == "active"
    assert promoted.embedding is not None


def test_update_creates_new_version_and_supersedes_old_only_after_confirmation(db):
    blogger = make_blogger(db, "版本博主")
    service = MemoryService(db, embedding=FakeEmbeddingService())
    old = service.sync_profile(blogger.id)

    blogger.style = "vlog"
    db.commit()
    candidate = service.update_memory(old.id, content=service._profile_content(blogger))
    assert candidate.status == "candidate"
    assert candidate.version == 2
    assert db.get(MemoryRecord, old.id).status == "active"

    active = service.update_memory(
        old.id,
        content=service._profile_content(blogger),
        user_confirmed=True,
    )
    assert active.status == "active"
    assert active.version == 3
    assert db.get(MemoryRecord, old.id).status == "superseded"
    assert db.get(MemoryRecord, candidate.id).status == "candidate"
    assert active.parent_memory_id == old.id
    assert active.embedding is not None


def test_semantic_search_isolated_by_blogger(db):
    first = make_blogger(db, "第一位博主")
    second = make_blogger(db, "第二位博主")
    service = MemoryService(db, embedding=FakeEmbeddingService())
    service.create_memory(
        first.id,
        "verified_knowledge",
        "第一位的知识",
        "第一位博主的贵州美食内容。",
        "user_confirmed",
        "first-source",
        confidence=1.0,
        status="active",
        user_confirmed=True,
    )
    service.create_memory(
        second.id,
        "verified_knowledge",
        "第二位的知识",
        "第二位博主的贵州美食内容。",
        "user_confirmed",
        "second-source",
        confidence=1.0,
        status="active",
        user_confirmed=True,
    )

    results = service.semantic_search(first.id, "贵州美食", limit=20)
    assert results
    assert all(item["blogger_id"] == first.id for item in results)
    assert all(item.blogger_id == first.id for item in results)
    assert not any(item["blogger_id"] == second.id for item in results)


def test_vector_failure_rolls_back_memory_and_embedding(db):
    blogger = make_blogger(db, "失败博主")
    service = MemoryService(db, embedding=FailingEmbeddingService())
    with pytest.raises(MemoryEmbeddingError):
        service.sync_profile(blogger.id)

    assert db.scalar(select(func.count()).select_from(MemoryRecord).where(MemoryRecord.blogger_id == blogger.id)) == 0
    assert db.scalar(select(func.count()).select_from(MemoryEmbedding)) == 0

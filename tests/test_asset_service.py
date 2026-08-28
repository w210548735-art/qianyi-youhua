from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models import (
    Asset,
    AssetEmbedding,
    AssetSource,
    Blogger,
    DecisionLog,
    MemoryRecord,
    SourceDocument,
)
from app.services.asset_service import (
    AssetConflictError,
    AssetNotFoundError,
    AssetService,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.memory_service import MemoryService


class FailingEmbeddingService(FakeEmbeddingService):
    def encode_documents(self, texts: list[str]):
        raise RuntimeError("模拟资产向量失败")


def _blogger(db, name: str = "资产服务博主") -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def _trusted_payload() -> dict:
    return {
        "lib_type": "knowledge",
        "category": "美食",
        "title": "手工可信酸汤知识",
        "content": "来自可信来源的贵州酸汤知识。",
        "tags": ["贵州", "酸汤"],
        "source_type": "official",
        "source_url": "https://example.com/manual-sour-soup",
        "source_title": "官方酸汤资料",
        "publisher": "测试文旅部门",
        "verified_at": "2026-08-28",
        "credibility": 5,
        "idempotency_key": "manual-trusted-asset-1",
    }


def test_manual_trusted_asset_is_atomic_idempotent_and_syncs_active_memory(db):
    blogger = _blogger(db)
    embedding = FakeEmbeddingService()
    service = AssetService(
        db,
        embedding=embedding,
        memory_service=MemoryService(db, embedding=embedding),
    )

    first = service.create_manual(blogger.id, _trusted_payload())
    repeated = service.create_manual(blogger.id, _trusted_payload())

    assert first.id == repeated.id
    assert first.origin == "manual" and first.manual_locked is True
    assert db.scalar(select(func.count()).select_from(Asset)) == 1
    assert db.scalar(select(func.count()).select_from(AssetEmbedding)) == 1
    assert db.scalar(select(func.count()).select_from(SourceDocument)) == 1
    assert db.scalar(select(func.count()).select_from(AssetSource)) == 1
    assert db.scalar(select(func.count()).select_from(DecisionLog)) == 1
    active = list(
        db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.blogger_id == blogger.id,
                MemoryRecord.source_type == "asset",
                MemoryRecord.source_id == str(first.id),
                MemoryRecord.status == "active",
            )
        )
    )
    assert len(active) == 1 and active[0].embedding is not None


def test_untrusted_knowledge_only_creates_candidate_memory(db):
    blogger = _blogger(db)
    embedding = FakeEmbeddingService()
    service = AssetService(
        db,
        embedding=embedding,
        memory_service=MemoryService(db, embedding=embedding),
    )
    payload = _trusted_payload()
    payload.update(
        {
            "title": "用户手工经验",
            "source_type": "manual",
            "source_url": None,
            "credibility": 2,
            "idempotency_key": "manual-untrusted-asset-1",
        }
    )

    asset = service.create_manual(blogger.id, payload)
    memories = list(
        db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.blogger_id == blogger.id,
                MemoryRecord.source_type == "asset",
                MemoryRecord.source_id == str(asset.id),
            )
        )
    )
    assert len(memories) == 1
    assert memories[0].status == "candidate"


def test_embedding_failure_rolls_back_asset_source_embedding_and_decision(db):
    blogger = _blogger(db)
    service = AssetService(db, embedding=FailingEmbeddingService())

    with pytest.raises(RuntimeError, match="模拟资产向量失败"):
        service.create_manual(blogger.id, _trusted_payload())

    for model in (Asset, AssetEmbedding, AssetSource, SourceDocument, DecisionLog):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_asset_service_crud_soft_delete_and_blogger_isolation(db):
    first = _blogger(db, "第一位")
    second = _blogger(db, "第二位")
    embedding = FakeEmbeddingService()
    service = AssetService(db, embedding=embedding)
    asset = service.create_manual(first.id, _trusted_payload())

    assert service.get(first.id, asset.id).id == asset.id
    with pytest.raises(AssetNotFoundError):
        service.get(second.id, asset.id)
    updated = service.update_manual(
        first.id,
        asset.id,
        {"title": "人工编辑后的标题", "tags": ["人工", "锁定"]},
    )
    assert updated.title == "人工编辑后的标题"
    assert json.loads(updated.tags_json) == ["人工", "锁定"]
    assert updated.manual_locked is True

    deleted = service.soft_delete(first.id, asset.id)
    repeated = service.soft_delete(first.id, asset.id)
    assert deleted.deleted_at is not None
    assert repeated.deleted_at == deleted.deleted_at
    with pytest.raises(AssetNotFoundError):
        service.get(first.id, asset.id)
    with pytest.raises(AssetConflictError):
        service.create_manual(first.id, _trusted_payload())

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import func, select

from app.core.config import settings
from app.models import Asset, AssetEmbedding, AssetSource, Blogger, DecisionLog
from app.services import build_service as build_service_module
from app.services.build_service import LibraryBuildService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.search_service import AssetSearchService


def create_blogger(db) -> Blogger:
    blogger = Blogger(
        name="测试博主",
        platform="抖音",
        content_types_json=json.dumps(["美食", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        frequency="周更",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def test_build_inserts_three_libraries_sources_and_embeddings(db):
    blogger = create_blogger(db)
    embedding = FakeEmbeddingService()
    service = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = service.start_build(blogger.id, "test-build-0001")
    result = service.execute_build(run.id)

    assert result.status == "succeeded"
    assert db.scalar(select(func.count()).select_from(Asset)) == 28
    assert db.scalar(select(func.count()).select_from(AssetEmbedding)) == 28
    assert db.scalar(select(func.count()).select_from(DecisionLog)) == 1
    assert db.scalar(select(func.count()).select_from(Asset).where(Asset.lib_type == "knowledge")) == 20
    assert db.scalar(select(func.count()).select_from(Asset).where(Asset.lib_type == "material")) == 5
    assert db.scalar(select(func.count()).select_from(Asset).where(Asset.lib_type == "algorithm")) == 3


def test_idempotent_build_does_not_duplicate_assets(db):
    blogger = create_blogger(db)
    service = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    run = service.start_build(blogger.id, "test-build-0002")
    service.execute_build(run.id)
    same_run = service.start_build(blogger.id, "test-build-0002")
    service.execute_build(same_run.id)

    assert run.id == same_run.id
    assert db.scalar(select(func.count()).select_from(Asset)) == 28


def test_hybrid_search_returns_semantic_results(db):
    blogger = create_blogger(db)
    embedding = FakeEmbeddingService()
    service = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = service.start_build(blogger.id, "test-build-0003")
    service.execute_build(run.id)

    results = AssetSearchService(db, embedding).search(blogger.id, query="酸汤鱼")

    assert results
    assert any(item["title"] == "凯里酸汤鱼" for item in results)
    acid_fish = next(item for item in results if item["title"] == "凯里酸汤鱼")
    assert acid_fish["sources"]
    assert acid_fish["sources"][0]["url"].startswith("https://")
    combined = AssetSearchService(db, embedding).search(
        blogger.id,
        query="酸汤鱼",
        lib_type="knowledge",
        category="美食",
    )
    assert combined
    assert all(item["lib_type"] == "knowledge" and item["category"] == "美食" for item in combined)


def test_all_knowledge_assets_have_sources_and_complete_metadata(db):
    blogger = create_blogger(db)
    service = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    run = service.start_build(blogger.id, "test-build-0004")
    service.execute_build(run.id)

    knowledge = list(db.scalars(select(Asset).where(Asset.lib_type == "knowledge")))
    assert len(knowledge) >= 15
    for asset in knowledge:
        assert asset.category and asset.title and asset.content and asset.tags_json
        assert asset.source_type and asset.credibility >= 4 and asset.decision_id
        assert db.scalar(select(func.count()).select_from(AssetSource).where(AssetSource.asset_id == asset.id)) == 1


def test_insufficient_trusted_seeds_fail_without_assets(db, tmp_path, monkeypatch):
    seed_file = tmp_path / "insufficient.json"
    seed_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        build_service_module,
        "settings",
        replace(settings, seed_file=seed_file),
    )
    blogger = create_blogger(db)
    service = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    run = service.start_build(blogger.id, "test-build-0005")
    result = service.execute_build(run.id)

    assert result.status == "failed"
    assert result.error_message == "TRUSTED_SEED_INSUFFICIENT"
    assert db.scalar(select(func.count()).select_from(Asset)) == 0


def test_locked_and_soft_deleted_assets_are_not_overwritten_or_revived(db):
    blogger = create_blogger(db)
    service = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    first = service.start_build(blogger.id, "test-build-0006")
    service.execute_build(first.id)
    asset = db.scalar(select(Asset).order_by(Asset.id))
    asset.title = "人工锁定标题"
    asset.manual_locked = True
    deleted = db.scalar(select(Asset).where(Asset.id != asset.id).order_by(Asset.id))
    from datetime import datetime

    deleted.deleted_at = datetime.utcnow()
    deleted.manual_locked = True
    db.commit()

    second = service.start_build(blogger.id, "test-build-0007")
    result = service.execute_build(second.id)
    db.refresh(asset)
    db.refresh(deleted)

    assert result.status == "succeeded"
    assert asset.title == "人工锁定标题"
    assert deleted.deleted_at is not None


def test_stable_pagination_and_default_soft_delete_filter(db):
    blogger = create_blogger(db)
    embedding = FakeEmbeddingService()
    service = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = service.start_build(blogger.id, "test-build-0008")
    service.execute_build(run.id)
    deleted = db.scalar(select(Asset).order_by(Asset.id))
    from datetime import datetime

    deleted.deleted_at = datetime.utcnow()
    db.commit()

    search = AssetSearchService(db, embedding)
    first_page = search.search(blogger.id, limit=10, offset=0)
    second_page = search.search(blogger.id, limit=10, offset=10)
    repeated = search.search(blogger.id, limit=10, offset=0)

    assert [item["id"] for item in first_page] == [item["id"] for item in repeated]
    assert not {item["id"] for item in first_page}.intersection(item["id"] for item in second_page)
    assert deleted.id not in {item["id"] for item in first_page + second_page}

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app.models import Asset, AssetEmbedding, AssetSource, Blogger, SourceDocument
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisError, LibraryAnalysisService


def _blogger(db, name: str, directions: list[str] | None = None) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(directions or ["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        profile_state="complete",
    )
    db.add(blogger)
    db.flush()
    return blogger


def _asset(
    db,
    blogger: Blogger,
    embedding: FakeEmbeddingService,
    *,
    lib_type: str,
    category: str,
    title: str,
    credibility: int,
    source: bool = True,
    with_vector: bool = True,
) -> Asset:
    asset = Asset(
        blogger_id=blogger.id,
        lib_type=lib_type,
        category=category,
        title=title,
        content=f"{title}的贵州文旅事实。",
        tags_json=json.dumps(["贵州", category], ensure_ascii=False),
        source_type="official" if source else "manual",
        credibility=credibility,
        origin="seed" if source else "manual",
        manual_locked=not source,
        dedupe_key=hashlib.sha256(f"{blogger.id}-{title}".encode()).hexdigest(),
    )
    db.add(asset)
    db.flush()
    if source:
        document = SourceDocument(
            title=f"{title}官方资料",
            url=f"https://example.com/{blogger.id}/{asset.id}",
            publisher="测试官方",
            source_type="official",
            verified_at="2026-08-28",
            content_excerpt=asset.content,
            content_hash=hashlib.sha256(asset.content.encode()).hexdigest(),
        )
        db.add(document)
        db.flush()
        db.add(AssetSource(asset_id=asset.id, source_document_id=document.id))
    if with_vector:
        result = embedding.encode_documents([asset.content])[0]
        db.add(
            AssetEmbedding(
                asset_id=asset.id,
                model_name=embedding.model_name,
                model_version="test",
                dimension=len(result.vector),
                vector=embedding.to_bytes(result.vector),
                vector_norm=1.0,
                content_hash=result.content_hash,
            )
        )
    db.commit()
    db.refresh(asset)
    return asset


def test_snapshot_analyzes_three_libraries_and_cross_library_relations(db):
    blogger = _blogger(db, "分析博主", ["美食"])
    embedding = FakeEmbeddingService()
    _asset(db, blogger, embedding, lib_type="knowledge", category="美食", title="酸汤鱼", credibility=5)
    _asset(db, blogger, embedding, lib_type="material", category="口播", title="酸汤鱼镜头", credibility=3)
    _asset(db, blogger, embedding, lib_type="algorithm", category="选题", title="美食选题算法", credibility=4)

    snapshot = LibraryAnalysisService(db, embedding).build_snapshot(blogger.id)

    assert snapshot["counts"] == {"knowledge": 1, "material": 1, "algorithm": 1, "total": 3}
    assert all("embedding" not in item for item in snapshot["assets"])
    assert all(item["embedding_hash"] for item in snapshot["assets"])
    assert snapshot["libraries"]["knowledge"]["category_distribution"] == {"美食": 1}
    assert snapshot["source_coverage"]["with_source"] == 3
    assert snapshot["relations"]
    assert snapshot["cross_library_relations"]["covered_pairs"]
    assert snapshot["core_assets"]
    assert snapshot["future_data"] == {"output": "暂无数据", "effect": "暂无数据"}
    assert snapshot["snapshot_hash"] == LibraryAnalysisService.calculate_snapshot_hash(snapshot)


def test_snapshot_marks_gaps_low_credibility_no_source_orphan_and_profile_directions(db):
    blogger = _blogger(db, "缺口博主", ["美食"])
    embedding = FakeEmbeddingService()
    _asset(
        db, blogger, embedding, lib_type="knowledge", category="美食", title="低可信美食", credibility=2, source=False
    )
    _asset(
        db,
        blogger,
        embedding,
        lib_type="material",
        category="口播",
        title="无向量素材",
        credibility=3,
        source=False,
        with_vector=False,
    )

    snapshot = LibraryAnalysisService(db, embedding).build_snapshot(blogger.id)

    reasons = {row["reason"] for row in snapshot["weak_assets"]}
    assert {"low_credibility", "no_source", "orphan"}.issubset(reasons)
    assert "景区" in snapshot["profile_direction_coverage"]["missing"]
    assert "非遗" in snapshot["profile_direction_coverage"]["missing"]
    assert any("算法库" in item for item in snapshot["missing_items"])
    assert any(item["category"] == "景区" for item in snapshot["weak_categories"])


def test_snapshot_excludes_deleted_assets_and_other_bloggers(db):
    blogger = _blogger(db, "当前博主")
    other = _blogger(db, "其他博主")
    embedding = FakeEmbeddingService()
    active = _asset(db, blogger, embedding, lib_type="knowledge", category="美食", title="保留", credibility=5)
    deleted = _asset(db, blogger, embedding, lib_type="knowledge", category="美食", title="删除", credibility=5)
    deleted.deleted_at = datetime.utcnow()
    _asset(db, other, embedding, lib_type="knowledge", category="美食", title="他人", credibility=5)
    db.commit()

    snapshot = LibraryAnalysisService(db, embedding).build_snapshot(blogger.id)

    assert [item["id"] for item in snapshot["assets"]] == [active.id]
    assert all(item["blogger_id"] == blogger.id for item in snapshot["assets"])
    blogger.deleted_at = datetime.utcnow()
    db.commit()
    try:
        LibraryAnalysisService(db, embedding).build_snapshot(blogger.id)
    except LibraryAnalysisError as exc:
        assert str(exc) == "BLOGGER_NOT_FOUND"
    else:
        raise AssertionError("已删除博主不应生成快照")


def test_snapshot_hash_and_analyze_are_reproducible_without_database_reads(db):
    blogger = _blogger(db, "可重放博主", ["美食"])
    embedding = FakeEmbeddingService()
    _asset(db, blogger, embedding, lib_type="knowledge", category="美食", title="重放事实", credibility=5)
    snapshot = LibraryAnalysisService(db, embedding).build_snapshot(blogger.id)
    service = LibraryAnalysisService(db, embedding)

    replayed = service.analyze(snapshot)

    assert replayed["snapshot_hash"] == snapshot["snapshot_hash"]
    assert replayed["counts"] == snapshot["counts"]
    assert replayed["future_data"]["effect"] == "暂无数据"
    assert service.calculate_snapshot_hash(snapshot) == service.calculate_snapshot_hash(replayed)

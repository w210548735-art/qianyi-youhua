from __future__ import annotations

import hashlib
import json
from time import perf_counter

from app.models import Asset, AssetEmbedding, Blogger
from app.services.embedding_service import FakeEmbeddingService
from app.services.search_service import AssetSearchService


def _blogger(db) -> Blogger:
    blogger = Blogger(
        name="性能测试博主",
        platform="抖音",
        content_types_json='["贵州文旅"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
    )
    db.add(blogger)
    db.flush()
    return blogger


def test_search_1000_assets_under_500ms(db):
    blogger = _blogger(db)
    embedding = FakeEmbeddingService()
    for index in range(1000):
        title = f"贵州文旅测试资产 {index:04d}"
        content = f"用于性能验收的酸汤鱼和非遗内容 {index}"
        asset = Asset(
            blogger_id=blogger.id,
            lib_type="knowledge",
            category="性能测试",
            title=title,
            content=content,
            tags_json=json.dumps(["贵州", "酸汤鱼"], ensure_ascii=False),
            source_type="official",
            credibility=5,
            dedupe_key=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        )
        db.add(asset)
        db.flush()
        result = embedding.encode_documents([content])[0]
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

    started = perf_counter()
    results = AssetSearchService(db, embedding).search(
        blogger.id,
        query="酸汤鱼",
        lib_type="knowledge",
        category="性能测试",
        limit=50,
        tags=["贵州", "酸汤鱼"],
        source_type="official",
        min_credibility=5,
        max_credibility=5,
    )
    elapsed = perf_counter() - started

    print(f"PERF_SEARCH_1000_SECONDS={elapsed:.6f}")
    assert len(results) == 50
    assert elapsed < 0.5, f"1000 条资产检索耗时 {elapsed:.3f}s"


def test_regular_crud_under_300ms(db):
    blogger = _blogger(db)
    started = perf_counter()
    asset = Asset(
        blogger_id=blogger.id,
        lib_type="material",
        category="CRUD",
        title="普通增改查删",
        content="性能验收",
        tags_json="[]",
        source_type="generated_template",
        credibility=1,
        dedupe_key="crud-performance-key",
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    asset.content = "已更新"
    db.commit()
    db.refresh(asset)
    asset.deleted_at = asset.updated_at
    db.commit()
    elapsed = perf_counter() - started

    print(f"PERF_CRUD_SECONDS={elapsed:.6f}")
    assert asset.content == "已更新"
    assert elapsed < 0.3, f"普通 CRUD 耗时 {elapsed:.3f}s"

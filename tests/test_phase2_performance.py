from __future__ import annotations

import hashlib
import json
from time import perf_counter

import pytest
from sqlalchemy import select

from app.models import Assessment, Asset, AssetEmbedding, Blogger
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService

pytestmark = pytest.mark.performance


def make_blogger(db) -> Blogger:
    blogger = Blogger(
        name="二期性能博主",
        platform="抖音",
        content_types_json='["景区","美食","非遗"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.flush()
    return blogger


def test_deterministic_analysis_of_1000_assets_under_one_second(db):
    blogger = make_blogger(db)
    embedding = FakeEmbeddingService()
    lib_types = ("knowledge", "material", "algorithm")
    categories = ("景区", "美食", "非遗")
    for index in range(1000):
        title = f"二期分析资产-{index:04d}"
        content = f"贵州{categories[index % 3]}内容与跨库语义关系 {index}"
        asset = Asset(
            blogger_id=blogger.id,
            lib_type=lib_types[index % 3],
            category=categories[index % 3],
            title=title,
            content=content,
            tags_json=json.dumps(["贵州", categories[index % 3]], ensure_ascii=False),
            source_type="official" if index % 3 == 0 else "generated_template",
            credibility=5 if index % 3 == 0 else 2,
            origin="seed" if index % 3 == 0 else "agent",
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

    service = LibraryAnalysisService(db, embedding)
    started = perf_counter()
    snapshot = service.build_snapshot(blogger.id)
    analysis = service.analyze(snapshot)
    elapsed = perf_counter() - started

    print(f"PERF_PHASE2_ANALYSIS_1000_SECONDS={elapsed:.6f}")
    assert analysis["counts"]["total"] == 1000
    assert analysis["cross_library_relations"]
    assert elapsed < 1.0, f"1000条资产确定性预分析耗时 {elapsed:.3f}s"


def test_assessment_history_query_and_crud_under_300ms(db):
    blogger = make_blogger(db)
    started = perf_counter()
    assessment = Assessment(
        blogger_id=blogger.id,
        status="pending",
        idempotency_key="phase2-performance-crud",
        input_snapshot_json="{}",
        prompt_version="phase2-assessment-v1",
        model_name="fake-assessment-agent",
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    assessment.status = "failed"
    assessment.error_code = "AGENT_TIMEOUT"
    assessment.error_message = "超时"
    db.commit()
    loaded = db.scalar(
        select(Assessment).where(
            Assessment.blogger_id == blogger.id,
            Assessment.id == assessment.id,
        )
    )
    elapsed = perf_counter() - started

    print(f"PERF_PHASE2_CRUD_SECONDS={elapsed:.6f}")
    assert loaded is not None and loaded.status == "failed"
    assert elapsed < 0.3, f"体检普通查询/CRUD耗时 {elapsed:.3f}s"

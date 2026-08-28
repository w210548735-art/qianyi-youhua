from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Asset, AssetPlace, Blogger, DecisionLog, OutputAsset, OutputPlace, Place, TaskSession
from app.services.assessment_agent import FakeAssessmentAgent
from app.services.assessment_service import AssessmentService
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.build_service import LibraryBuildService
from app.services.context_service import ContextService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.output_agent import FakeOutputAgent
from app.services.output_service import OutputService
from app.services.output_validation_service import OutputValidationService
from app.services.route_service import RouteService, RouteServiceError
from app.services.task_memory_service import TaskMemoryService


def setup_context(db, tmp_path: Path):
    blogger = Blogger(
        name="路线博主",
        platform="抖音",
        content_types_json=json.dumps(["美食", "景区"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["探店"], ensure_ascii=False),
        routes="贵阳 美食",
        viral_topic="贵州美食",
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    embedding = FakeEmbeddingService()
    build = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = build.start_build(blogger.id, "route-build")
    assert build.execute_build(run.id).status == "succeeded"
    memory = MemoryService(db, embedding)
    assessment_service = AssessmentService(
        db,
        agent=FakeAssessmentAgent(),
        analysis_service=LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=TaskMemoryService(db, tmp_path / "assessment"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )
    pending = assessment_service.start_assessment(blogger.id, "route-assessment")
    assessment = assessment_service.execute_assessment(pending.id, blogger.id)
    output = OutputService(
        db,
        agent=FakeOutputAgent(),
        validation_service=OutputValidationService(),
        task_service=TaskMemoryService(db, tmp_path / "route"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
        analysis_service=LibraryAnalysisService(db, embedding),
    )
    service = RouteService(
        db,
        agent=FakeOutputAgent(),
        output_service=output,
        task_service=output.task_service,
        context_service=output.context_service,
        memory_service=memory,
    )
    return blogger, assessment, service


def add_place(db, blogger_id: int, name: str, *, cost, benefit, like, koc=True, shoot=True) -> Place:
    row = Place(
        blogger_id=blogger_id,
        name=name,
        category="美食",
        location="贵阳",
        specialty="贵州风味",
        tags_json='["美食", "贵阳"]',
        source_type="manual",
        credibility=5,
        like_level=like,
        est_cost=cost,
        est_benefit=benefit,
        fits_koc=koc,
        fits_shoot=shoot,
        origin="manual",
        manual_locked=True,
        dedupe_key=f"place-{blogger_id}-{name}",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_route_sorting_evidence_decision_and_idempotency(db, tmp_path: Path):
    blogger, assessment, service = setup_context(db, tmp_path)
    low = add_place(db, blogger.id, "低收益店", cost=100, benefit=180, like=2)
    high = add_place(db, blogger.id, "高收益喜爱店", cost=100, benefit=400, like=5)
    knowledge = db.scalar(
        select(Asset).where(Asset.blogger_id == blogger.id, Asset.lib_type == "knowledge")
    )
    assert knowledge is not None
    db.add_all(
        [
            AssetPlace(asset_id=knowledge.id, place_id=low.id, relation_type="地点知识", source_type="manual"),
            AssetPlace(asset_id=knowledge.id, place_id=high.id, relation_type="地点知识", source_type="manual"),
        ]
    )
    db.commit()

    output = service.recommend(
        blogger.id,
        assessment.id,
        "route-idempotency-key",
        place_ids=[low.id, high.id],
    )
    same = service.recommend(
        blogger.id,
        assessment.id,
        "route-idempotency-key",
        place_ids=[low.id, high.id],
    )
    payload = json.loads(output.content_json)

    assert same.id == output.id
    assert [row["place_id"] for row in payload["stops"]] == [high.id, low.id]
    assert payload["formula"]["weights"] == RouteService.weights
    assert [row.place_id for row in output.places] == [high.id, low.id]
    assert db.scalar(select(func.count()).select_from(OutputPlace).where(OutputPlace.output_id == output.id)) == 2
    assert db.scalar(select(func.count()).select_from(OutputAsset).where(OutputAsset.output_id == output.id)) == 1
    decision = db.get(DecisionLog, output.decision_id)
    assert decision is not None and decision.decision_type == "route_recommendation"
    assert "net_benefit" in decision.decision
    assert db.get(TaskSession, output.task_id).status == "completed"


def test_null_or_untrusted_commercial_fields_return_precise_missing_details(db, tmp_path: Path):
    blogger, assessment, service = setup_context(db, tmp_path)
    incomplete = add_place(db, blogger.id, "未知收益店", cost=None, benefit=None, like=None, koc=None, shoot=None)

    with pytest.raises(RouteServiceError) as caught:
        service.recommend(blogger.id, assessment.id, "route-null-commercial", place_ids=[incomplete.id])

    assert caught.value.code == "ROUTE_COMMERCIAL_DATA_INCOMPLETE"
    assert caught.value.details == [
        {
            "place_id": incomplete.id,
            "name": incomplete.name,
            "missing_fields": ["est_benefit", "est_cost", "fits_koc", "fits_shoot", "like_level"],
        }
    ]

    incomplete.est_cost = 10
    incomplete.est_benefit = 20
    incomplete.like_level = 3
    incomplete.fits_koc = True
    incomplete.fits_shoot = True
    incomplete.origin = "seed"
    incomplete.source_type = "unknown"
    incomplete.credibility = 1
    db.commit()
    with pytest.raises(RouteServiceError) as untrusted:
        service.recommend(blogger.id, assessment.id, "route-untrusted-commercial", place_ids=[incomplete.id])
    assert untrusted.value.details[0]["missing_fields"] == ["commercial_source"]


def test_route_cross_blogger_place_is_hidden(db, tmp_path: Path):
    owner, assessment, service = setup_context(db, tmp_path)
    other = Blogger(
        name="其他博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万以下",
        monetization_types_json="[]",
        frequency="周更",
        profile_state="complete",
    )
    db.add(other)
    db.commit()
    db.refresh(other)
    foreign = add_place(db, other.id, "其他地点", cost=10, benefit=20, like=5)

    with pytest.raises(RouteServiceError) as caught:
        service.recommend(owner.id, assessment.id, "route-cross-blogger", place_ids=[foreign.id])
    assert caught.value.status_code == 404


def test_rank_1000_places_under_one_second_without_database_calls(db):
    blogger = Blogger(
        name="性能博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万以下",
        monetization_types_json="[]",
        routes="贵阳",
        frequency="日更",
        profile_state="complete",
    )
    places = [
        Place(
            id=index + 1,
            blogger_id=1,
            name=f"地点{index}",
            category="美食",
            location="贵阳",
            tags_json="[]",
            source_type="manual",
            credibility=5,
            like_level=index % 6,
            est_cost=100.0,
            est_benefit=101.0 + index,
            fits_koc=True,
            fits_shoot=True,
            origin="manual",
            manual_locked=True,
            dedupe_key=str(index),
        )
        for index in range(1000)
    ]
    started = time.perf_counter()
    ranked = RouteService.rank_places(places, blogger)
    elapsed = time.perf_counter() - started
    print(f"PHASE3_ROUTE_SORT_1000_SECONDS={elapsed:.6f}")
    assert len(ranked) == 1000
    assert ranked[0]["score"] >= ranked[-1]["score"]
    assert elapsed < 1.0

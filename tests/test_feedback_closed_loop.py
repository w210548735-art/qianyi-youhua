from __future__ import annotations

import json
from datetime import date, datetime

from app.models import (
    Blogger,
    FeedbackRun,
    Metric,
    Output,
    Place,
    PlaceCommercialRevision,
    ProfileFeedbackRevision,
    Schedule,
)
from app.services.embedding_service import FakeEmbeddingService
from app.services.feedback_service import FeedbackService
from app.services.memory_service import MemoryService
from app.services.output_agent import FakeOutputAgent
from app.services.route_service import RouteService


class StableAnalysis:
    def build_snapshot(self, *_args, **_kwargs):
        return {"snapshot_hash": "closed-loop"}


def _script_snapshot(blogger: Blogger) -> dict:
    return {
        "blogger_id": blogger.id,
        "snapshot_hash": f"profile-{blogger.suit_type}-{blogger.knowledge_focus}",
        "profile": {
            "id": blogger.id,
            "platform": blogger.platform,
            "style": blogger.style,
            "content_types": ["美食"],
            "suit_type": blogger.suit_type,
            "knowledge_focus": blogger.knowledge_focus,
        },
        "assets": [
            {
                "id": 900,
                "blogger_id": blogger.id,
                "lib_type": "knowledge",
                "title": "可信酸汤知识",
                "content": "用户确认事实",
                "credibility": 5,
                "source_document_ids": [901],
            }
        ],
        "places": [],
    }


def test_confirmed_feedback_changes_fake_script_direction_and_route_order(db) -> None:
    blogger = Blogger(
        name="闭环博主",
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["探店"]',
        profile_state="complete",
        suit_type="贵州综合",
    )
    db.add(blogger)
    db.flush()
    output = Output(
        blogger_id=blogger.id,
        type="script",
        category="酸汤美食",
        title="闭环反馈脚本",
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
        views=100,
        likes=20,
        comments=2,
        collects=1,
        shares=1,
        user_confirmed=True,
        idempotency_key="closed-loop-metric",
        collected_at=datetime.utcnow(),
    )
    place_a = Place(
        blogger_id=blogger.id,
        name="甲店",
        category="酸汤美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key="closed-loop-place-a",
        est_cost=50,
        est_benefit=100,
        like_level=5,
        fits_koc=True,
        fits_shoot=True,
    )
    place_b = Place(
        blogger_id=blogger.id,
        name="乙店",
        category="酸汤美食",
        tags_json="[]",
        source_type="manual",
        credibility=5,
        origin="manual",
        manual_locked=True,
        dedupe_key="closed-loop-place-b",
        est_cost=50,
        est_benefit=200,
        like_level=5,
        fits_koc=True,
        fits_shoot=True,
    )
    db.add_all([metric, place_a, place_b])
    db.flush()
    run = FeedbackRun(
        blogger_id=blogger.id,
        output_id=output.id,
        primary_metric_id=metric.id,
        status="analyzed",
        idempotency_key="closed-loop-feedback",
        snapshot_json="{}",
        snapshot_hash="closed-loop",
        analysis_json="{}",
        summary="确认酸汤方向与甲店收益",
        prompt_version="fake",
        model_name="fake",
    )
    db.add(run)
    db.flush()
    profile_revision = ProfileFeedbackRevision(
        run_id=run.id,
        blogger_id=blogger.id,
        field_name="knowledge_focus",
        before=None,
        after="酸汤美食",
        reason="用户确认主攻方向",
        status="pending",
        version=1,
    )
    before = {
        "est_cost": 50,
        "est_benefit": 100,
        "like_level": 5,
        "fits_koc": True,
        "fits_shoot": True,
    }
    place_revision = PlaceCommercialRevision(
        run_id=run.id,
        place_id=place_a.id,
        before_json=json.dumps(before),
        after_json=json.dumps({"est_benefit": 400, "simulation_only": False}),
        reason="用户确认甲店新收益",
        status="pending",
        version=1,
    )
    db.add_all([profile_revision, place_revision])
    db.commit()

    output_agent = FakeOutputAgent()
    before_script = output_agent.generate_script([], _script_snapshot(blogger))
    before_route = RouteService.rank_places([place_a, place_b], blogger)
    assert before_script["category"] == "贵州综合"
    assert before_route[0]["place_id"] == place_b.id

    embedding = FakeEmbeddingService()
    service = FeedbackService(
        db,
        analysis_service=StableAnalysis(),
        embedding_service=embedding,
        memory_service=MemoryService(db, embedding=embedding),
    )
    service.confirm(
        blogger.id,
        run.id,
        candidate_ids=[f"profile:{profile_revision.id}", f"place_commercial:{place_revision.id}"],
    )
    after_script = output_agent.generate_script([], _script_snapshot(blogger))
    after_route = RouteService.rank_places([place_a, place_b], blogger)

    assert after_script["category"] == "酸汤美食"
    assert after_route[0]["place_id"] == place_a.id
    assert place_a.est_benefit == 400

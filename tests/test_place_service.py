from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Blogger, DecisionLog, Place
from app.services.place_service import (
    PlaceNotFoundError,
    PlaceService,
    PlaceValidationError,
)


def make_blogger(db, name: str = "地点测试博主") -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["贵州文旅"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def trusted_seed(title: str = "黄果树瀑布群") -> dict:
    return {
        "title": title,
        "category": "景区",
        "content": "官方来源记录的贵州自然景观事实。",
        "tags": ["安顺", "贵州", "自然景观"],
        "source_title": "贵州官方文旅资料",
        "source_url": "https://example.com/guizhou/huangguoshu",
        "publisher": "贵州省人民政府",
        "source_type": "official",
        "verified_at": "2026-08-28",
        "credibility": 5,
    }


def test_manual_crud_null_commercial_fields_and_audit(db):
    blogger = make_blogger(db)
    service = PlaceService(db)
    place = service.create(
        blogger.id,
        name="青岩古镇",
        category="景区",
        tags=["贵阳", "古镇"],
        source="manual",
        credibility=3,
    )
    assert place.manual_locked is True
    assert place.origin == "manual"
    assert place.est_cost is None
    assert place.est_benefit is None
    assert place.like_level is None
    assert place.fits_koc is None
    assert place.fits_shoot is None
    assert place.decision_id is not None

    updated = service.update(
        blogger.id,
        place.id,
        specialty="用户明确补充的拍摄特色",
        est_cost=120.0,
        fits_shoot=True,
    )
    assert updated.specialty == "用户明确补充的拍摄特色"
    assert updated.est_cost == 120.0
    assert updated.est_benefit is None
    assert updated.fits_shoot is True
    assert updated.manual_locked is True

    deleted = service.delete(blogger.id, place.id)
    assert deleted.deleted_at is not None
    assert service.delete(blogger.id, place.id).deleted_at == deleted.deleted_at
    assert service.list(blogger.id) == []
    assert service.get(blogger.id, place.id) is None
    decisions = list(
        db.scalars(
            select(DecisionLog).where(DecisionLog.blogger_id == blogger.id).order_by(DecisionLog.id)
        )
    )
    assert [item.decision_type for item in decisions] == [
        "place_manual_create",
        "place_manual_update",
        "place_manual_delete",
    ]


def test_manual_create_is_idempotent_and_does_not_revive_deleted(db):
    blogger = make_blogger(db)
    service = PlaceService(db)
    first = service.create(
        blogger.id,
        name="西江千户苗寨",
        category="景区",
        location="黔东南",
        source="manual",
        credibility=2,
    )
    same = service.create(
        blogger.id,
        name="西江千户苗寨",
        category="景区",
        location="黔东南",
        source="manual",
        credibility=5,
        est_cost=999,
    )
    assert same.id == first.id
    assert same.credibility == 2
    assert same.est_cost is None
    service.delete(blogger.id, first.id)
    still_deleted = service.create(
        blogger.id,
        name="西江千户苗寨",
        category="景区",
        location="黔东南",
        source="manual",
        credibility=5,
    )
    assert still_deleted.id == first.id
    assert still_deleted.deleted_at is not None
    assert service.count(blogger.id) == 0


def test_trusted_seed_sync_is_idempotent_and_preserves_manual_edit_and_delete(db):
    blogger = make_blogger(db)
    service = PlaceService(db)
    seed = trusted_seed()
    first = service.sync_trusted_seeds(blogger.id, [seed])
    assert len(first) == 1
    place = first[0]
    assert place.origin == "seed"
    assert place.manual_locked is False
    assert place.source_type == "official"
    assert place.source_url == seed["source_url"]
    assert place.credibility == 5
    assert place.est_cost is None
    assert place.est_benefit is None
    assert place.like_level is None
    assert place.fits_koc is None
    assert place.fits_shoot is None
    assert place.decision_id is not None

    assert service.sync_trusted_seeds(blogger.id, [seed]) == []
    service.update(blogger.id, place.id, specialty="人工编辑后的事实说明")
    assert service.sync_trusted_seeds(blogger.id, [seed]) == []
    current = db.get(Place, place.id)
    assert current is not None
    assert current.specialty == "人工编辑后的事实说明"
    service.delete(blogger.id, place.id)
    assert service.sync_trusted_seeds(blogger.id, [seed]) == []
    current = db.get(Place, place.id)
    assert current is not None and current.deleted_at is not None


def test_trusted_seed_rejects_unverified_source_and_invalid_credibility(db):
    blogger = make_blogger(db)
    service = PlaceService(db)
    invalid = trusted_seed()
    invalid["source_type"] = "generated_template"
    with pytest.raises(PlaceValidationError, match="TRUSTED_SEED_SOURCE_INVALID"):
        service.sync_trusted_seeds(blogger.id, [invalid])
    invalid = trusted_seed()
    invalid["credibility"] = 3
    with pytest.raises(PlaceValidationError, match="TRUSTED_SEED_INVALID"):
        service.sync_trusted_seeds(blogger.id, [invalid])


def test_filters_are_independent_and_blogger_scoped(db):
    first = make_blogger(db, "第一位地点博主")
    second = make_blogger(db, "第二位地点博主")
    service = PlaceService(db)
    service.create(
        first.id,
        name="甲秀楼",
        category="景区",
        location="贵阳",
        specialty="夜景拍摄",
        tags=["贵阳", "夜景"],
        source="official",
        credibility=5,
    )
    service.create(
        first.id,
        name="酸汤鱼体验",
        category="美食",
        location="凯里",
        specialty="酸汤风味",
        tags=["凯里", "酸汤"],
        source="manual",
        credibility=3,
    )
    other = service.create(
        second.id,
        name="甲秀楼",
        category="景区",
        location="贵阳",
        tags=["贵阳"],
        source="official",
        credibility=5,
    )
    assert [item.name for item in service.list(first.id, tags=["夜景"])] == ["甲秀楼"]
    assert service.list(first.id, tags=["不存在"]) == []
    assert [item.name for item in service.list(first.id, source="official")] == ["甲秀楼"]
    assert [item.name for item in service.list(first.id, min_credibility=4)] == ["甲秀楼"]
    assert [item.name for item in service.list(first.id, max_credibility=3)] == ["酸汤鱼体验"]
    assert [item.name for item in service.list(first.id, q="夜景")] == ["甲秀楼"]
    assert service.get(first.id, other.id) is None
    with pytest.raises(PlaceNotFoundError, match="BLOGGER_NOT_FOUND"):
        service.list(999999)

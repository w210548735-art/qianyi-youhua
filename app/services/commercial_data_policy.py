"""地点商业字段的统一可信来源策略。

地点原始来源保持不变；反馈确认通过已应用的 ``PlaceCommercialRevision``
提供字段级来源，避免把整条低可信地点静默升级为可信。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import Place, PlaceCommercialRevision

COMMERCIAL_FIELDS = ("est_cost", "est_benefit", "like_level", "fits_koc", "fits_shoot")
TRUSTED_PLACE_SOURCE_TYPES = frozenset({"official", "government", "unesco", "ihchina", "trusted"})


def _decode(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def place_commercial_provenance(
    place: Place,
    db: Session | None = None,
) -> dict[str, dict[str, Any]]:
    """返回当前值可用的字段级来源；无来源字段不会出现在结果中。"""

    return place_commercial_provenance_map([place], db).get(place.id, {})


def _base_provenance(place: Place) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    manual_source = place.origin == "manual" and place.source_type == "manual"
    trusted_source = place.source_type in TRUSTED_PLACE_SOURCE_TYPES and place.credibility >= 3
    if manual_source or trusted_source:
        source_kind = "manual" if manual_source else "trusted_source"
        for field in COMMERCIAL_FIELDS:
            if getattr(place, field) is not None:
                provenance[field] = {
                    "source_kind": source_kind,
                    "source_type": place.source_type,
                    "credibility": place.credibility,
                    "manual_locked": place.manual_locked,
                }
    return provenance


def place_commercial_provenance_map(
    places: list[Place],
    db: Session | None = None,
) -> dict[int, dict[str, dict[str, Any]]]:
    """批量返回字段来源，Revision 始终只执行一次查询。"""

    result = {place.id: _base_provenance(place) for place in places if place.id is not None}
    if not places:
        return result

    session = db or object_session(places[0])
    place_ids = list(result)
    if session is None or not place_ids:
        return result
    revisions = list(
        session.scalars(
            select(PlaceCommercialRevision)
            .where(
                PlaceCommercialRevision.place_id.in_(place_ids),
                PlaceCommercialRevision.status == "applied",
            )
            .order_by(PlaceCommercialRevision.applied_at, PlaceCommercialRevision.id)
        )
    )
    by_id = {place.id: place for place in places if place.id is not None}
    for revision in revisions:
        place = by_id.get(revision.place_id)
        if place is None:
            continue
        after = _decode(revision.after_json)
        confirmed_fields = after.get("_confirmed_fields", [])
        if not isinstance(confirmed_fields, list):
            continue
        for field in confirmed_fields:
            if field not in COMMERCIAL_FIELDS or field not in after:
                continue
            if after[field] != getattr(place, field):
                continue
            result[place.id][field] = {
                "source_kind": "user_confirmed_revision",
                "revision_id": revision.id,
                "feedback_run_id": revision.run_id,
                "confirmed_at": revision.confirmed_at,
                "applied_at": revision.applied_at,
                "reason": revision.reason,
            }
    return result


def trusted_estimate_places(db: Session, places: list[Place]) -> list[Place]:
    """仅返回成本和收益都有可追溯来源的地点。"""

    provenance = place_commercial_provenance_map(places, db)
    return [
        place
        for place in places
        if place.est_cost is not None
        and place.est_benefit is not None
        and {"est_cost", "est_benefit"} <= set(provenance.get(place.id, {}))
    ]


__all__ = [
    "COMMERCIAL_FIELDS",
    "TRUSTED_PLACE_SOURCE_TYPES",
    "place_commercial_provenance",
    "place_commercial_provenance_map",
    "trusted_estimate_places",
]

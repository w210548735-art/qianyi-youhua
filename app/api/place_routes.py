from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.place_service import (
    PlaceNotFoundError,
    PlaceService,
    PlaceValidationError,
    place_to_dict,
)


class PlaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    location: str | None = None
    specialty: str | None = None
    tags: list[str] = Field(default_factory=list)
    # ``source`` 是对外稳定字段；source_type 兼容内部旧模型和已有客户端。
    source: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    credibility: int = Field(default=0, ge=0, le=5)
    like_level: int | None = Field(default=None, ge=0, le=5)
    est_cost: float | None = Field(default=None, ge=0)
    est_benefit: float | None = Field(default=None, ge=0)
    fits_koc: bool | None = None
    fits_shoot: bool | None = None


class PlaceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = None
    specialty: str | None = None
    tags: list[str] | None = None
    source: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    credibility: int | None = Field(default=None, ge=0, le=5)
    like_level: int | None = Field(default=None, ge=0, le=5)
    est_cost: float | None = Field(default=None, ge=0)
    est_benefit: float | None = Field(default=None, ge=0)
    fits_koc: bool | None = None
    fits_shoot: bool | None = None


router = APIRouter(prefix="/api/v1")


def _service(db: Session = Depends(get_db)) -> PlaceService:
    return PlaceService(db)


def _raise_api_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if message in {"BLOGGER_NOT_FOUND", "PLACE_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message == "PLACE_DUPLICATE":
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/bloggers/{blogger_id}/places")
def create_place(
    blogger_id: int,
    body: PlaceCreateRequest,
    service: PlaceService = Depends(_service),
) -> dict[str, Any]:
    try:
        place = service.create(blogger_id, **body.model_dump())
    except (PlaceNotFoundError, PlaceValidationError) as exc:
        raise _raise_api_error(exc) from exc
    return place_to_dict(place)


@router.post("/bloggers/{blogger_id}/places/sync")
def sync_places(
    blogger_id: int,
    service: PlaceService = Depends(_service),
) -> dict[str, Any]:
    try:
        places = service.sync_trusted_seeds(blogger_id)
    except (PlaceNotFoundError, PlaceValidationError) as exc:
        raise _raise_api_error(exc) from exc
    return {"inserted": len(places), "places": [place_to_dict(place) for place in places]}


@router.get("/bloggers/{blogger_id}/places")
def list_places(
    blogger_id: int,
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=100),
    tags: list[str] | None = Query(default=None),
    tag: str | None = Query(default=None, max_length=100),
    source: str | None = Query(default=None, max_length=100),
    source_type: str | None = Query(default=None, max_length=100),
    min_credibility: int | None = Query(default=None, ge=0, le=5),
    max_credibility: int | None = Query(default=None, ge=0, le=5),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    service: PlaceService = Depends(_service),
) -> list[dict[str, Any]]:
    selected_tags = list(tags or [])
    if tag:
        selected_tags.append(tag)
    try:
        places = service.list(
            blogger_id,
            q=q,
            category=category,
            tags=selected_tags,
            source=source,
            source_type=source_type,
            min_credibility=min_credibility,
            max_credibility=max_credibility,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    except (PlaceNotFoundError, PlaceValidationError) as exc:
        raise _raise_api_error(exc) from exc
    return [place_to_dict(place) for place in places]


@router.get("/bloggers/{blogger_id}/places/{place_id}")
def get_place(
    blogger_id: int,
    place_id: int,
    service: PlaceService = Depends(_service),
) -> dict[str, Any]:
    try:
        place = service.get(blogger_id, place_id)
    except PlaceNotFoundError as exc:
        raise _raise_api_error(exc) from exc
    if place is None:
        raise HTTPException(status_code=404, detail="PLACE_NOT_FOUND")
    return place_to_dict(place)


@router.put("/bloggers/{blogger_id}/places/{place_id}")
def update_place(
    blogger_id: int,
    place_id: int,
    body: PlaceUpdateRequest,
    service: PlaceService = Depends(_service),
) -> dict[str, Any]:
    try:
        place = service.update(blogger_id, place_id, **body.model_dump(exclude_unset=True))
    except (PlaceNotFoundError, PlaceValidationError) as exc:
        raise _raise_api_error(exc) from exc
    return place_to_dict(place)


@router.delete("/bloggers/{blogger_id}/places/{place_id}")
def delete_place(
    blogger_id: int,
    place_id: int,
    service: PlaceService = Depends(_service),
) -> dict[str, Any]:
    try:
        place = service.delete(blogger_id, place_id)
    except PlaceNotFoundError as exc:
        raise _raise_api_error(exc) from exc
    return {
        "id": place.id,
        "blogger_id": place.blogger_id,
        "status": "deleted",
        "deleted_at": place.deleted_at.isoformat() if place.deleted_at else None,
    }


__all__ = ["router"]

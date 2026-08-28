from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable, List

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Blogger, DecisionLog, Place


class PlaceNotFoundError(ValueError):
    """地点或其所属的有效博主不存在。"""


class PlaceValidationError(ValueError):
    """地点输入不满足第一阶段约束。"""


class PlaceService:
    """地点库的业务服务。

    所有公开方法都显式接收 ``blogger_id``，并在查询中重复约束博主范围，
    从而保证地点不会因为客户端传入其他地点 ID 而跨博主泄漏。
    """

    VALID_CATEGORIES = {"景区", "美食", "非遗", "民俗", "地点", "其他"}
    TRUSTED_SOURCE_TYPES = {"official", "government", "unesco", "ihchina", "trusted"}

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        blogger_id: int,
        *,
        name: str,
        category: str,
        location: str | None = None,
        specialty: str | None = None,
        tags: Iterable[str] | None = None,
        source: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        credibility: int = 0,
        like_level: int | None = None,
        est_cost: float | None = None,
        est_benefit: float | None = None,
        fits_koc: bool | None = None,
        fits_shoot: bool | None = None,
        manual_locked: bool = True,
        origin: str = "manual",
        decision_type: str = "place_manual_create",
        reason: str = "用户手工新增地点",
        commit: bool = True,
    ) -> Place:
        """新增手工地点并记录决策。

        相同博主和去重键的记录按幂等处理：已有记录直接返回，绝不复活已软删除
        记录，也不覆盖人工编辑或锁定内容。
        """
        self._get_active_blogger(blogger_id)
        normalized = self._normalise_input(
            name=name,
            category=category,
            location=location,
            specialty=specialty,
            tags=tags,
            source=source,
            source_type=source_type,
            source_url=source_url,
            credibility=credibility,
            like_level=like_level,
            est_cost=est_cost,
            est_benefit=est_benefit,
            fits_koc=fits_koc,
            fits_shoot=fits_shoot,
            trusted=origin == "seed",
        )
        dedupe_key = self.make_dedupe_key(
            normalized["name"], normalized["category"], normalized["location"]
        )
        existing = self.db.scalar(
            select(Place).where(Place.blogger_id == blogger_id, Place.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing

        try:
            place = Place(
                blogger_id=blogger_id,
                name=normalized["name"],
                category=normalized["category"],
                location=normalized["location"],
                specialty=normalized["specialty"],
                tags_json=json.dumps(normalized["tags"], ensure_ascii=False),
                source_type=normalized["source_type"],
                source_url=normalized["source_url"],
                credibility=normalized["credibility"],
                like_level=normalized["like_level"],
                est_cost=normalized["est_cost"],
                est_benefit=normalized["est_benefit"],
                fits_koc=normalized["fits_koc"],
                fits_shoot=normalized["fits_shoot"],
                origin=origin,
                manual_locked=manual_locked,
                dedupe_key=dedupe_key,
            )
            self.db.add(place)
            self.db.flush()
            decision = self._record_decision(
                blogger_id=blogger_id,
                decision_type=decision_type,
                input_summary=self._input_summary(normalized),
                decision={"place_id": place.id, "origin": origin},
                reason=reason,
            )
            place.decision_id = decision.id
            if commit:
                self.db.commit()
                self.db.refresh(place)
            return place
        except Exception:
            self.db.rollback()
            raise

    def create_manual(self, blogger_id: int, data: dict[str, Any]) -> Place:
        """与资产服务一致的手工新增入口，供统一路由和调用方使用。"""
        return self.create(blogger_id, **data)

    def get(self, blogger_id: int, place_id: int, *, include_deleted: bool = False) -> Place | None:
        self._get_active_blogger(blogger_id)
        statement = select(Place).where(Place.id == place_id, Place.blogger_id == blogger_id)
        if not include_deleted:
            statement = statement.where(Place.deleted_at.is_(None))
        return self.db.scalar(statement)

    def get_place(self, blogger_id: int, place_id: int, *, include_deleted: bool = False) -> Place | None:
        """语义更明确的详情别名。"""
        return self.get(blogger_id, place_id, include_deleted=include_deleted)

    def list(
        self,
        blogger_id: int,
        *,
        q: str | None = None,
        category: str | None = None,
        tags: Iterable[str] | None = None,
        source: str | None = None,
        source_type: str | None = None,
        min_credibility: int | None = None,
        max_credibility: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Place]:
        self._get_active_blogger(blogger_id)
        if limit < 1 or limit > 100:
            raise PlaceValidationError("limit must be between 1 and 100")
        if offset < 0:
            raise PlaceValidationError("offset must be non-negative")
        if min_credibility is not None and not 0 <= min_credibility <= 5:
            raise PlaceValidationError("min_credibility must be between 0 and 5")
        if max_credibility is not None and not 0 <= max_credibility <= 5:
            raise PlaceValidationError("max_credibility must be between 0 and 5")
        if (
            min_credibility is not None
            and max_credibility is not None
            and min_credibility > max_credibility
        ):
            raise PlaceValidationError("min_credibility cannot exceed max_credibility")

        statement = select(Place).where(
            Place.blogger_id == blogger_id,
            Place.deleted_at.is_(None),
        )
        if category:
            statement = statement.where(Place.category == category.strip())
        source_filter = (source_type or "").strip()
        if source_filter:
            statement = statement.where(Place.source_type == source_filter)
        elif source and source.strip():
            source_filter = source.strip()
            # source 既可表示来源类型，也可直接表示来源 URL。
            statement = statement.where(
                or_(Place.source_type == source_filter, Place.source_url == source_filter)
            )
        if min_credibility is not None:
            statement = statement.where(Place.credibility >= min_credibility)
        if max_credibility is not None:
            statement = statement.where(Place.credibility <= max_credibility)
        if q and q.strip():
            escaped = self._escape_like(q.strip())
            pattern = f"%{escaped}%"
            statement = statement.where(
                or_(
                    Place.name.like(pattern, escape="\\"),
                    Place.location.like(pattern, escape="\\"),
                    Place.specialty.like(pattern, escape="\\"),
                )
            )
        for tag in self._normalise_tags(tags):
            escaped_tag = self._escape_like(tag)
            # tags_json 是 JSON 数组；带引号的匹配避免把“贵阳”误匹配为“贵阳周边”。
            statement = statement.where(
                Place.tags_json.like(f'%"{escaped_tag}"%', escape="\\")
            )
        statement = statement.order_by(Place.created_at.desc(), Place.id.desc())
        return list(self.db.scalars(statement.offset(offset).limit(limit)))

    def list_places(self, blogger_id: int, **filters: Any) -> List[Place]:
        """语义更明确的列表别名，保留 ``list`` 供简洁调用。"""
        return self.list(blogger_id, **filters)

    def update(self, blogger_id: int, place_id: int, *, commit: bool = True, **changes: Any) -> Place:
        place = self.get(blogger_id, place_id)
        if place is None:
            raise PlaceNotFoundError("PLACE_NOT_FOUND")
        allowed = {
            "name",
            "category",
            "location",
            "specialty",
            "tags",
            "source",
            "source_type",
            "source_url",
            "credibility",
            "like_level",
            "est_cost",
            "est_benefit",
            "fits_koc",
            "fits_shoot",
        }
        unknown = set(changes).difference(allowed)
        if unknown:
            raise PlaceValidationError(f"unsupported fields: {', '.join(sorted(unknown))}")
        values = self._current_values(place)
        for key, value in changes.items():
            if key == "source":
                if value is not None:
                    values["source_type"] = value
            elif key == "tags":
                values[key] = [] if value is None else value
            else:
                # Nullable 地点字段允许显式清空；必填字段会在统一校验中拒绝 None。
                values[key] = value
        normalized = self._normalise_input(**values, trusted=False)
        old_dedupe = place.dedupe_key
        new_dedupe = self.make_dedupe_key(
            normalized["name"], normalized["category"], normalized["location"]
        )
        conflict = self.db.scalar(
            select(Place).where(
                Place.blogger_id == blogger_id,
                Place.dedupe_key == new_dedupe,
                Place.id != place.id,
            )
        )
        if conflict is not None:
            raise PlaceValidationError("PLACE_DUPLICATE")
        try:
            place.name = normalized["name"]
            place.category = normalized["category"]
            place.location = normalized["location"]
            place.specialty = normalized["specialty"]
            place.tags_json = json.dumps(normalized["tags"], ensure_ascii=False)
            place.source_type = normalized["source_type"]
            place.source_url = normalized["source_url"]
            place.credibility = normalized["credibility"]
            place.like_level = normalized["like_level"]
            place.est_cost = normalized["est_cost"]
            place.est_benefit = normalized["est_benefit"]
            place.fits_koc = normalized["fits_koc"]
            place.fits_shoot = normalized["fits_shoot"]
            place.dedupe_key = new_dedupe
            place.manual_locked = True
            place.origin = "manual"
            decision = self._record_decision(
                blogger_id=blogger_id,
                decision_type="place_manual_update",
                input_summary=json.dumps({"place_id": place_id, "changes": changes}, ensure_ascii=False),
                decision={
                    "place_id": place_id,
                    "old_dedupe_key": old_dedupe,
                    "new_dedupe_key": new_dedupe,
                },
                reason="用户手工编辑地点，保留人工锁定并记录审计决策",
            )
            place.decision_id = decision.id
            if commit:
                self.db.commit()
                self.db.refresh(place)
            return place
        except Exception:
            self.db.rollback()
            raise

    def update_manual(self, blogger_id: int, place_id: int, changes: dict[str, Any]) -> Place:
        """与资产服务一致的手工编辑入口。"""
        return self.update(blogger_id, place_id, **changes)

    def delete(self, blogger_id: int, place_id: int, *, commit: bool = True) -> Place:
        self._get_active_blogger(blogger_id)
        place = self.db.scalar(
            select(Place).where(Place.id == place_id, Place.blogger_id == blogger_id)
        )
        if place is None:
            raise PlaceNotFoundError("PLACE_NOT_FOUND")
        try:
            if place.deleted_at is None:
                place.deleted_at = datetime.utcnow()
                decision = self._record_decision(
                    blogger_id=blogger_id,
                    decision_type="place_manual_delete",
                    input_summary=json.dumps({"place_id": place_id}, ensure_ascii=False),
                    decision={"place_id": place_id, "deleted": True},
                    reason="用户手工软删除地点，保留历史记录且不允许建库复活",
                )
                place.decision_id = decision.id
            if commit:
                self.db.commit()
                self.db.refresh(place)
            return place
        except Exception:
            self.db.rollback()
            raise

    def soft_delete(self, blogger_id: int, place_id: int) -> Place:
        """与其他第一阶段服务一致的软删除入口。"""
        return self.delete(blogger_id, place_id)

    def sync_trusted_seeds(
        self,
        blogger_id: int,
        seeds: List[dict[str, Any]] | None = None,
        *,
        commit: bool = True,
    ) -> List[Place]:
        """从已核验种子建立地点，严格不生成店铺与商业估计。"""
        self._get_active_blogger(blogger_id)
        if seeds is None:
            seeds = json.loads(settings.seed_file.read_text(encoding="utf-8"))
        if not isinstance(seeds, list):
            raise PlaceValidationError("TRUSTED_SEED_INVALID")
        inserted: List[Place] = []
        try:
            for index, seed in enumerate(seeds):
                self._validate_seed(seed, index)
                category = str(seed["category"]).strip()
                # 种子标题本身就是来源事实；只保留官方事实中的名称和描述，绝不拼接店名。
                tags = self._normalise_tags(seed.get("tags", []))
                location = tags[0] if tags else None
                dedupe_key = self.make_dedupe_key(str(seed["title"]), category, location)
                existing = self.db.scalar(
                    select(Place).where(
                        Place.blogger_id == blogger_id,
                        or_(
                            Place.dedupe_key == dedupe_key,
                            Place.source_url == str(seed["source_url"]).strip(),
                        ),
                    )
                )
                if existing is not None:
                    # 无论 existing 是否软删除或人工锁定，建库都不得覆盖或复活。
                    continue
                place = self.create(
                    blogger_id,
                    name=str(seed["title"]).strip(),
                    category=category,
                    location=location,
                    specialty=str(seed["content"]).strip(),
                    tags=tags,
                    source=str(seed["source_type"]).strip(),
                    source_type=str(seed["source_type"]).strip(),
                    source_url=str(seed["source_url"]).strip(),
                    credibility=int(seed["credibility"]),
                    like_level=self._optional_int(seed.get("like_level")),
                    est_cost=self._optional_float(seed.get("est_cost")),
                    est_benefit=self._optional_float(seed.get("est_benefit")),
                    fits_koc=self._optional_bool(seed.get("fits_koc")),
                    fits_shoot=self._optional_bool(seed.get("fits_shoot")),
                    manual_locked=False,
                    origin="seed",
                    decision_type="place_seed_sync",
                    reason="来自可信贵州文旅种子；未补造具体店铺或商业估计",
                    commit=False,
                )
                if place.id not in {item.id for item in inserted}:
                    inserted.append(place)
            if commit:
                self.db.commit()
                for place in inserted:
                    self.db.refresh(place)
            return inserted
        except Exception:
            self.db.rollback()
            raise

    def sync_from_seeds(
        self,
        blogger_id: int,
        seeds: List[dict[str, Any]] | None = None,
    ) -> List[Place]:
        """兼容建库编排器的种子同步别名。"""
        return self.sync_trusted_seeds(blogger_id, seeds)

    def sync(self, blogger_id: int, seeds: List[dict[str, Any]] | None = None) -> List[Place]:
        """最短同步别名，便于建库流程注入地点同步步骤。"""
        return self.sync_trusted_seeds(blogger_id, seeds)

    def count(self, blogger_id: int) -> int:
        self._get_active_blogger(blogger_id)
        return int(
            self.db.scalar(
                select(func.count()).select_from(Place).where(
                    Place.blogger_id == blogger_id,
                    Place.deleted_at.is_(None),
                )
            )
            or 0
        )

    def _get_active_blogger(self, blogger_id: int) -> Blogger:
        statement = select(Blogger).where(Blogger.id == blogger_id)
        if hasattr(Blogger, "deleted_at"):
            statement = statement.where(Blogger.deleted_at.is_(None))
        blogger = self.db.scalar(statement)
        if blogger is None:
            raise PlaceNotFoundError("BLOGGER_NOT_FOUND")
        return blogger

    def _record_decision(
        self,
        *,
        blogger_id: int,
        decision_type: str,
        input_summary: str,
        decision: dict[str, Any],
        reason: str,
    ) -> DecisionLog:
        row = DecisionLog(
            blogger_id=blogger_id,
            decision_type=decision_type,
            prompt_version="phase1-place-v1",
            input_summary=input_summary,
            decision=json.dumps(decision, ensure_ascii=False),
            reason=reason,
        )
        self.db.add(row)
        self.db.flush()
        return row

    @classmethod
    def make_dedupe_key(cls, name: str, category: str, location: str | None = None) -> str:
        raw = "|".join(cls._normalise_text(value) for value in (name, category, location or ""))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def _normalise_input(
        cls,
        *,
        name: str,
        category: str,
        location: str | None,
        specialty: str | None,
        tags: Iterable[str] | None,
        source: str | None,
        source_type: str | None,
        source_url: str | None,
        credibility: int,
        like_level: int | None,
        est_cost: float | None,
        est_benefit: float | None,
        fits_koc: bool | None,
        fits_shoot: bool | None,
        trusted: bool,
    ) -> dict[str, Any]:
        name = cls._normalise_text(name)
        category = cls._normalise_text(category)
        if not name or not category:
            raise PlaceValidationError("name and category are required")
        if len(name) > 300 or len(category) > 100:
            raise PlaceValidationError("name or category is too long")
        if cls.VALID_CATEGORIES and category not in cls.VALID_CATEGORIES:
            # 允许业务新增分类，但不允许空分类；过滤仍采用精确匹配。
            if len(category) < 1:
                raise PlaceValidationError("category is required")
        tags_list = cls._normalise_tags(tags)
        source_value = cls._normalise_text(source_type or source or ("official" if trusted else "manual"))
        if not source_value:
            raise PlaceValidationError("source is required")
        if isinstance(credibility, bool) or not isinstance(credibility, int):
            raise PlaceValidationError("credibility must be an integer")
        if not 0 <= credibility <= 5:
            raise PlaceValidationError("credibility must be between 0 and 5")
        if trusted and source_value.lower() not in cls.TRUSTED_SOURCE_TYPES:
            raise PlaceValidationError("TRUSTED_SEED_SOURCE_INVALID")
        if trusted and not source_url:
            raise PlaceValidationError("TRUSTED_SEED_SOURCE_URL_REQUIRED")
        for field, value in {
            "like_level": like_level,
            "est_cost": est_cost,
            "est_benefit": est_benefit,
        }.items():
            if value is not None and float(value) < 0:
                raise PlaceValidationError(f"{field} must be non-negative")
        if like_level is not None and (
            isinstance(like_level, bool)
            or not isinstance(like_level, int)
            or not 0 <= like_level <= 5
        ):
            raise PlaceValidationError("like_level must be between 0 and 5")
        return {
            "name": name,
            "category": category,
            "location": cls._optional_text(location),
            "specialty": cls._optional_text(specialty),
            "tags": tags_list,
            "source_type": source_value,
            "source_url": cls._optional_text(source_url),
            "credibility": credibility,
            "like_level": like_level if like_level is not None else None,
            "est_cost": float(est_cost) if est_cost is not None else None,
            "est_benefit": float(est_benefit) if est_benefit is not None else None,
            "fits_koc": fits_koc,
            "fits_shoot": fits_shoot,
        }

    @classmethod
    def _current_values(cls, place: Place) -> dict[str, Any]:
        return {
            "name": place.name,
            "category": place.category,
            "location": place.location,
            "specialty": place.specialty,
            "tags": json.loads(place.tags_json or "[]"),
            "source": place.source_type,
            "source_type": place.source_type,
            "source_url": place.source_url,
            "credibility": place.credibility,
            "like_level": place.like_level,
            "est_cost": place.est_cost,
            "est_benefit": place.est_benefit,
            "fits_koc": place.fits_koc,
            "fits_shoot": place.fits_shoot,
        }

    @classmethod
    def _validate_seed(cls, seed: dict[str, Any], index: int) -> None:
        required = {
            "category",
            "title",
            "content",
            "tags",
            "source_type",
            "source_url",
            "source_title",
            "publisher",
            "verified_at",
            "credibility",
        }
        if not isinstance(seed, dict) or required.difference(seed):
            raise PlaceValidationError(f"TRUSTED_SEED_INVALID:{index}")
        credibility = seed.get("credibility")
        if isinstance(credibility, bool) or not isinstance(credibility, int) or credibility < 4:
            raise PlaceValidationError(f"TRUSTED_SEED_INVALID:{index}")
        if str(seed.get("source_type", "")).lower() not in cls.TRUSTED_SOURCE_TYPES:
            raise PlaceValidationError(f"TRUSTED_SEED_SOURCE_INVALID:{index}")
        source_url = str(seed.get("source_url", "")).strip()
        if not source_url or not source_url.startswith(("http://", "https://")):
            raise PlaceValidationError(f"TRUSTED_SEED_SOURCE_URL_REQUIRED:{index}")
        if not str(seed.get("title", "")).strip() or not str(seed.get("content", "")).strip():
            raise PlaceValidationError(f"TRUSTED_SEED_INVALID:{index}")

    @staticmethod
    def _normalise_text(value: Any) -> str:
        return "" if value is None else " ".join(str(value).strip().split())

    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        result = cls._normalise_text(value)
        return result or None

    @classmethod
    def _normalise_tags(cls, tags: Iterable[str] | None) -> List[str]:
        if tags is None:
            return []
        if isinstance(tags, str):
            tags = tags.replace("，", ",").replace("、", ",").split(",")
        result: List[str] = []
        for value in tags:
            text = cls._normalise_text(value)
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return None if value is None or value == "" else int(value)

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None or value == "" else float(value)

    @staticmethod
    def _optional_bool(value: Any) -> bool | None:
        return None if value is None or value == "" else bool(value)

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _input_summary(values: dict[str, Any]) -> str:
        return json.dumps(values, ensure_ascii=False, sort_keys=True)


def place_to_dict(place: Place) -> dict[str, Any]:
    """将地点转换为 API/演示页面使用的稳定 JSON 结构。"""
    source_value = getattr(place, "source", None) or getattr(place, "source_type", None)
    return {
        "id": place.id,
        "blogger_id": place.blogger_id,
        "name": place.name,
        "category": place.category,
        "location": place.location,
        "specialty": place.specialty,
        "tags": json.loads(place.tags_json or "[]"),
        "source": source_value,
        "source_type": source_value,
        "source_url": getattr(place, "source_url", None),
        "credibility": place.credibility,
        "like_level": place.like_level,
        "est_cost": place.est_cost,
        "est_benefit": place.est_benefit,
        "fits_koc": place.fits_koc,
        "fits_shoot": place.fits_shoot,
        "decision_id": place.decision_id,
        "origin": place.origin,
        "manual_locked": place.manual_locked,
        "deleted_at": place.deleted_at.isoformat() if place.deleted_at else None,
        "created_at": place.created_at.isoformat() if place.created_at else None,
        "updated_at": place.updated_at.isoformat() if place.updated_at else None,
    }


__all__ = [
    "PlaceNotFoundError",
    "PlaceService",
    "PlaceValidationError",
    "place_to_dict",
]

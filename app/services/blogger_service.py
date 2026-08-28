from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Blogger, DecisionLog
from app.services.memory_service import MemoryService


class BloggerNotFoundError(RuntimeError):
    """博主不存在或已被软删除。"""


class BloggerValidationError(RuntimeError):
    """画像更新内容不符合约束。"""


class BloggerService:
    """已确认博主画像的查询、版本化更新和可审计软删除服务。"""

    EDITABLE_FIELDS = {
        "name",
        "platform",
        "content_types",
        "style",
        "follower_band",
        "monetization_types",
        "routes",
        "viral_topic",
        "frequency",
        "suit_type",
        "knowledge_focus",
    }

    def __init__(
        self,
        db: Session,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.db = db
        self.memory_service = memory_service or MemoryService(db)

    def get_active(self, blogger_id: int) -> Blogger:
        blogger = self.db.scalar(
            select(Blogger).where(
                Blogger.id == blogger_id,
                Blogger.deleted_at.is_(None),
            )
        )
        if blogger is None:
            raise BloggerNotFoundError("BLOGGER_NOT_FOUND")
        return blogger

    def list_active(self) -> list[Blogger]:
        return list(
            self.db.scalars(
                select(Blogger)
                .where(Blogger.deleted_at.is_(None))
                .order_by(Blogger.id.desc())
            )
        )

    def update_confirmed_profile(
        self,
        blogger_id: int,
        changes: dict[str, Any],
    ) -> Blogger:
        blogger = self.get_active(blogger_id)
        if blogger.profile_state != "complete":
            raise BloggerValidationError("BLOGGER_PROFILE_NOT_CONFIRMED")
        clean_changes = {
            key: value
            for key, value in changes.items()
            if key in self.EDITABLE_FIELDS and value is not None
        }
        if not clean_changes:
            raise BloggerValidationError("BLOGGER_UPDATE_EMPTY")

        before = self._snapshot(blogger)
        for field, value in clean_changes.items():
            if field == "content_types":
                if not isinstance(value, list) or not value:
                    raise BloggerValidationError("CONTENT_TYPES_INVALID")
                blogger.content_types_json = json.dumps(value, ensure_ascii=False)
            elif field == "monetization_types":
                if not isinstance(value, list) or not value:
                    raise BloggerValidationError("MONETIZATION_TYPES_INVALID")
                blogger.monetization_types_json = json.dumps(value, ensure_ascii=False)
            else:
                if field in {"name", "platform", "style", "follower_band"} and not str(value).strip():
                    raise BloggerValidationError(f"{field.upper()}_EMPTY")
                setattr(blogger, field, value)

        after = self._snapshot(blogger)
        decision = DecisionLog(
            blogger_id=blogger.id,
            decision_type="profile_update",
            prompt_version="phase1-closure-v1",
            input_summary=json.dumps(before, ensure_ascii=False),
            decision=json.dumps(after, ensure_ascii=False),
            reason="用户明确编辑并确认已完成画像",
        )
        self.db.add(decision)
        self.db.flush()
        try:
            self.memory_service.sync_profile(blogger.id, user_confirmed=True)
            self.db.refresh(blogger)
            return blogger
        except Exception:
            self.db.rollback()
            raise

    def soft_delete(self, blogger_id: int) -> Blogger:
        blogger = self.db.get(Blogger, blogger_id)
        if blogger is None:
            raise BloggerNotFoundError("BLOGGER_NOT_FOUND")
        if blogger.deleted_at is not None:
            return blogger
        blogger.deleted_at = datetime.utcnow()
        self.db.add(
            DecisionLog(
                blogger_id=blogger.id,
                decision_type="profile_delete",
                prompt_version="phase1-closure-v1",
                input_summary=json.dumps(self._snapshot(blogger), ensure_ascii=False),
                decision=json.dumps(
                    {"blogger_id": blogger.id, "deleted_at": blogger.deleted_at.isoformat()},
                    ensure_ascii=False,
                ),
                reason="用户明确执行画像软删除；关联资产、地点、任务、记忆和决策保留用于审计",
            )
        )
        self.db.commit()
        self.db.refresh(blogger)
        return blogger

    @staticmethod
    def _snapshot(blogger: Blogger) -> dict[str, Any]:
        return {
            "id": blogger.id,
            "name": blogger.name,
            "platform": blogger.platform,
            "content_types": json.loads(blogger.content_types_json),
            "style": blogger.style,
            "follower_band": blogger.follower_band,
            "monetization_types": json.loads(blogger.monetization_types_json),
            "routes": blogger.routes,
            "viral_topic": blogger.viral_topic,
            "frequency": blogger.frequency,
            "suit_type": blogger.suit_type,
            "knowledge_focus": getattr(blogger, "knowledge_focus", None),
        }

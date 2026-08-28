from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    AssetEmbedding,
    AssetSource,
    DecisionLog,
    MemoryRecord,
    SourceDocument,
)
from app.services.blogger_service import BloggerService
from app.services.embedding_service import EmbeddingService
from app.services.memory_service import MemoryService

LIB_TYPES = {"knowledge", "material", "algorithm"}
TRUSTED_SOURCE_TYPES = {"official", "verified", "verified_url", "user_confirmed"}


class AssetServiceError(RuntimeError):
    """手工资产服务基础异常。"""


class AssetNotFoundError(AssetServiceError):
    """资产不存在、已删除或不属于当前博主。"""


class AssetValidationError(AssetServiceError):
    """资产输入不符合约束。"""


class AssetConflictError(AssetServiceError):
    """幂等键或稳定去重键指向已删除/冲突资产。"""


class AssetService:
    """按博主隔离的手工资产完整 CRUD 与事务服务。"""

    def __init__(
        self,
        db: Session,
        embedding: EmbeddingService | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.db = db
        self.embedding = embedding or EmbeddingService()
        self.memory_service = memory_service or MemoryService(db, embedding=self.embedding)

    def create_manual(self, blogger_id: int, data: dict[str, Any]) -> Asset:
        BloggerService(self.db).get_active(blogger_id)
        normalized = self._normalize(data, require_all=True)
        dedupe_key = self._dedupe_key(normalized)
        existing = self.db.scalar(
            select(Asset).where(
                Asset.blogger_id == blogger_id,
                Asset.dedupe_key == dedupe_key,
            )
        )
        if existing is not None:
            if existing.deleted_at is not None:
                raise AssetConflictError("ASSET_DELETED_CONFLICT")
            if existing.origin == "manual":
                return existing
            raise AssetConflictError("ASSET_DEDUPE_CONFLICT")

        embedding_result = self.embedding.encode_documents([self._embedding_text(normalized)])[0]
        try:
            asset = Asset(
                blogger_id=blogger_id,
                lib_type=normalized["lib_type"],
                category=normalized["category"],
                title=normalized["title"],
                content=normalized["content"],
                tags_json=json.dumps(normalized["tags"], ensure_ascii=False),
                source_type=normalized["source_type"],
                credibility=normalized["credibility"],
                origin="manual",
                manual_locked=True,
                dedupe_key=dedupe_key,
            )
            self.db.add(asset)
            self.db.flush()
            source = self._upsert_source(normalized)
            if source is not None:
                self.db.flush()
                self.db.add(AssetSource(asset_id=asset.id, source_document_id=source.id))
            decision = self._decision(blogger_id, "asset_create", normalized, asset.id)
            self.db.add(decision)
            self.db.flush()
            asset.decision_id = decision.id
            self.db.add(self._embedding_row(asset.id, embedding_result))
            self.db.commit()
            self.db.refresh(asset)
        except Exception:
            self.db.rollback()
            raise
        self._sync_memory(asset)
        return asset

    def get(self, blogger_id: int, asset_id: int, *, include_deleted: bool = False) -> Asset:
        BloggerService(self.db).get_active(blogger_id)
        statement = select(Asset).where(
            Asset.id == asset_id,
            Asset.blogger_id == blogger_id,
        )
        if not include_deleted:
            statement = statement.where(Asset.deleted_at.is_(None))
        asset = self.db.scalar(statement)
        if asset is None:
            raise AssetNotFoundError("ASSET_NOT_FOUND")
        return asset

    def update_manual(
        self,
        blogger_id: int,
        asset_id: int,
        changes: dict[str, Any],
    ) -> Asset:
        asset = self.get(blogger_id, asset_id)
        current = self._asset_data(asset)
        current.update({key: value for key, value in changes.items() if value is not None})
        normalized = self._normalize(current, require_all=True)
        embedding_result = self.embedding.encode_documents([self._embedding_text(normalized)])[0]
        try:
            asset.lib_type = normalized["lib_type"]
            asset.category = normalized["category"]
            asset.title = normalized["title"]
            asset.content = normalized["content"]
            asset.tags_json = json.dumps(normalized["tags"], ensure_ascii=False)
            asset.source_type = normalized["source_type"]
            asset.credibility = normalized["credibility"]
            asset.origin = "manual"
            asset.manual_locked = True
            if any(key in changes for key in {"source_type", "source_url", "source_title", "publisher"}):
                self.db.execute(delete(AssetSource).where(AssetSource.asset_id == asset.id))
                source = self._upsert_source(normalized)
                if source is not None:
                    self.db.flush()
                    self.db.add(AssetSource(asset_id=asset.id, source_document_id=source.id))
            decision = self._decision(blogger_id, "asset_update", normalized, asset.id)
            self.db.add(decision)
            self.db.flush()
            asset.decision_id = decision.id
            embedding = self.db.get(AssetEmbedding, asset.id)
            if embedding is None:
                embedding = self._embedding_row(asset.id, embedding_result)
                self.db.add(embedding)
            else:
                self._apply_embedding(embedding, embedding_result)
            self.db.commit()
            self.db.refresh(asset)
        except Exception:
            self.db.rollback()
            raise
        self._sync_memory(asset)
        return asset

    def soft_delete(self, blogger_id: int, asset_id: int) -> Asset:
        asset = self.get(blogger_id, asset_id, include_deleted=True)
        if asset.deleted_at is not None:
            return asset
        asset.deleted_at = datetime.utcnow()
        decision = self._decision(
            blogger_id,
            "asset_delete",
            self._asset_data(asset),
            asset.id,
        )
        self.db.add(decision)
        self.db.flush()
        asset.decision_id = decision.id
        for memory in self.db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.blogger_id == blogger_id,
                MemoryRecord.source_type == "asset",
                MemoryRecord.source_id == str(asset.id),
                MemoryRecord.status == "active",
            )
        ):
            memory.status = "superseded"
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def _sync_memory(self, asset: Asset) -> None:
        if asset.lib_type != "knowledge":
            return
        if self._trusted(asset):
            self.memory_service.sync_verified_assets(
                asset.blogger_id,
                assets=[asset.id],
                include_source_documents=True,
            )
            return
        candidate = self.memory_service.create_memory(
            asset.blogger_id,
            "verified_knowledge",
            asset.title,
            asset.content,
            "asset",
            str(asset.id),
            confidence=float(asset.credibility) / 5.0,
            status="candidate",
            user_confirmed=False,
        )
        active_rows = list(
            self.db.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.blogger_id == asset.blogger_id,
                    MemoryRecord.source_type == "asset",
                    MemoryRecord.source_id == str(asset.id),
                    MemoryRecord.status == "active",
                    MemoryRecord.id != candidate.id,
                )
            )
        )
        if active_rows:
            for row in active_rows:
                row.status = "superseded"
            self.db.commit()

    @staticmethod
    def _trusted(asset: Asset) -> bool:
        return asset.source_type in TRUSTED_SOURCE_TYPES and asset.credibility >= 4

    def _normalize(self, data: dict[str, Any], *, require_all: bool) -> dict[str, Any]:
        required = {"lib_type", "category", "title", "content", "tags", "source_type", "credibility"}
        if require_all and any(key not in data for key in required):
            raise AssetValidationError("ASSET_REQUIRED_FIELDS_MISSING")
        lib_type = str(data.get("lib_type", "")).strip()
        if lib_type not in LIB_TYPES:
            raise AssetValidationError("ASSET_LIB_TYPE_INVALID")
        category = str(data.get("category", "")).strip()
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        source_type = str(data.get("source_type", "")).strip().lower()
        tags = data.get("tags")
        if not category or not title or not content or not source_type:
            raise AssetValidationError("ASSET_TEXT_FIELD_EMPTY")
        if not isinstance(tags, list) or not tags or any(not str(tag).strip() for tag in tags):
            raise AssetValidationError("ASSET_TAGS_INVALID")
        credibility = data.get("credibility")
        if isinstance(credibility, bool) or not isinstance(credibility, int) or not 0 <= credibility <= 5:
            raise AssetValidationError("ASSET_CREDIBILITY_INVALID")
        source_url = data.get("source_url")
        if source_url is not None and not str(source_url).startswith(("http://", "https://")):
            raise AssetValidationError("ASSET_SOURCE_URL_INVALID")
        if source_type in {"official", "verified", "verified_url"} and not source_url:
            raise AssetValidationError("ASSET_TRUSTED_SOURCE_URL_REQUIRED")
        return {
            **data,
            "lib_type": lib_type,
            "category": category,
            "title": title,
            "content": content,
            "tags": [str(tag).strip() for tag in tags],
            "source_type": source_type,
            "source_url": source_url,
            "credibility": credibility,
        }

    @staticmethod
    def _dedupe_key(data: dict[str, Any]) -> str:
        idempotency_key = str(data.get("idempotency_key") or "").strip()
        raw = (
            f"manual-idempotency|{idempotency_key}"
            if idempotency_key
            else "|".join(
                [
                    "manual",
                    data["lib_type"],
                    data["category"],
                    data["title"],
                    data["content"],
                    str(data.get("source_url") or data["source_type"]),
                ]
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _upsert_source(self, data: dict[str, Any]) -> SourceDocument | None:
        source_url = data.get("source_url")
        if not source_url:
            return None
        existing = self.db.scalar(select(SourceDocument).where(SourceDocument.url == source_url))
        if existing is not None:
            return existing
        content_hash = hashlib.sha256(
            f"{source_url}|{data['content']}".encode("utf-8")
        ).hexdigest()
        source = SourceDocument(
            title=str(data.get("source_title") or data["title"]),
            url=str(source_url),
            publisher=str(data.get("publisher") or "用户提供"),
            source_type=data["source_type"],
            verified_at=str(data.get("verified_at") or datetime.utcnow().date().isoformat()),
            content_excerpt=data["content"],
            content_hash=content_hash,
        )
        self.db.add(source)
        return source

    def _embedding_row(self, asset_id: int, result: Any) -> AssetEmbedding:
        vector = np.asarray(result.vector, dtype=np.float32)
        return AssetEmbedding(
            asset_id=asset_id,
            model_name=self.embedding.model_name,
            model_version="v1.5" if "bge-small" in self.embedding.model_name else "test",
            dimension=len(vector),
            vector=self.embedding.to_bytes(vector),
            vector_norm=float(np.linalg.norm(vector)),
            content_hash=result.content_hash,
        )

    def _apply_embedding(self, row: AssetEmbedding, result: Any) -> None:
        vector = np.asarray(result.vector, dtype=np.float32)
        row.model_name = self.embedding.model_name
        row.model_version = "v1.5" if "bge-small" in self.embedding.model_name else "test"
        row.dimension = len(vector)
        row.vector = self.embedding.to_bytes(vector)
        row.vector_norm = float(np.linalg.norm(vector))
        row.content_hash = result.content_hash

    @staticmethod
    def _embedding_text(data: dict[str, Any]) -> str:
        return (
            f"标题：{data['title']}\n分类：{data['category']}\n内容：{data['content']}"
            f"\n标签：{' '.join(data['tags'])}"
        )

    def _asset_data(self, asset: Asset) -> dict[str, Any]:
        source = self.db.scalar(
            select(SourceDocument)
            .join(AssetSource, AssetSource.source_document_id == SourceDocument.id)
            .where(AssetSource.asset_id == asset.id)
        )
        return {
            "lib_type": asset.lib_type,
            "category": asset.category,
            "title": asset.title,
            "content": asset.content,
            "tags": json.loads(asset.tags_json),
            "source_type": asset.source_type,
            "source_url": source.url if source else None,
            "source_title": source.title if source else None,
            "publisher": source.publisher if source else None,
            "verified_at": source.verified_at if source else None,
            "credibility": asset.credibility,
        }

    @staticmethod
    def _decision(
        blogger_id: int,
        decision_type: str,
        data: dict[str, Any],
        asset_id: int,
    ) -> DecisionLog:
        return DecisionLog(
            blogger_id=blogger_id,
            decision_type=decision_type,
            prompt_version="phase1-closure-v1",
            input_summary=json.dumps(data, ensure_ascii=False, default=str),
            decision=json.dumps({"asset_id": asset_id}, ensure_ascii=False),
            reason="用户手工创建、编辑或删除资产；资产锁定并保留操作审计",
        )

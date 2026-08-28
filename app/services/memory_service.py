"""长期记忆服务。

本模块只负责长期记忆的写入、晋升、版本化和语义检索。任务会话记忆由
``task_session`` 相关服务负责，避免把临时上下文误写入长期记忆。

设计约束：

* ``MemoryRecord`` 是事实记录，``MemoryEmbedding`` 是它的检索索引；新记录
  必须先完成向量化，向量化失败时整次写入回滚。
* 画像和可信来源事实可以在明确确认后直接进入 ``active``；模型推断、临时
  信息以及未确认的正式决策只能进入 ``candidate``。
* 更新不覆盖旧行，而是增加版本。确认后的新版本激活时，旧版本标记为
  ``superseded``。
* 检索接口必须接收 ``blogger_id``，查询条件也强制带上该字段，防止不同
  博主之间发生记忆泄漏。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    AssetSource,
    Blogger,
    DecisionLog,
    MemoryEmbedding,
    MemoryRecord,
    SourceDocument,
)
from app.services.embedding_service import EmbeddingService

SUPPORTED_MEMORY_TYPES = {
    "profile_fact",
    "user_preference",
    "verified_knowledge",
    "decision_summary",
}

# 这些值对应项目中 source_document 的来源类型。``user_confirmed`` 是
# 由用户明确确认的事实，不等同于模型自行推断。
TRUSTED_SOURCE_TYPES = {
    "official",
    "verified_url",
    "verified",
    "user_confirmed",
}

REJECTED_DECISION_MARKERS = {
    "failed",
    "failure",
    "error",
    "temporary",
    "temp",
    "draft",
    "inferred",
    "猜测",
    "临时",
    "失败",
}


class MemoryServiceError(RuntimeError):
    """长期记忆服务的基类异常。"""


class MemoryNotFoundError(MemoryServiceError):
    """请求的博主或记忆不存在。"""


class MemoryConfirmationRequiredError(MemoryServiceError):
    """未获得用户确认，不能将候选记忆激活。"""


class MemoryEmbeddingError(MemoryServiceError):
    """记忆向量化失败。"""


class MemoryValidationError(MemoryServiceError):
    """记忆输入不符合长期记忆约束。"""


class MemorySearchHit(dict[str, Any]):
    """可同时按字典和属性访问的检索结果。

    现有资产检索服务返回字典，部分调用方则更适合读取属性。这个轻量
    适配对象保持字典兼容，同时提供属性访问，不改变数据库模型。
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class _MemoryDraft:
    blogger_id: int
    memory_type: str
    title: str
    content: str
    source_type: str
    source_id: str | None
    confidence: float
    status: str
    user_confirmed: bool


class MemoryService:
    """长期记忆的统一写入与检索服务。

    ``embedding`` 是可注入的。生产环境默认使用项目配置的本地
    ``BAAI/bge-small-zh-v1.5``；测试可传入 ``FakeEmbeddingService``，也可以
    传入实现同一 ``encode_documents``/``encode_query`` 接口的替代向量器。
    """

    def __init__(
        self,
        db: Session,
        embedding: EmbeddingService | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        if embedding is not None and embedding_service is not None and embedding is not embedding_service:
            raise ValueError("EMBEDDING_SERVICE_AMBIGUOUS")
        self.db = db
        self.embedding = embedding or embedding_service or EmbeddingService()

    # ------------------------------------------------------------------
    # 画像、资产、来源和决策的融合入口
    # ------------------------------------------------------------------
    def sync_profile(
        self,
        blogger_id: int,
        user_confirmed: bool | None = None,
    ) -> MemoryRecord:
        """将已确认的博主画像融合为一条 ``profile_fact`` 记忆。

        ``Blogger.profile_state == complete`` 是现有画像确认流程写入的确认
        标记。调用方也可以显式传 ``user_confirmed=False``，让结果保持候选。
        """

        blogger = self._get_blogger(blogger_id)
        confirmed = blogger.profile_state == "complete" if user_confirmed is None else user_confirmed
        draft = _MemoryDraft(
            blogger_id=blogger.id,
            memory_type="profile_fact",
            title="博主画像（已确认）",
            content=self._profile_content(blogger),
            source_type="blogger",
            source_id=str(blogger.id),
            confidence=1.0 if confirmed else 0.5,
            status="active" if confirmed else "candidate",
            user_confirmed=confirmed,
        )
        return self._write_drafts([draft])[0]

    # 语义更明确的别名，便于画像确认服务集成。
    remember_profile = sync_profile
    ingest_profile = sync_profile
    remember_confirmed_profile = sync_profile
    create_profile_memory = sync_profile

    def sync_verified_assets(
        self,
        blogger_id: int,
        assets: Iterable[Asset | int] | None = None,
        include_source_documents: bool = True,
    ) -> list[MemoryRecord]:
        """将有可信来源的资产和来源事实写入长期记忆。

        仅接受未软删除、可信度至少为 4 且来源类型可信的资产；模型生成的
        ``generated_template``、无来源低可信资产会被跳过。若资产关联了
        ``SourceDocument``，默认还会保存一份来源事实，保留发布者和摘要。
        """

        self._get_blogger(blogger_id)
        rows = self._resolve_assets(blogger_id, assets)
        drafts: list[_MemoryDraft] = []
        source_ids: set[int] = set()
        for asset in rows:
            if not self._is_trusted_asset(asset):
                continue
            drafts.append(
                _MemoryDraft(
                    blogger_id=blogger_id,
                    memory_type="verified_knowledge",
                    title=asset.title,
                    content=self._asset_content(asset),
                    source_type="asset",
                    source_id=str(asset.id),
                    confidence=self._asset_confidence(asset),
                    status="active",
                    user_confirmed=True,
                )
            )
            if include_source_documents:
                for source in self._sources_for_asset(asset.id):
                    if self._is_trusted_source(source):
                        source_ids.add(source.id)

        if include_source_documents and source_ids:
            for source_id in sorted(source_ids):
                loaded_source = self.db.get(SourceDocument, source_id)
                if loaded_source is None:
                    continue
                drafts.append(
                    _MemoryDraft(
                        blogger_id=blogger_id,
                        memory_type="verified_knowledge",
                        title=f"来源：{loaded_source.title}",
                        content=self._source_content(loaded_source),
                        source_type="source_document",
                        source_id=str(loaded_source.id),
                        confidence=1.0,
                        status="active",
                        user_confirmed=True,
                    )
                )

        if not drafts:
            return []
        return self._write_drafts(drafts)

    # 常用的兼容别名。
    remember_verified_assets = sync_verified_assets
    ingest_verified_assets = sync_verified_assets
    sync_assets = sync_verified_assets

    def sync_verified_asset(self, blogger_id: int, asset: Asset | int) -> MemoryRecord | None:
        """同步单条可信资产，便于资产编辑后的增量更新。"""

        records = self.sync_verified_assets(blogger_id, [asset])
        return next((row for row in records if row.source_type == "asset"), None)

    remember_verified_asset = sync_verified_asset

    def sync_sources(self, blogger_id: int) -> list[MemoryRecord]:
        """只同步当前博主可信资产关联的来源文档。"""

        return self.sync_verified_assets(blogger_id, include_source_documents=True)

    def sync_decisions(
        self,
        blogger_id: int,
        decisions: Iterable[DecisionLog | int] | None = None,
        user_confirmed: bool = False,
    ) -> list[MemoryRecord]:
        """保存正式决策摘要。

        默认写入 ``candidate``。只有调用方明确传入 ``user_confirmed=True``
        才能激活，防止模型输出、临时决策或失败结果污染 active 长期记忆。
        画像确认过程中产生的 ``profile`` 决策，如果理由明确包含用户确认，
        视为已确认的正式决策。
        """

        self._get_blogger(blogger_id)
        rows = self._resolve_decisions(blogger_id, decisions)
        drafts: list[_MemoryDraft] = []
        for decision in rows:
            if not self._is_formal_decision(decision):
                continue
            profile_confirmation = decision.decision_type.lower() == "profile" and "确认" in decision.reason
            confirmed = user_confirmed or profile_confirmation
            drafts.append(
                _MemoryDraft(
                    blogger_id=blogger_id,
                    memory_type="decision_summary",
                    title=f"决策：{decision.decision_type}",
                    content=self._decision_content(decision),
                    source_type="decision_log",
                    source_id=str(decision.id),
                    confidence=1.0 if confirmed else 0.5,
                    status="active" if confirmed else "candidate",
                    user_confirmed=confirmed,
                )
            )
        if not drafts:
            return []
        return self._write_drafts(drafts)

    remember_decisions = sync_decisions
    ingest_decisions = sync_decisions

    def sync_decision(
        self,
        blogger_id: int,
        decision: DecisionLog | int,
        user_confirmed: bool = False,
    ) -> MemoryRecord | None:
        """同步单条决策摘要。"""

        records = self.sync_decisions(blogger_id, [decision], user_confirmed=user_confirmed)
        return records[0] if records else None

    remember_decision = sync_decision

    def sync_all(
        self,
        blogger_id: int,
        *,
        user_confirmed_profile: bool | None = None,
        user_confirmed_decisions: bool = False,
        include_source_documents: bool = True,
    ) -> list[MemoryRecord]:
        """在一个事务中融合画像、可信资产、来源和决策。"""

        blogger = self._get_blogger(blogger_id)
        profile_confirmed = (
            blogger.profile_state == "complete" if user_confirmed_profile is None else user_confirmed_profile
        )
        drafts = [
            _MemoryDraft(
                blogger_id=blogger.id,
                memory_type="profile_fact",
                title="博主画像（已确认）",
                content=self._profile_content(blogger),
                source_type="blogger",
                source_id=str(blogger.id),
                confidence=1.0 if profile_confirmed else 0.5,
                status="active" if profile_confirmed else "candidate",
                user_confirmed=profile_confirmed,
            )
        ]
        assets = self._resolve_assets(blogger_id, None)
        source_ids: set[int] = set()
        for asset in assets:
            if not self._is_trusted_asset(asset):
                continue
            drafts.append(
                _MemoryDraft(
                    blogger_id=blogger_id,
                    memory_type="verified_knowledge",
                    title=asset.title,
                    content=self._asset_content(asset),
                    source_type="asset",
                    source_id=str(asset.id),
                    confidence=self._asset_confidence(asset),
                    status="active",
                    user_confirmed=True,
                )
            )
            if include_source_documents:
                source_ids.update(
                    source.id for source in self._sources_for_asset(asset.id) if self._is_trusted_source(source)
                )
        if include_source_documents:
            for source_id in sorted(source_ids):
                source = self.db.get(SourceDocument, source_id)
                if source is not None:
                    drafts.append(
                        _MemoryDraft(
                            blogger_id=blogger_id,
                            memory_type="verified_knowledge",
                            title=f"来源：{source.title}",
                            content=self._source_content(source),
                            source_type="source_document",
                            source_id=str(source.id),
                            confidence=1.0,
                            status="active",
                            user_confirmed=True,
                        )
                    )
        for decision in self._resolve_decisions(blogger_id, None):
            if not self._is_formal_decision(decision):
                continue
            profile_confirmation = decision.decision_type.lower() == "profile" and "确认" in decision.reason
            confirmed = user_confirmed_decisions or profile_confirmation
            drafts.append(
                _MemoryDraft(
                    blogger_id=blogger_id,
                    memory_type="decision_summary",
                    title=f"决策：{decision.decision_type}",
                    content=self._decision_content(decision),
                    source_type="decision_log",
                    source_id=str(decision.id),
                    confidence=1.0 if confirmed else 0.5,
                    status="active" if confirmed else "candidate",
                    user_confirmed=confirmed,
                )
            )
        return self._write_drafts(drafts)

    fuse = sync_all
    build_long_term_memory = sync_all

    # ------------------------------------------------------------------
    # 通用记忆写入、晋升与版本更新
    # ------------------------------------------------------------------
    def create_memory(
        self,
        blogger_id: int,
        memory_type: str,
        title: str,
        content: str,
        source_type: str,
        source_id: str | int | None = None,
        confidence: float = 1.0,
        status: str = "candidate",
        user_confirmed: bool = False,
    ) -> MemoryRecord:
        """创建一条长期记忆。

        直接请求 ``status='active'`` 但没有用户确认时，会安全降级为
        ``candidate``，而不是绕过晋升规则。
        """

        self._get_blogger(blogger_id)
        draft = self._make_draft(
            blogger_id=blogger_id,
            memory_type=memory_type,
            title=title,
            content=content,
            source_type=source_type,
            source_id=source_id,
            confidence=confidence,
            status=status,
            user_confirmed=user_confirmed,
        )
        return self._write_drafts([draft])[0]

    def promote_memory(self, memory_id: int, user_confirmed: bool = False) -> MemoryRecord:
        """在明确用户确认后将候选记忆晋升为 active。"""

        record = self.db.get(MemoryRecord, memory_id)
        if record is None:
            raise MemoryNotFoundError("MEMORY_NOT_FOUND")
        self._get_blogger(record.blogger_id)
        if record.status == "active":
            return record
        if record.status != "candidate":
            raise MemoryValidationError("MEMORY_NOT_PROMOTABLE")
        if not user_confirmed:
            raise MemoryConfirmationRequiredError("MEMORY_USER_CONFIRMATION_REQUIRED")

        try:
            # 手工插入的 candidate 可能尚未有索引；晋升前补齐并仍受事务保护。
            if record.embedding is None:
                self.db.flush()
                self._attach_embeddings([record])
            self._supersede_active_siblings(record)
            record.status = "active"
            record.confidence = max(record.confidence, 1.0)
            self.db.commit()
            self.db.refresh(record)
            return record
        except Exception:
            self.db.rollback()
            raise

    promote_candidate = promote_memory
    promote = promote_memory
    add_memory = create_memory

    def update_memory(
        self,
        memory_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        confidence: float | None = None,
        user_confirmed: bool = False,
    ) -> MemoryRecord:
        """以新版本更新记忆，不静默覆盖原记录。

        未确认的更新会生成 candidate，并保留当前 active 版本；确认更新则
        新版本 active、旧版本 superseded。两种路径都会重新生成向量。
        """

        old = self.db.get(MemoryRecord, memory_id)
        if old is None:
            raise MemoryNotFoundError("MEMORY_NOT_FOUND")
        self._get_blogger(old.blogger_id)
        new_title = title if title is not None else old.title
        new_content = content if content is not None else old.content
        new_confidence = confidence if confidence is not None else old.confidence
        draft = self._make_draft(
            blogger_id=old.blogger_id,
            memory_type=old.memory_type,
            title=new_title,
            content=new_content,
            source_type=old.source_type,
            source_id=old.source_id,
            confidence=new_confidence,
            status="active" if user_confirmed else "candidate",
            user_confirmed=user_confirmed,
        )
        try:
            version = self._next_version(old)
            record = self._new_record(draft, version=version, parent_memory_id=old.id)
            self.db.add(record)
            self.db.flush()
            self._attach_embeddings([record])
            if user_confirmed:
                self._supersede_active_siblings(record, exclude_id=old.id)
                old.status = "superseded"
                record.status = "active"
            self.db.commit()
            self.db.refresh(record)
            return record
        except Exception:
            self.db.rollback()
            raise

    update = update_memory

    # ------------------------------------------------------------------
    # 候选查询、单条获取和语义检索
    # ------------------------------------------------------------------
    def get_memory(self, memory_id: int) -> MemoryRecord:
        record = self.db.get(MemoryRecord, memory_id)
        if record is None:
            raise MemoryNotFoundError("MEMORY_NOT_FOUND")
        self._get_blogger(record.blogger_id)
        return record

    def list_memories(
        self,
        blogger_id: int,
        *,
        status: str | None = "active",
        memory_type: str | None = None,
    ) -> list[MemoryRecord]:
        self._get_blogger(blogger_id)
        statement = select(MemoryRecord).where(MemoryRecord.blogger_id == blogger_id)
        if status is not None:
            statement = statement.where(MemoryRecord.status == status)
        if memory_type is not None:
            statement = statement.where(MemoryRecord.memory_type == memory_type)
        statement = statement.order_by(MemoryRecord.created_at.desc(), MemoryRecord.id.desc())
        return list(self.db.scalars(statement))

    def semantic_search(
        self,
        blogger_id: int,
        query: str,
        limit: int = 10,
        min_confidence: float = 0.0,
        memory_types: Sequence[str] | None = None,
    ) -> list[MemorySearchHit]:
        """仅在指定博主的 active 记忆中进行向量检索。"""

        self._get_blogger(blogger_id)
        if not query or not query.strip():
            return []
        if limit < 1:
            raise ValueError("MEMORY_SEARCH_LIMIT_INVALID")
        if not 0 <= min_confidence <= 1:
            raise ValueError("MEMORY_SEARCH_CONFIDENCE_INVALID")
        try:
            query_vector = self._normalise_vector(self.embedding.encode_query(query))
        except Exception as exc:
            if isinstance(exc, MemoryEmbeddingError):
                raise
            raise MemoryEmbeddingError("MEMORY_QUERY_EMBEDDING_FAILED") from exc

        statement = (
            select(MemoryRecord, MemoryEmbedding)
            .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryRecord.id)
            .where(
                MemoryRecord.blogger_id == blogger_id,
                MemoryRecord.status == "active",
                MemoryRecord.confidence >= min_confidence,
            )
        )
        if memory_types:
            invalid = set(memory_types) - SUPPORTED_MEMORY_TYPES
            if invalid:
                raise ValueError(f"MEMORY_TYPE_INVALID:{','.join(sorted(invalid))}")
            statement = statement.where(MemoryRecord.memory_type.in_(memory_types))

        hits: list[MemorySearchHit] = []
        for record, embedding in self.db.execute(statement):
            try:
                vector = self._normalise_vector(self.embedding.from_bytes(embedding.vector))
            except Exception:
                # 已存在的损坏索引不应中断其他记忆的检索；写入路径不会产生
                # 这种记录，因此这里安全跳过即可。
                continue
            if vector.shape != query_vector.shape:
                continue
            similarity = float(np.dot(query_vector, vector))
            hits.append(self._serialize_hit(record, similarity))
        hits.sort(
            key=lambda item: (
                -float(item["similarity"]),
                -float(item["confidence"]),
                -int(item["version"]),
                int(item["id"]),
            )
        )
        return hits[:limit]

    search = semantic_search
    retrieve = semantic_search

    def search_records(
        self,
        blogger_id: int,
        query: str,
        limit: int = 10,
        min_confidence: float = 0.0,
        memory_types: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        """返回 ORM 记录版本的检索结果，供需要继续访问关系的调用方使用。"""

        hits = self.semantic_search(
            blogger_id,
            query,
            limit=limit,
            min_confidence=min_confidence,
            memory_types=memory_types,
        )
        ids = [int(hit["id"]) for hit in hits]
        if not ids:
            return []
        records = {row.id: row for row in self.db.scalars(select(MemoryRecord).where(MemoryRecord.id.in_(ids)))}
        return [records[row_id] for row_id in ids if row_id in records]

    # ------------------------------------------------------------------
    # 内部事务和数据转换
    # ------------------------------------------------------------------
    def _new_record(
        self,
        draft: _MemoryDraft,
        *,
        version: int,
        parent_memory_id: int | None = None,
    ) -> MemoryRecord:
        return MemoryRecord(
            blogger_id=draft.blogger_id,
            memory_type=draft.memory_type,
            title=draft.title.strip(),
            content=draft.content.strip(),
            source_type=draft.source_type.strip(),
            source_id=None if draft.source_id is None else str(draft.source_id),
            confidence=draft.confidence,
            status=draft.status,
            version=version,
            parent_memory_id=parent_memory_id,
            content_hash=self._content_hash(draft.content),
        )

    def _attach_embeddings(self, records: Sequence[MemoryRecord]) -> None:
        if not records:
            return
        texts = [self._embedding_text(record) for record in records]
        try:
            encoded = self.embedding.encode_documents(texts)
        except Exception as exc:
            raise MemoryEmbeddingError("MEMORY_EMBEDDING_FAILED") from exc
        if len(encoded) != len(records):
            raise MemoryEmbeddingError("MEMORY_EMBEDDING_COUNT_MISMATCH")
        model_name = str(getattr(self.embedding, "model_name", "unknown"))
        model_version = "v1.5" if "bge-small-zh" in model_name else "test"
        for record, embedding_result in zip(records, encoded, strict=True):
            try:
                vector = self._normalise_vector(embedding_result.vector)
                vector_bytes = self.embedding.to_bytes(vector)
                content_hash = str(embedding_result.content_hash)
            except Exception as exc:
                raise MemoryEmbeddingError("MEMORY_EMBEDDING_INVALID") from exc
            self.db.add(
                MemoryEmbedding(
                    memory_id=record.id,
                    model_name=model_name,
                    model_version=model_version,
                    dimension=int(vector.size),
                    vector=vector_bytes,
                    vector_norm=float(np.linalg.norm(vector)),
                    content_hash=content_hash,
                )
            )
        self.db.flush()

    def _find_same_current(self, draft: _MemoryDraft) -> MemoryRecord | None:
        statement = select(MemoryRecord).where(
            MemoryRecord.blogger_id == draft.blogger_id,
            MemoryRecord.memory_type == draft.memory_type,
            MemoryRecord.source_type == draft.source_type,
            MemoryRecord.source_id == (None if draft.source_id is None else str(draft.source_id)),
            MemoryRecord.status.in_(["active", "candidate"]),
        )
        rows = list(self.db.scalars(statement.order_by(MemoryRecord.version.desc())))
        content_hash = self._content_hash(draft.content)
        for row in rows:
            if row.content_hash == content_hash and row.title == draft.title:
                # 如果调用方要求 active，而当前同内容仍是 candidate，用户
                # 确认后应走晋升而不是继续制造相同版本。
                if draft.status == "active" and row.status == "candidate" and draft.user_confirmed:
                    self._supersede_active_siblings(row)
                    row.status = "active"
                    row.confidence = max(row.confidence, draft.confidence)
                return row
        return None

    def _next_version_for_draft(self, draft: _MemoryDraft) -> int:
        statement = select(MemoryRecord.version).where(
            MemoryRecord.blogger_id == draft.blogger_id,
            MemoryRecord.memory_type == draft.memory_type,
            MemoryRecord.source_type == draft.source_type,
            MemoryRecord.source_id == (None if draft.source_id is None else str(draft.source_id)),
        )
        versions = [int(version) for version in self.db.scalars(statement)]
        return max(versions, default=0) + 1

    def _next_version(self, old: MemoryRecord) -> int:
        statement = select(MemoryRecord.version).where(
            MemoryRecord.blogger_id == old.blogger_id,
            MemoryRecord.memory_type == old.memory_type,
            MemoryRecord.source_type == old.source_type,
            MemoryRecord.source_id == old.source_id,
        )
        versions = [int(version) for version in self.db.scalars(statement)]
        return max(versions, default=old.version) + 1

    def _supersede_active_siblings(
        self,
        record: MemoryRecord,
        *,
        exclude_id: int | None = None,
    ) -> None:
        statement = select(MemoryRecord).where(
            MemoryRecord.blogger_id == record.blogger_id,
            MemoryRecord.memory_type == record.memory_type,
            MemoryRecord.source_type == record.source_type,
            MemoryRecord.source_id == record.source_id,
            MemoryRecord.status == "active",
        )
        for sibling in self.db.scalars(statement):
            if sibling.id != record.id and sibling.id != exclude_id:
                sibling.status = "superseded"

    def _make_draft(
        self,
        *,
        blogger_id: int,
        memory_type: str,
        title: str,
        content: str,
        source_type: str,
        source_id: str | int | None,
        confidence: float,
        status: str,
        user_confirmed: bool,
    ) -> _MemoryDraft:
        if memory_type not in SUPPORTED_MEMORY_TYPES:
            raise MemoryValidationError("MEMORY_TYPE_INVALID")
        if not title or not title.strip() or not content or not content.strip():
            raise MemoryValidationError("MEMORY_CONTENT_EMPTY")
        if not source_type or not source_type.strip():
            raise MemoryValidationError("MEMORY_SOURCE_TYPE_EMPTY")
        if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
            raise MemoryValidationError("MEMORY_CONFIDENCE_INVALID")
        if status not in {"candidate", "active"}:
            raise MemoryValidationError("MEMORY_STATUS_INVALID")
        confirmed_status = "active" if status == "active" and user_confirmed else "candidate"
        return _MemoryDraft(
            blogger_id=blogger_id,
            memory_type=memory_type,
            title=title,
            content=content,
            source_type=source_type,
            source_id=None if source_id is None else str(source_id),
            confidence=float(confidence),
            status=confirmed_status,
            user_confirmed=bool(user_confirmed),
        )

    def _write_drafts(self, drafts: Sequence[_MemoryDraft]) -> list[MemoryRecord]:
        """在单一事务中写入一批草稿。"""

        if not drafts:
            return []
        new_records: list[MemoryRecord] = []
        result: list[MemoryRecord] = []
        try:
            for draft in drafts:
                existing = self._find_same_current(draft)
                if existing is not None:
                    result.append(existing)
                    continue
                version = self._next_version_for_draft(draft)
                record = self._new_record(draft, version=version)
                self.db.add(record)
                self.db.flush()
                if draft.status == "active" and draft.user_confirmed:
                    self._supersede_active_siblings(record)
                    record.status = "active"
                new_records.append(record)
                result.append(record)
            if new_records:
                self._attach_embeddings(new_records)
            self.db.commit()
            for record in result:
                self.db.refresh(record)
            return result
        except Exception:
            self.db.rollback()
            raise

    # ------------------------------------------------------------------
    # 查询和格式化辅助
    # ------------------------------------------------------------------
    def _get_blogger(self, blogger_id: int) -> Blogger:
        blogger = self.db.scalar(
            select(Blogger).where(
                Blogger.id == blogger_id,
                Blogger.deleted_at.is_(None),
            )
        )
        if blogger is None:
            raise MemoryNotFoundError("BLOGGER_NOT_FOUND")
        return blogger

    def _resolve_assets(
        self,
        blogger_id: int,
        assets: Iterable[Asset | int] | None,
    ) -> list[Asset]:
        if assets is None:
            return list(
                self.db.scalars(
                    select(Asset)
                    .where(Asset.blogger_id == blogger_id, Asset.deleted_at.is_(None))
                    .order_by(Asset.id.asc())
                )
            )
        resolved: list[Asset] = []
        for value in assets:
            asset = value if isinstance(value, Asset) else self.db.get(Asset, int(value))
            if asset is not None and asset.blogger_id == blogger_id and asset.deleted_at is None:
                resolved.append(asset)
        return resolved

    def _resolve_decisions(
        self,
        blogger_id: int,
        decisions: Iterable[DecisionLog | int] | None,
    ) -> list[DecisionLog]:
        if decisions is None:
            return list(
                self.db.scalars(
                    select(DecisionLog).where(DecisionLog.blogger_id == blogger_id).order_by(DecisionLog.id.asc())
                )
            )
        resolved: list[DecisionLog] = []
        for value in decisions:
            decision = value if isinstance(value, DecisionLog) else self.db.get(DecisionLog, int(value))
            if decision is not None and decision.blogger_id == blogger_id:
                resolved.append(decision)
        return resolved

    def _sources_for_asset(self, asset_id: int) -> list[SourceDocument]:
        statement = (
            select(SourceDocument)
            .join(AssetSource, AssetSource.source_document_id == SourceDocument.id)
            .where(AssetSource.asset_id == asset_id)
            .order_by(SourceDocument.id.asc())
        )
        return list(self.db.scalars(statement))

    @staticmethod
    def _is_trusted_source(source: SourceDocument) -> bool:
        return source.source_type.lower() in TRUSTED_SOURCE_TYPES

    def _is_trusted_asset(self, asset: Asset) -> bool:
        if asset.deleted_at is not None or asset.credibility < 4:
            return False
        source_type = asset.source_type.lower()
        if source_type not in TRUSTED_SOURCE_TYPES:
            return False
        linked_sources = self._sources_for_asset(asset.id)
        if linked_sources:
            return any(self._is_trusted_source(source) for source in linked_sources)
        # 允许导入时尚未建立关系表的官方种子；source_type 本身仍是来源声明。
        return source_type in {"official", "verified_url", "verified", "user_confirmed"}

    @staticmethod
    def _asset_confidence(asset: Asset) -> float:
        return min(1.0, max(0.0, float(asset.credibility) / 5.0))

    @staticmethod
    def _is_formal_decision(decision: DecisionLog) -> bool:
        decision_type = (decision.decision_type or "").lower()
        text = " ".join([decision_type, decision.reason or "", decision.decision or ""]).lower()
        return not any(marker in text for marker in REJECTED_DECISION_MARKERS)

    @staticmethod
    def _profile_content(blogger: Blogger) -> str:
        def parse_array(value: str | None) -> list[str]:
            if not value:
                return []
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return [value]
            return parsed if isinstance(parsed, list) else [str(parsed)]

        payload = {
            "blogger_id": blogger.id,
            "name": blogger.name,
            "platform": blogger.platform,
            "content_types": parse_array(blogger.content_types_json),
            "style": blogger.style,
            "follower_band": blogger.follower_band,
            "monetization_types": parse_array(blogger.monetization_types_json),
            "routes": blogger.routes,
            "viral_topic": blogger.viral_topic,
            "frequency": blogger.frequency,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _asset_content(asset: Asset) -> str:
        try:
            tags = json.loads(asset.tags_json or "[]")
        except json.JSONDecodeError:
            tags = [asset.tags_json] if asset.tags_json else []
        payload = {
            "asset_id": asset.id,
            "library": asset.lib_type,
            "category": asset.category,
            "title": asset.title,
            "content": asset.content,
            "tags": tags if isinstance(tags, list) else [str(tags)],
            "source_type": asset.source_type,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _source_content(source: SourceDocument) -> str:
        return json.dumps(
            {
                "source_document_id": source.id,
                "title": source.title,
                "publisher": source.publisher,
                "url": source.url,
                "source_type": source.source_type,
                "verified_at": source.verified_at,
                "content_excerpt": source.content_excerpt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _decision_content(decision: DecisionLog) -> str:
        try:
            value = json.loads(decision.decision)
        except (TypeError, json.JSONDecodeError):
            value = decision.decision
        return json.dumps(
            {
                "decision_log_id": decision.id,
                "decision_type": decision.decision_type,
                "prompt_version": decision.prompt_version,
                "decision": value,
                "reason": decision.reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _embedding_text(record: MemoryRecord) -> str:
        return f"标题：{record.title}\n类型：{record.memory_type}\n内容：{record.content}"

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalise_vector(vector: Any) -> np.ndarray:
        array = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(array))
        if array.size == 0 or not np.isfinite(array).all() or not math.isfinite(norm) or norm <= 0:
            raise MemoryEmbeddingError("MEMORY_EMBEDDING_VECTOR_INVALID")
        return array / norm

    @staticmethod
    def _serialize_hit(record: MemoryRecord, similarity: float) -> MemorySearchHit:
        return MemorySearchHit(
            {
                "id": record.id,
                "blogger_id": record.blogger_id,
                "memory_type": record.memory_type,
                "title": record.title,
                "content": record.content,
                "source_type": record.source_type,
                "source_id": record.source_id,
                "confidence": record.confidence,
                "status": record.status,
                "version": record.version,
                "similarity": similarity,
            }
        )


# 便于按“长期记忆服务”名称导入；主实现保持 MemoryService 简洁。
LongTermMemoryService = MemoryService

__all__ = [
    "LongTermMemoryService",
    "MemoryConfirmationRequiredError",
    "MemoryEmbeddingError",
    "MemoryNotFoundError",
    "MemorySearchHit",
    "MemoryService",
    "MemoryServiceError",
    "MemoryValidationError",
]

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Asset,
    AssetEmbedding,
    AssetSource,
    Blogger,
    BuildRun,
    DecisionLog,
    SourceDocument,
)
from app.services.deepseek_client import DeepSeekClient
from app.services.embedding_service import EmbeddingService
from app.services.place_service import PlaceService


class LibraryBuildService:
    def __init__(
        self,
        db: Session,
        deepseek: DeepSeekClient | None = None,
        embedding: EmbeddingService | None = None,
    ) -> None:
        self.db = db
        self.deepseek = deepseek or DeepSeekClient()
        self.embedding = embedding or EmbeddingService()

    def start_build(self, blogger_id: int, idempotency_key: str) -> BuildRun:
        existing = self.db.scalar(select(BuildRun).where(BuildRun.idempotency_key == idempotency_key))
        if existing:
            return existing
        blogger = self.db.scalar(
            select(Blogger).where(
                Blogger.id == blogger_id,
                Blogger.deleted_at.is_(None),
            )
        )
        if blogger is None:
            raise ValueError("BLOGGER_NOT_FOUND")
        run = BuildRun(
            blogger_id=blogger_id,
            status="pending",
            idempotency_key=idempotency_key,
            input_snapshot=self._profile_json(blogger),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def execute_build(self, run_id: int) -> BuildRun:
        run = self.db.get(BuildRun, run_id)
        if run is None:
            raise ValueError("BUILD_RUN_NOT_FOUND")
        if run.status == "succeeded":
            return run
        run.status = "running"
        run.started_at = datetime.utcnow()
        self.db.commit()
        try:
            blogger = self.db.scalar(
                select(Blogger).where(
                    Blogger.id == run.blogger_id,
                    Blogger.deleted_at.is_(None),
                )
            )
            if blogger is None:
                raise ValueError("BLOGGER_NOT_FOUND")
            profile = json.loads(self._profile_json(blogger))
            seeds = json.loads(settings.seed_file.read_text(encoding="utf-8"))
            self._validate_seeds(seeds)
            generated = self.deepseek.generate_support_assets(profile, [item["title"] for item in seeds])
            drafts = [
                {
                    "lib_type": "knowledge",
                    "category": item["category"],
                    "title": item["title"],
                    "content": item["content"],
                    "tags": item["tags"],
                    "source_type": item["source_type"],
                    "credibility": item["credibility"],
                    "seed": item,
                    "reason": "来自已核验贵州文旅测试种子",
                }
                for item in seeds
            ]
            drafts.extend(
                {
                    "lib_type": item.lib_type,
                    "category": item.category,
                    "title": item.title,
                    "content": item.content,
                    "tags": item.tags,
                    "source_type": "generated_template",
                    "credibility": 1,
                    "seed": None,
                    "reason": item.reason,
                }
                for item in generated
            )
            texts = [self._embedding_text(item) for item in drafts]
            vectors = self.embedding.encode_documents(texts)
            if len(vectors) != len(drafts):
                raise RuntimeError("EMBEDDING_COUNT_MISMATCH")

            decision = DecisionLog(
                blogger_id=blogger.id,
                build_run_id=run.id,
                decision_type="build",
                prompt_version="phase1-v1",
                input_summary=json.dumps(profile, ensure_ascii=False),
                decision=json.dumps(
                    {"knowledge": len(seeds), "material": 5, "algorithm": 3},
                    ensure_ascii=False,
                ),
                reason="主领域采用权威种子，素材与算法按画像生成；全部在入库前向量化",
            )
            self.db.add(decision)
            self.db.flush()

            inserted = 0
            for draft, embedding_result in zip(drafts, vectors, strict=True):
                dedupe_key = self._dedupe_key(draft)
                exists = self.db.scalar(
                    select(Asset).where(
                        Asset.blogger_id == blogger.id,
                        Asset.dedupe_key == dedupe_key,
                    )
                )
                if exists:
                    continue
                asset = Asset(
                    blogger_id=blogger.id,
                    lib_type=draft["lib_type"],
                    category=draft["category"],
                    title=draft["title"],
                    content=draft["content"],
                    tags_json=json.dumps(draft["tags"], ensure_ascii=False),
                    source_type=draft["source_type"],
                    credibility=draft["credibility"],
                    origin="seed" if draft["seed"] else "agent",
                    dedupe_key=dedupe_key,
                    decision_id=decision.id,
                )
                self.db.add(asset)
                self.db.flush()
                vector = embedding_result.vector
                self.db.add(
                    AssetEmbedding(
                        asset_id=asset.id,
                        model_name=self.embedding.model_name,
                        model_version="v1.5" if "bge-small" in self.embedding.model_name else "test",
                        dimension=len(vector),
                        vector=self.embedding.to_bytes(vector),
                        vector_norm=float(np.linalg.norm(vector)),
                        content_hash=embedding_result.content_hash,
                    )
                )
                if draft["seed"]:
                    source = self._upsert_source(draft["seed"])
                    self.db.flush()
                    self.db.add(AssetSource(asset_id=asset.id, source_document_id=source.id))
                inserted += 1

            places = PlaceService(self.db).sync_trusted_seeds(blogger.id, seeds, commit=False)
            summary = {
                "inserted": inserted,
                "places_inserted": len(places),
                "knowledge": len(seeds),
                "material": 5,
                "algorithm": 3,
                "embedding_model": self.embedding.model_name,
                "device": self.embedding.device,
            }
            run.status = "succeeded"
            run.output_summary = json.dumps(summary, ensure_ascii=False)
            run.finished_at = datetime.utcnow()
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            run = self.db.get(BuildRun, run_id)
            if run is None:
                raise RuntimeError("BUILD_RUN_LOST_AFTER_ROLLBACK") from exc
            run.status = "failed"
            run.error_code = exc.__class__.__name__
            run.error_message = str(exc)[:1000]
            run.finished_at = datetime.utcnow()
            self.db.commit()
        return run

    def _upsert_source(self, seed: dict) -> SourceDocument:
        existing = self.db.scalar(select(SourceDocument).where(SourceDocument.url == seed["source_url"]))
        if existing:
            return existing
        content_hash = hashlib.sha256(seed["content"].encode("utf-8")).hexdigest()
        source = SourceDocument(
            title=seed["source_title"],
            url=seed["source_url"],
            publisher=seed["publisher"],
            source_type=seed["source_type"],
            verified_at=seed["verified_at"],
            content_excerpt=seed["content"],
            content_hash=content_hash,
        )
        self.db.add(source)
        return source

    @staticmethod
    def _profile_json(blogger: Blogger) -> str:
        return json.dumps(
            {
                "name": blogger.name,
                "platform": blogger.platform,
                "content_types": json.loads(blogger.content_types_json),
                "style": blogger.style,
                "follower_band": blogger.follower_band,
                "monetization_types": json.loads(blogger.monetization_types_json),
                "routes": blogger.routes,
                "viral_topic": blogger.viral_topic,
                "frequency": blogger.frequency,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _dedupe_key(item: dict) -> str:
        raw = f"{item['lib_type']}|{item['category']}|{item['title']}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _embedding_text(item: dict) -> str:
        tags = " ".join(item["tags"])
        return f"标题：{item['title']}\n分类：{item['category']}\n内容：{item['content']}\n标签：{tags}"

    @staticmethod
    def _validate_seeds(seeds: list[dict]) -> None:
        if len(seeds) < 15:
            raise ValueError("TRUSTED_SEED_INSUFFICIENT")
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
        for index, seed in enumerate(seeds):
            if required.difference(seed) or seed.get("credibility", 0) < 4:
                raise ValueError(f"TRUSTED_SEED_INVALID:{index}")

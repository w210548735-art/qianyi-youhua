from __future__ import annotations

import json

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Asset, AssetEmbedding, AssetSource, Blogger, SourceDocument
from app.services.embedding_service import EmbeddingService


class AssetSearchService:
    def __init__(self, db: Session, embedding: EmbeddingService | None = None) -> None:
        self.db = db
        self.embedding = embedding or EmbeddingService()

    def search(
        self,
        blogger_id: int,
        query: str | None = None,
        lib_type: str | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags: list[str] | None = None,
        source_type: str | None = None,
        source: str | None = None,
        min_credibility: int | None = None,
        max_credibility: int | None = None,
    ) -> list[dict]:
        active_blogger = self.db.scalar(
            select(Blogger.id).where(
                Blogger.id == blogger_id,
                Blogger.deleted_at.is_(None),
            )
        )
        if active_blogger is None:
            return []
        statement = select(Asset).where(
            Asset.blogger_id == blogger_id,
            Asset.deleted_at.is_(None),
        )
        if lib_type:
            statement = statement.where(Asset.lib_type == lib_type)
        if category:
            statement = statement.where(Asset.category == category)
        if tags:
            for tag in tags:
                escaped_tag = tag.replace("%", "\\%").replace("_", "\\_").replace('"', '\\"')
                statement = statement.where(
                    Asset.tags_json.like(f'%"{escaped_tag}"%', escape="\\")
                )
        if source_type:
            statement = statement.where(Asset.source_type == source_type)
        if min_credibility is not None:
            statement = statement.where(Asset.credibility >= min_credibility)
        if max_credibility is not None:
            statement = statement.where(Asset.credibility <= max_credibility)
        if source:
            escaped_source = source.replace("%", "\\%").replace("_", "\\_")
            source_asset_ids = (
                select(AssetSource.asset_id)
                .join(SourceDocument, AssetSource.source_document_id == SourceDocument.id)
                .where(
                    or_(
                        SourceDocument.title.like(f"%{escaped_source}%", escape="\\"),
                        SourceDocument.publisher.like(f"%{escaped_source}%", escape="\\"),
                        SourceDocument.url.like(f"%{escaped_source}%", escape="\\"),
                    )
                )
            )
            statement = statement.where(Asset.id.in_(source_asset_ids))
        keyword_ids: set[int] = set()
        if query:
            escaped = query.replace("%", "\\%").replace("_", "\\_")
            keyword_statement = statement.where(
                or_(
                    Asset.title.like(f"%{escaped}%", escape="\\"),
                    Asset.content.like(f"%{escaped}%", escape="\\"),
                    Asset.tags_json.like(f"%{escaped}%", escape="\\"),
                )
            )
            keyword_ids = {asset.id for asset in self.db.scalars(keyword_statement)}
        assets = list(self.db.scalars(statement.limit(1000)))
        scores: dict[int, float] = {}
        if query and assets:
            query_vector = self.embedding.encode_query(query)
            embeddings = {
                item.asset_id: self.embedding.from_bytes(item.vector)
                for item in self.db.scalars(
                    select(AssetEmbedding).where(AssetEmbedding.asset_id.in_([a.id for a in assets]))
                )
            }
            comparable = [
                asset for asset in assets if asset.id in embeddings and len(embeddings[asset.id]) == len(query_vector)
            ]
            if comparable:
                matrix = np.stack([embeddings[asset.id] for asset in comparable])
                similarities = matrix @ query_vector
                scores.update(
                    {asset.id: float(similarity) for asset, similarity in zip(comparable, similarities, strict=True)}
                )
            for asset in assets:
                scores.setdefault(asset.id, 0.0)
                if asset.id in keyword_ids:
                    scores[asset.id] += 0.25
            assets.sort(key=lambda item: (scores.get(item.id, 0.0), item.id), reverse=True)
        else:
            assets.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        page = assets[offset : offset + limit]
        sources = self._sources_by_asset([asset.id for asset in page])
        return [self._serialize(asset, scores.get(asset.id), sources.get(asset.id, [])) for asset in page]

    def get(self, blogger_id: int, asset_id: int) -> dict | None:
        active_blogger = self.db.scalar(
            select(Blogger.id).where(
                Blogger.id == blogger_id,
                Blogger.deleted_at.is_(None),
            )
        )
        if active_blogger is None:
            return None
        asset = self.db.scalar(
            select(Asset).where(
                Asset.id == asset_id,
                Asset.blogger_id == blogger_id,
                Asset.deleted_at.is_(None),
            )
        )
        return self._serialize(asset, None) if asset is not None else None

    def _sources_by_asset(self, asset_ids: list[int]) -> dict[int, list[SourceDocument]]:
        if not asset_ids:
            return {}
        rows = self.db.execute(
            select(AssetSource.asset_id, SourceDocument)
            .join(SourceDocument, AssetSource.source_document_id == SourceDocument.id)
            .where(AssetSource.asset_id.in_(asset_ids))
        )
        result: dict[int, list[SourceDocument]] = {}
        for asset_id, source in rows:
            result.setdefault(asset_id, []).append(source)
        return result

    def _serialize(
        self,
        asset: Asset,
        similarity: float | None,
        source_rows: list[SourceDocument] | None = None,
    ) -> dict:
        if source_rows is None:
            source_rows = self._sources_by_asset([asset.id]).get(asset.id, [])
        return {
            "id": asset.id,
            "lib_type": asset.lib_type,
            "category": asset.category,
            "title": asset.title,
            "content": asset.content,
            "tags": json.loads(asset.tags_json),
            "source_type": asset.source_type,
            "credibility": asset.credibility,
            "origin": asset.origin,
            "manual_locked": asset.manual_locked,
            "decision_id": asset.decision_id,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
            "similarity": similarity,
            "sources": [{"title": row.title, "url": row.url, "publisher": row.publisher} for row in source_rows],
        }

"""第二阶段知识库体检的确定性分析。

本模块只读取第一阶段已经落库的事实（博主、资产、来源和向量），不调用
大模型，也不把后续阶段的产出/效果数据猜成零。返回值是 JSON 友好的快照，
供 ``AssessmentAgent`` 和历史比较服务使用。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, AssetEmbedding, AssetSource, Blogger, SourceDocument
from app.services.embedding_service import EmbeddingService


class LibraryAnalysisError(RuntimeError):
    """确定性库分析错误。``args[0]`` 始终是可供 API 映射的错误码。"""


class LibraryAnalysisService:
    """构建博主三库的可复现快照并计算库间语义关系。

    ``embedding`` 可注入测试向量器。生产环境复用第一阶段的本地 BGE，
    不在分析过程中重新加载或调用模型；数据库只按批量查询读取资产、来源和
    向量，避免逐资产的 N+1 查询。
    """

    LIB_TYPES = ("knowledge", "material", "algorithm")
    DEFAULT_DIRECTIONS = ("景区", "美食", "非遗")
    _HASH_EXCLUDED_KEYS = {"created_at", "snapshot_hash"}

    def __init__(self, db: Session, embedding: EmbeddingService | None = None) -> None:
        self.db = db
        self.embedding = embedding or EmbeddingService()

    def build_snapshot(self, blogger_id: int) -> dict[str, Any]:
        """读取当前博主的三库事实并返回带快照哈希的 JSON 对象。

        已删除博主和已软删除资产都不会出现在快照中。缺少博主时抛出
        ``BLOGGER_NOT_FOUND``，而不是返回一个看似合法的空库，防止跨博主
        或误用 ID 时生成报告。
        """

        blogger = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if blogger is None:
            raise LibraryAnalysisError("BLOGGER_NOT_FOUND")

        assets = list(
            self.db.scalars(
                select(Asset).where(Asset.blogger_id == blogger_id, Asset.deleted_at.is_(None)).order_by(Asset.id)
            )
        )
        asset_ids = [asset.id for asset in assets]
        source_rows = self._load_sources(asset_ids)
        embedding_rows = self._load_embeddings(asset_ids)
        serialized_assets = [self._serialize_asset(asset, source_rows, embedding_rows) for asset in assets]
        libraries = {
            lib_type: self._library_summary(
                lib_type, [item for item in serialized_assets if item["lib_type"] == lib_type]
            )
            for lib_type in self.LIB_TYPES
        }

        relations = self._semantic_relations(serialized_assets, embedding_rows)
        direction_coverage = self._direction_coverage(blogger, serialized_assets)
        weak_assets = self._weak_assets(serialized_assets)
        core_assets = self._core_assets(serialized_assets, relations)
        weak_categories = self._weak_categories(serialized_assets, direction_coverage)
        source_coverage = self._source_coverage(serialized_assets)
        snapshot: dict[str, Any] = {
            "blogger_id": blogger_id,
            "blogger": self._serialize_blogger(blogger),
            "libraries": libraries,
            "counts": {
                "knowledge": libraries["knowledge"]["count"],
                "material": libraries["material"]["count"],
                "algorithm": libraries["algorithm"]["count"],
                "total": len(serialized_assets),
            },
            "assets": serialized_assets,
            "sources": self._serialize_sources(source_rows),
            "category_distribution": {
                lib_type: libraries[lib_type]["category_distribution"] for lib_type in self.LIB_TYPES
            },
            "tag_distribution": {lib_type: libraries[lib_type]["tag_distribution"] for lib_type in self.LIB_TYPES},
            "credibility_distribution": {
                lib_type: libraries[lib_type]["credibility_distribution"] for lib_type in self.LIB_TYPES
            },
            "source_coverage": source_coverage,
            "low_credibility_assets": [item for item in weak_assets if item["reason"] == "low_credibility"],
            "no_source_assets": [item for item in weak_assets if item["reason"] == "no_source"],
            "orphan_assets": [item for item in weak_assets if item["reason"] == "orphan"],
            "weak_assets": weak_assets,
            "profile_direction_coverage": direction_coverage,
            "relations": relations,
            "cross_library_relations": self._relation_summary(relations),
            "core_assets": core_assets,
            "weak_categories": weak_categories,
            "feature_readiness": self._feature_readiness(libraries, source_coverage, direction_coverage),
            "missing_items": self._missing_items(libraries, source_coverage, direction_coverage),
            "suggestions": self._suggestions(weak_categories, direction_coverage, source_coverage),
            "future_data": {"output": "暂无数据", "effect": "暂无数据"},
        }
        snapshot["snapshot_hash"] = self.calculate_snapshot_hash(snapshot)
        return snapshot

    def analyze(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """基于已构建快照重新生成确定性分析字段。

        该入口不访问数据库，适合在 Agent 调用前冻结输入，也可用于测试和
        历史重放。传入的 snapshot 会被复制，不会被原地修改。
        """

        # 分析只替换顶层结果字段，不会修改资产/关系等嵌套对象；浅复制即可保持
        # 输入不可变，同时避免对 1000 条资产及其向量做一次完整 JSON 往返。
        normalized = dict(snapshot)
        complete_keys = {
            "assets",
            "libraries",
            "relations",
            "cross_library_relations",
            "core_assets",
            "weak_categories",
            "source_coverage",
            "profile_direction_coverage",
            "feature_readiness",
            "missing_items",
        }
        # build_snapshot 已经完成全部确定性计算。编排流程随后调用 analyze
        # 是为了冻结/重放输入；完整快照无需再次进行 O(n²) 跨库矩阵计算。
        if complete_keys.issubset(normalized):
            expected_future_data = {"output": "暂无数据", "effect": "暂无数据"}
            future_data_changed = normalized.get("future_data") != expected_future_data
            if future_data_changed:
                normalized["future_data"] = expected_future_data
            # build_snapshot 已按同一份完整快照生成 hash。只要输入仍带有
            # future_data，重放不会改变参与哈希的字段，因此直接复用可避免
            # 对大快照重复递归序列化；无 hash 或异常 future_data 时仍重算。
            if future_data_changed or not normalized.get("snapshot_hash"):
                normalized["snapshot_hash"] = self.calculate_snapshot_hash(normalized)
            return normalized
        assets = [item for item in normalized.get("assets", []) if isinstance(item, Mapping)]
        libraries = {
            lib_type: self._library_summary(lib_type, [item for item in assets if item.get("lib_type") == lib_type])
            for lib_type in self.LIB_TYPES
        }
        embedding_rows: dict[int, np.ndarray] = {}
        for item in assets:
            raw_vector = item.get("embedding")
            if isinstance(raw_vector, Sequence) and not isinstance(raw_vector, (str, bytes, bytearray)):
                try:
                    embedding_rows[int(item["id"])] = np.asarray(raw_vector, dtype=np.float32)
                except (TypeError, ValueError):
                    continue
        relations = self._semantic_relations(assets, embedding_rows)
        coverage = normalized.get("profile_direction_coverage") or self._direction_coverage_from_snapshot(
            normalized, assets
        )
        source_coverage = self._source_coverage(assets)
        normalized.update(
            {
                "libraries": libraries,
                "counts": {
                    **{lib_type: libraries[lib_type]["count"] for lib_type in self.LIB_TYPES},
                    "total": len(assets),
                },
                "source_coverage": source_coverage,
                "profile_direction_coverage": coverage,
                "relations": relations,
                "cross_library_relations": self._relation_summary(relations),
                "core_assets": self._core_assets(assets, relations),
                "weak_categories": self._weak_categories(assets, coverage),
                "future_data": {"output": "暂无数据", "effect": "暂无数据"},
            }
        )
        normalized["snapshot_hash"] = self.calculate_snapshot_hash(normalized)
        return normalized

    @classmethod
    def calculate_snapshot_hash(cls, snapshot: Mapping[str, Any]) -> str:
        """按稳定 JSON 计算 SHA-256；忽略时间戳和已有 hash 字段。"""

        canonical = cls._canonicalize(snapshot)
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _load_sources(self, asset_ids: list[int]) -> dict[int, list[SourceDocument]]:
        if not asset_ids:
            return {}
        rows = self.db.execute(
            select(AssetSource.asset_id, SourceDocument)
            .join(SourceDocument, AssetSource.source_document_id == SourceDocument.id)
            .where(AssetSource.asset_id.in_(asset_ids))
        )
        result: dict[int, list[SourceDocument]] = defaultdict(list)
        for asset_id, source in rows:
            result[int(asset_id)].append(source)
        return dict(result)

    def _load_embeddings(self, asset_ids: list[int]) -> dict[int, np.ndarray]:
        if not asset_ids:
            return {}
        result: dict[int, np.ndarray] = {}
        for row in self.db.scalars(select(AssetEmbedding).where(AssetEmbedding.asset_id.in_(asset_ids))):
            try:
                vector = np.frombuffer(row.vector, dtype=np.float32)
                if vector.size and vector.size == int(row.dimension):
                    result[int(row.asset_id)] = vector.copy()
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _serialize_blogger(blogger: Blogger) -> dict[str, Any]:
        def parse_list(value: str | None) -> list[str]:
            try:
                loaded = json.loads(value or "[]")
                return [str(item) for item in loaded] if isinstance(loaded, list) else []
            except (TypeError, json.JSONDecodeError):
                return []

        return {
            "id": blogger.id,
            "name": blogger.name,
            "platform": blogger.platform,
            "content_types": parse_list(blogger.content_types_json),
            "style": blogger.style,
            "follower_band": blogger.follower_band,
            "monetization_types": parse_list(blogger.monetization_types_json),
            "routes": blogger.routes,
            "viral_topic": blogger.viral_topic,
            "frequency": blogger.frequency,
            "suit_type": blogger.suit_type,
            "knowledge_focus": getattr(blogger, "knowledge_focus", None),
        }

    @staticmethod
    def _serialize_sources(source_rows: Mapping[int, Sequence[SourceDocument]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        result: list[dict[str, Any]] = []
        for rows in source_rows.values():
            for row in rows:
                if row.id in seen:
                    continue
                seen.add(row.id)
                result.append(
                    {
                        "id": row.id,
                        "title": row.title,
                        "url": row.url,
                        "publisher": row.publisher,
                        "source_type": row.source_type,
                        "verified_at": row.verified_at,
                    }
                )
        return sorted(result, key=lambda item: int(item["id"]))

    @staticmethod
    def _serialize_asset(
        asset: Asset,
        source_rows: Mapping[int, Sequence[SourceDocument]],
        embedding_rows: Mapping[int, np.ndarray],
    ) -> dict[str, Any]:
        try:
            tags = json.loads(asset.tags_json or "[]")
            tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
        except (TypeError, json.JSONDecodeError):
            tags = []
        sources = [
            {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "publisher": source.publisher,
                "source_type": source.source_type,
            }
            for source in source_rows.get(asset.id, [])
        ]
        vector = embedding_rows.get(asset.id)
        return {
            "id": asset.id,
            "blogger_id": asset.blogger_id,
            "lib_type": asset.lib_type,
            "category": asset.category,
            "title": asset.title,
            "content": asset.content,
            "tags": tags,
            "source_type": asset.source_type,
            "credibility": asset.credibility,
            "origin": asset.origin,
            "manual_locked": asset.manual_locked,
            "decision_id": asset.decision_id,
            "effect": getattr(asset, "effect", None),
            "effect_weight": getattr(asset, "effect_weight", None),
            "sources": sources,
            "source_document_ids": [int(str(source["id"])) for source in sources],
            "embedding_dimension": int(vector.size) if vector is not None else None,
            # 原始向量只在本地内存中参与批量关系计算。快照和 API 仅保留摘要，
            # 防止数百维数值进入数据库、Agent 上下文或外部模型请求。
            "embedding_hash": (
                hashlib.sha256(vector.astype(np.float32, copy=False).tobytes()).hexdigest()
                if vector is not None
                else None
            ),
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
        }

    @staticmethod
    def _library_summary(lib_type: str, assets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        category_distribution = Counter(str(item.get("category") or "未分类") for item in assets)
        tag_distribution: Counter[str] = Counter()
        credibility_distribution: Counter[str] = Counter()
        source_count = 0
        for item in assets:
            tag_distribution.update(str(tag) for tag in item.get("tags", []) if str(tag).strip())
            credibility_distribution[str(item.get("credibility"))] += 1
            if item.get("sources") or item.get("source_document_ids"):
                source_count += 1
        return {
            "lib_type": lib_type,
            "count": len(assets),
            "category_distribution": dict(sorted(category_distribution.items())),
            "tag_distribution": dict(sorted(tag_distribution.items())),
            "credibility_distribution": dict(sorted(credibility_distribution.items())),
            "source_coverage": {
                "with_source": source_count,
                "without_source": len(assets) - source_count,
                "ratio": round(source_count / len(assets), 6) if assets else 0.0,
            },
        }

    @staticmethod
    def _source_coverage(assets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        by_lib: dict[str, dict[str, Any]] = {}
        total_with = 0
        for lib_type in LibraryAnalysisService.LIB_TYPES:
            rows = [item for item in assets if item.get("lib_type") == lib_type]
            summary = LibraryAnalysisService._library_summary(lib_type, rows)["source_coverage"]
            by_lib[lib_type] = summary
            total_with += int(summary["with_source"])
        return {
            "by_library": by_lib,
            "with_source": total_with,
            "without_source": len(assets) - total_with,
            "ratio": round(total_with / len(assets), 6) if assets else 0.0,
        }

    @staticmethod
    def _weak_assets(assets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in assets:
            credibility = item.get("credibility")
            if isinstance(credibility, (int, float)) and credibility < 4:
                result.append(
                    {
                        "asset_id": item.get("id"),
                        "title": item.get("title"),
                        "lib_type": item.get("lib_type"),
                        "reason": "low_credibility",
                    }
                )
            if not item.get("sources") and not item.get("source_document_ids"):
                result.append(
                    {
                        "asset_id": item.get("id"),
                        "title": item.get("title"),
                        "lib_type": item.get("lib_type"),
                        "reason": "no_source",
                    }
                )
            if item.get("embedding_dimension") is None:
                result.append(
                    {
                        "asset_id": item.get("id"),
                        "title": item.get("title"),
                        "lib_type": item.get("lib_type"),
                        "reason": "orphan",
                    }
                )
        return result

    @staticmethod
    def _semantic_relations(
        assets: Sequence[Mapping[str, Any]], embedding_rows: Mapping[int, np.ndarray]
    ) -> list[dict[str, Any]]:
        """用 NumPy 批量计算每组跨库关系，每个源资产保留每个目标库 top-3。"""

        relation_rows: list[dict[str, Any]] = []
        for left_type in LibraryAnalysisService.LIB_TYPES:
            left = [
                item
                for item in assets
                if item.get("lib_type") == left_type and int(item.get("id", 0)) in embedding_rows
            ]
            if not left:
                continue
            for right_type in LibraryAnalysisService.LIB_TYPES:
                if left_type == right_type:
                    continue
                right = [
                    item
                    for item in assets
                    if item.get("lib_type") == right_type and int(item.get("id", 0)) in embedding_rows
                ]
                if not right:
                    continue
                left_ids = [int(item["id"]) for item in left]
                right_ids = [int(item["id"]) for item in right]
                # 不同向量模型/测试替身的维度可能不同；只有可比较维度进入矩阵。
                dimensions = [embedding_rows[item_id].size for item_id in left_ids + right_ids]
                dimension = Counter(dimensions).most_common(1)[0][0]
                left_valid = [
                    index for index, item_id in enumerate(left_ids) if embedding_rows[item_id].size == dimension
                ]
                right_valid = [
                    index for index, item_id in enumerate(right_ids) if embedding_rows[item_id].size == dimension
                ]
                if not left_valid or not right_valid:
                    continue
                left_matrix = np.stack([embedding_rows[left_ids[index]] for index in left_valid]).astype(np.float32)
                right_matrix = np.stack([embedding_rows[right_ids[index]] for index in right_valid]).astype(np.float32)
                left_norm = np.linalg.norm(left_matrix, axis=1, keepdims=True)
                right_norm = np.linalg.norm(right_matrix, axis=1, keepdims=True)
                left_matrix = left_matrix / np.where(left_norm == 0, 1.0, left_norm)
                right_matrix = right_matrix / np.where(right_norm == 0, 1.0, right_norm)
                similarities = left_matrix @ right_matrix.T
                for left_index, source_index in enumerate(left_valid):
                    order = np.argsort(-similarities[left_index], kind="stable")[:3]
                    for target_index in order:
                        relation_rows.append(
                            {
                                "from_asset_id": left_ids[source_index],
                                "from_lib_type": left_type,
                                "to_asset_id": right_ids[right_valid[int(target_index)]],
                                "to_lib_type": right_type,
                                "similarity": round(float(similarities[left_index, target_index]), 6),
                                "relation_type": "semantic",
                            }
                        )
        relation_rows.sort(
            key=lambda row: (
                row["from_lib_type"],
                row["to_lib_type"],
                row["from_asset_id"],
                -row["similarity"],
                row["to_asset_id"],
            )
        )
        return relation_rows

    @staticmethod
    def _relation_summary(relations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        pairs: dict[str, dict[str, Any]] = {}
        for left in LibraryAnalysisService.LIB_TYPES:
            for right in LibraryAnalysisService.LIB_TYPES:
                if left == right:
                    continue
                rows = [
                    row for row in relations if row.get("from_lib_type") == left and row.get("to_lib_type") == right
                ]
                key = f"{left}_to_{right}"
                pairs[key] = {
                    "count": len(rows),
                    "max_similarity": max((float(row["similarity"]) for row in rows), default=None),
                    "avg_similarity": round(float(np.mean([float(row["similarity"]) for row in rows])), 6)
                    if rows
                    else None,
                }
        return {"pairs": pairs, "covered_pairs": [key for key, value in pairs.items() if value["count"] > 0]}

    @classmethod
    def _direction_coverage(cls, blogger: Blogger, assets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        try:
            profile_types = json.loads(blogger.content_types_json or "[]")
            profile_types = [str(item).strip() for item in profile_types if str(item).strip()]
        except (TypeError, json.JSONDecodeError):
            profile_types = []
        return cls._direction_coverage_from_values(profile_types, assets)

    @classmethod
    def _direction_coverage_from_snapshot(
        cls, snapshot: Mapping[str, Any], assets: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        blogger = snapshot.get("blogger")
        profile_types = blogger.get("content_types", []) if isinstance(blogger, Mapping) else []
        return cls._direction_coverage_from_values([str(item) for item in profile_types], assets)

    @classmethod
    def _direction_coverage_from_values(
        cls, profile_types: Sequence[str], assets: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        requested = list(dict.fromkeys([*cls.DEFAULT_DIRECTIONS, *profile_types]))
        aliases = {
            "自然": "景区",
            "旅游": "景区",
            "旅行": "景区",
            "传统文化": "非遗",
            "文化": "非遗",
        }
        rows: dict[str, dict[str, Any]] = {}
        for direction in requested:
            normalized = aliases.get(direction, direction)
            matching = [
                item
                for item in assets
                if normalized in str(item.get("category") or "")
                or any(normalized in str(tag) for tag in item.get("tags", []))
            ]
            per_library = {
                lib_type: sum(1 for item in matching if item.get("lib_type") == lib_type) for lib_type in cls.LIB_TYPES
            }
            rows[direction] = {
                "requested": direction in profile_types,
                "normalized": normalized,
                "count": len(matching),
                "by_library": per_library,
                "covered": bool(matching),
            }
        missing = [direction for direction, value in rows.items() if not value["covered"]]
        return {
            "requested": list(profile_types),
            "directions": rows,
            "covered": [key for key, value in rows.items() if value["covered"]],
            "missing": missing,
        }

    @staticmethod
    def _core_assets(
        assets: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        relation_scores: dict[int, float] = defaultdict(float)
        for relation in relations:
            relation_scores[int(relation["from_asset_id"])] += max(0.0, float(relation["similarity"]))
            relation_scores[int(relation["to_asset_id"])] += max(0.0, float(relation["similarity"]))
        ranked = sorted(
            assets,
            key=lambda item: (
                -(
                    float(item.get("credibility") or 0) / 5
                    + (1.0 if item.get("sources") else 0.0)
                    + relation_scores[int(item.get("id", 0))] / 10
                ),
                str(item.get("lib_type")),
                int(item.get("id", 0)),
            ),
        )
        return [
            {
                "asset_id": item.get("id"),
                "lib_type": item.get("lib_type"),
                "title": item.get("title"),
                "reason": "可信度、来源和跨库关系综合排序",
            }
            for item in ranked[: min(10, len(ranked))]
        ]

    @staticmethod
    def _weak_categories(assets: Sequence[Mapping[str, Any]], coverage: Mapping[str, Any]) -> list[dict[str, Any]]:
        categories: Counter[str] = Counter(str(item.get("category") or "未分类") for item in assets)
        result = [
            {"category": category, "count": count, "reason": "资产数量少"}
            for category, count in sorted(categories.items())
            if count <= 1
        ]
        for direction in coverage.get("missing", []):
            result.append({"category": direction, "count": 0, "reason": "画像方向缺少对应资产"})
        return result

    @staticmethod
    def _feature_readiness(
        libraries: Mapping[str, Mapping[str, Any]],
        source_coverage: Mapping[str, Any],
        direction_coverage: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        missing = LibraryAnalysisService._missing_items(libraries, source_coverage, direction_coverage)
        has_three = all(
            int(libraries.get(lib_type, {}).get("count", 0)) > 0 for lib_type in LibraryAnalysisService.LIB_TYPES
        )
        return {
            "script_generation": {"ready": has_three, "missing_items": [] if has_three else missing},
            "route_recommendation": {
                "ready": has_three and bool(source_coverage.get("with_source")),
                "missing_items": [] if has_three and source_coverage.get("with_source") else missing,
            },
            "publishing": {"ready": False, "missing_items": ["暂无平台发布数据与授权"]},
            "feedback_learning": {"ready": False, "missing_items": ["暂无发布反馈数据"]},
        }

    @staticmethod
    def _missing_items(
        libraries: Mapping[str, Mapping[str, Any]],
        source_coverage: Mapping[str, Any],
        direction_coverage: Mapping[str, Any],
    ) -> list[str]:
        missing: list[str] = []
        labels = {"knowledge": "知识", "material": "素材", "algorithm": "算法"}
        for lib_type in LibraryAnalysisService.LIB_TYPES:
            if int(libraries.get(lib_type, {}).get("count", 0)) == 0:
                missing.append(f"缺少{labels[lib_type]}库资产")
        if int(source_coverage.get("without_source", 0)) > 0:
            missing.append(f"有{source_coverage['without_source']}条资产缺少可信来源")
        for direction in direction_coverage.get("missing", []):
            missing.append(f"画像方向缺少{direction}资产")
        return missing

    @staticmethod
    def _suggestions(
        weak_categories: Sequence[Mapping[str, Any]],
        direction_coverage: Mapping[str, Any],
        source_coverage: Mapping[str, Any],
    ) -> list[str]:
        suggestions: list[str] = []
        if direction_coverage.get("missing"):
            suggestions.append("优先补齐画像方向对应的知识资产")
        if source_coverage.get("without_source"):
            suggestions.append("为无来源资产补充可核验的来源文档")
        if weak_categories:
            suggestions.append("扩充薄弱分类并为每类保留至少一条可信核心资产")
        return suggestions

    @classmethod
    def _canonicalize(cls, value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {
                str(item_key): cls._canonicalize(item_value, str(item_key))
                for item_key, item_value in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(item_key) not in cls._HASH_EXCLUDED_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [cls._canonicalize(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value


__all__ = ["LibraryAnalysisError", "LibraryAnalysisService"]

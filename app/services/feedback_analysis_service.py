"""第四阶段反馈的确定性预分析与冻结快照构建。"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Asset,
    AssetPlace,
    AssetSource,
    Blogger,
    MemoryRecord,
    Metric,
    Output,
    OutputAsset,
    OutputPlace,
    Place,
    TaskSession,
)

MIN_HISTORICAL_SAMPLES = 2
COMMERCIAL_FIELDS = {"est_benefit", "est_cost", "like_level", "fits_koc", "fits_shoot"}
SNAPSHOT_HASH_FIELDS = (
    "blogger_id",
    "profile",
    "output",
    "primary_metric",
    "metric_history",
    "assets",
    "output_assets",
    "places",
    "output_places",
    "asset_places",
    "user_confirmed_place_updates",
)


class FeedbackAnalysisError(ValueError):
    """确定性预分析的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _serial(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=_serial,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _normalize_name(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value.lower(), flags=re.UNICODE)


class FeedbackAnalysisService:
    """只读业务表，生成 Agent 不可越权扩展的冻结事实快照。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_snapshot(
        self,
        blogger_id: int,
        output_id: int,
        primary_metric_id: int,
        *,
        task_id: str | None = None,
        user_instruction: str = "",
        user_confirmed_place_updates: Mapping[str | int, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        blogger = self.session.scalar(
            select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None))
        )
        if blogger is None:
            raise FeedbackAnalysisError("BLOGGER_NOT_FOUND", "博主不存在")
        output = self.session.scalar(
            select(Output).where(
                Output.id == output_id,
                Output.blogger_id == blogger_id,
                Output.deleted_at.is_(None),
            )
        )
        if output is None:
            raise FeedbackAnalysisError("OUTPUT_NOT_FOUND", "产出不存在")
        if output.status != "succeeded":
            raise FeedbackAnalysisError("FEEDBACK_INVALID_OUTPUT", "只有有效成功产出可以分析反馈")
        metric = self.session.scalar(
            select(Metric).where(Metric.id == primary_metric_id, Metric.output_id == output_id)
        )
        if metric is None:
            raise FeedbackAnalysisError("METRIC_NOT_FOUND", "Metric 不存在或不属于当前 Output")
        self._validate_metric(metric)

        assets, output_assets = self._load_assets(blogger_id, output_id)
        places, output_places, asset_places = self._resolve_places(
            blogger_id, output, assets
        )
        updates = self._normalize_place_updates(user_confirmed_place_updates, places)
        history_pairs = self._history(blogger_id, metric.source_type)
        metric_history = [self._metric_snapshot(row, related_output) for row, related_output in history_pairs]
        deterministic = self._deterministic_analysis(metric, output, history_pairs, places)
        task = self._task_snapshot(task_id or output.task_id, blogger_id)
        memories = self._active_memories(blogger_id)

        snapshot: dict[str, Any] = {
            "blogger_id": blogger_id,
            "profile": self._profile_snapshot(blogger),
            "output": self._output_snapshot(output),
            "primary_metric": self._metric_snapshot(metric, output),
            "metric_history": metric_history,
            "assets": assets,
            "output_assets": output_assets,
            "places": places,
            "output_places": output_places,
            "asset_places": asset_places,
            "task_memory": task,
            "active_memories": memories,
            "deterministic_analysis": deterministic,
            "evidence_whitelist": self._evidence(
                output, metric, assets, output_assets, places, output_places
            ),
            "user_instruction": user_instruction.strip(),
            "user_confirmed_place_updates": updates,
        }
        snapshot["snapshot_hash"] = self.hash_snapshot(snapshot)
        return snapshot

    analyze = build_snapshot

    @staticmethod
    def hash_snapshot(snapshot: Mapping[str, Any]) -> str:
        """供 confirm 前复核；忽略旧 hash 字段后重新计算。"""

        # 任务消息、checkpoint、候选记忆和用户指令会在编排过程中变化，
        # 但不改变反馈业务事实；把它们纳入 hash 会制造必然的伪冲突。
        value = {key: snapshot.get(key) for key in SNAPSHOT_HASH_FIELDS}
        return _canonical_hash(value)

    @staticmethod
    def _validate_metric(metric: Metric) -> None:
        if metric.source_type not in {"manual", "simulated"}:
            raise FeedbackAnalysisError("FEEDBACK_INVALID_METRIC", "Metric 来源必须是 manual 或 simulated")
        for field in ("views", "likes", "comments", "collects", "shares"):
            value = getattr(metric, field, 0)
            if _number(value) is None:
                raise FeedbackAnalysisError("FEEDBACK_INVALID_METRIC", f"Metric.{field} 必须非负")
        revenue = getattr(metric, "actual_revenue", None)
        cost = getattr(metric, "actual_cost", None)
        confirmed = bool(getattr(metric, "user_confirmed", False))
        if (revenue is not None or cost is not None) and (
            metric.source_type != "manual" or not confirmed
        ):
            raise FeedbackAnalysisError(
                "FEEDBACK_INVALID_METRIC", "实际商业值仅允许用户确认的 manual Metric"
            )
        for field, value in (("actual_revenue", revenue), ("actual_cost", cost)):
            if value is not None and _number(value) is None:
                raise FeedbackAnalysisError("FEEDBACK_INVALID_METRIC", f"Metric.{field} 必须非负")

    def _load_assets(
        self, blogger_id: int, output_id: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pairs = self.session.execute(
            select(OutputAsset, Asset)
            .join(Asset, Asset.id == OutputAsset.asset_id)
            .where(
                OutputAsset.output_id == output_id,
                Asset.blogger_id == blogger_id,
                Asset.deleted_at.is_(None),
            )
            .order_by(OutputAsset.id)
        ).all()
        asset_ids = [asset.id for _, asset in pairs]
        source_map: dict[int, list[int]] = defaultdict(list)
        if asset_ids:
            source_rows = self.session.execute(
                select(AssetSource.asset_id, AssetSource.source_document_id).where(
                    AssetSource.asset_id.in_(asset_ids)
                )
            ).all()
            for asset_id, source_id in source_rows:
                source_map[int(asset_id)].append(int(source_id))
        assets: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for relation, asset in pairs:
            source_ids = sorted(set(source_map.get(asset.id, [])))
            if asset.credibility < 3 and not source_ids:
                continue
            assets.append(self._asset_snapshot(asset, source_ids))
            relations.append(
                {
                    "id": relation.id,
                    "output_id": relation.output_id,
                    "asset_id": relation.asset_id,
                    "usage_type": relation.usage_type,
                    "claim": relation.claim,
                }
            )
        return assets, relations

    def _resolve_places(
        self, blogger_id: int, output: Output, assets: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        explicit_pairs = self.session.execute(
            select(OutputPlace, Place)
            .join(Place, Place.id == OutputPlace.place_id)
            .where(
                OutputPlace.output_id == output.id,
                Place.blogger_id == blogger_id,
                Place.deleted_at.is_(None),
            )
            .order_by(OutputPlace.sequence, OutputPlace.id)
        ).all()
        output_places = [
            {
                "id": relation.id,
                "output_id": relation.output_id,
                "place_id": relation.place_id,
                "role": relation.role,
                "sequence": relation.sequence,
                "claim": relation.claim,
            }
            for relation, _ in explicit_pairs
        ]
        if explicit_pairs:
            places = [
                self._place_snapshot(place, "output_place", "high")
                for _, place in explicit_pairs
            ]
            return self._dedupe_places(places), output_places, []

        asset_ids = [int(item["id"]) for item in assets]
        asset_pairs = (
            self.session.execute(
                select(AssetPlace, Place)
                .join(Place, Place.id == AssetPlace.place_id)
                .where(
                    AssetPlace.asset_id.in_(asset_ids),
                    Place.blogger_id == blogger_id,
                    Place.deleted_at.is_(None),
                )
                .order_by(AssetPlace.id)
            ).all()
            if asset_ids
            else []
        )
        asset_places = [
            {
                "id": relation.id,
                "asset_id": relation.asset_id,
                "place_id": relation.place_id,
                "relation_type": relation.relation_type,
                "source_type": relation.source_type,
            }
            for relation, _ in asset_pairs
        ]
        if asset_pairs:
            places = [
                self._place_snapshot(place, "asset_place", "high") for _, place in asset_pairs
            ]
            return self._dedupe_places(places), [], asset_places

        content = f"{output.title}\n{output.content_json}"
        normalized_content = _normalize_name(content)
        all_places = self.session.scalars(
            select(Place)
            .where(Place.blogger_id == blogger_id, Place.deleted_at.is_(None))
            .order_by(Place.id)
        ).all()
        matched = [
            self._place_snapshot(place, "controlled_name_match", "low")
            for place in all_places
            if len(_normalize_name(place.name)) >= 2
            and _normalize_name(place.name) in normalized_content
        ]
        return self._dedupe_places(matched), [], []

    def _history(self, blogger_id: int, source_type: str) -> list[tuple[Metric, Output]]:
        rows = self.session.execute(
            select(Metric, Output)
            .join(Output, Output.id == Metric.output_id)
            .where(
                Output.blogger_id == blogger_id,
                Output.deleted_at.is_(None),
                Output.status == "succeeded",
                Metric.source_type == source_type,
            )
            .order_by(Metric.collected_at, Metric.id)
        ).all()
        result: list[tuple[Metric, Output]] = []
        for metric, output in rows:
            try:
                self._validate_metric(metric)
            except FeedbackAnalysisError:
                continue
            if source_type == "manual" and not bool(getattr(metric, "user_confirmed", False)):
                continue
            result.append((metric, output))
        return result

    def _deterministic_analysis(
        self,
        primary: Metric,
        output: Output,
        history: list[tuple[Metric, Output]],
        places: list[dict[str, Any]],
    ) -> dict[str, Any]:
        engagement = self._engagement(primary)
        previous_same_category = [
            metric
            for metric, related in history
            if related.category == output.category and metric.id != primary.id
        ]
        comparison = self._historical_comparison(primary, previous_same_category)
        category_performance = self._category_performance(history)
        business = self._business(primary, places)
        enough = len(previous_same_category) >= MIN_HISTORICAL_SAMPLES
        overall = "ok" if enough and engagement["status"] == "ok" else "data_insufficient"
        return {
            "overall_status": overall,
            "sample_quality": {
                "status": "ok" if enough else "data_insufficient",
                "historical_sample_count": len(previous_same_category),
                "minimum_required": MIN_HISTORICAL_SAMPLES,
                "reason": (
                    "同类历史样本足以进行相对中位数比较"
                    if enough
                    else "首次或同类历史样本不足，不能凭单条绝对播放量分类"
                ),
            },
            "engagement": engagement,
            "completion_availability": self._completion(primary),
            "historical_comparison": comparison,
            "category_performance": category_performance,
            "business": business,
        }

    @staticmethod
    def _engagement(metric: Metric) -> dict[str, Any]:
        views = int(metric.views)
        interactions = int(metric.likes) + int(metric.comments) + int(metric.collects) + int(
            getattr(metric, "shares", 0)
        )
        if views <= 0:
            return {
                "status": "data_insufficient",
                "rate": None,
                "reason": "views 为 0，互动率分母无效",
            }
        return {
            "status": "ok",
            "rate": interactions / views,
            "formula_key": "engagement_over_views",
        }

    @staticmethod
    def _completion(metric: Metric) -> dict[str, Any]:
        value = getattr(metric, "completion_rate", None)
        if value is None:
            return {
                "status": "data_insufficient",
                "value": None,
                "reason": "当前 Metric 未采集完播字段",
            }
        number = _number(value)
        if number is None:
            return {"status": "data_insufficient", "value": None, "reason": "完播字段无效"}
        return {"status": "ok", "value": number}

    def _historical_comparison(
        self, primary: Metric, history: list[Metric]
    ) -> dict[str, Any]:
        if len(history) < MIN_HISTORICAL_SAMPLES:
            return {
                "status": "data_insufficient",
                "views_median": None,
                "engagement_rate_median": None,
                "trend": "unknown",
            }
        views_median = float(statistics.median(row.views for row in history))
        rates = [
            rate
            for row in history
            if (rate := self._engagement(row).get("rate")) is not None
        ]
        rate_median = float(statistics.median(rates)) if rates else None
        trend = "flat"
        if primary.views > views_median:
            trend = "up"
        elif primary.views < views_median:
            trend = "down"
        return {
            "status": "ok",
            "views_median": views_median,
            "engagement_rate_median": rate_median,
            "trend": trend,
        }

    def _category_performance(
        self, history: list[tuple[Metric, Output]]
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[Metric]] = defaultdict(list)
        for metric, output in history:
            grouped[output.category].append(metric)
        result: list[dict[str, Any]] = []
        for category in sorted(grouped):
            metrics = grouped[category]
            status = "ok" if len(metrics) >= MIN_HISTORICAL_SAMPLES else "data_insufficient"
            rates = [
                rate
                for metric in metrics
                if (rate := self._engagement(metric).get("rate")) is not None
            ]
            trend = "unknown"
            if len(metrics) >= 2:
                trend = "up" if metrics[-1].views > metrics[0].views else "down"
                if metrics[-1].views == metrics[0].views:
                    trend = "flat"
            result.append(
                {
                    "category": category,
                    "status": status,
                    "sample_count": len(metrics),
                    "views_median": float(statistics.median(row.views for row in metrics)),
                    "engagement_rate_median": float(statistics.median(rates)) if rates else None,
                    "trend": trend,
                }
            )
        return result

    @staticmethod
    def _business(primary: Metric, places: list[dict[str, Any]]) -> dict[str, Any]:
        revenue = getattr(primary, "actual_revenue", None)
        cost = getattr(primary, "actual_cost", None)
        confirmed_actual = (
            primary.source_type == "manual"
            and bool(getattr(primary, "user_confirmed", False))
            and revenue is not None
            and cost is not None
        )
        actual_revenue = float(revenue) if revenue is not None else None
        actual_cost = float(cost) if cost is not None else None
        actual_net = (
            actual_revenue - actual_cost
            if confirmed_actual and actual_revenue is not None and actual_cost is not None
            else None
        )
        return {
            "status": "actual" if confirmed_actual else "data_insufficient",
            "actual_revenue": actual_revenue if confirmed_actual else None,
            "actual_cost": actual_cost if confirmed_actual else None,
            "actual_net": actual_net,
            "place_count": len(places),
            "place_commercial_complete_count": sum(
                item.get("est_benefit") is not None and item.get("est_cost") is not None
                for item in places
            ),
            "reason": (
                "净收益由用户确认的 actual_revenue - actual_cost 确定性计算"
                if confirmed_actual
                else "缺少用户确认的实际收入或成本，不能判断实际赚钱或亏损"
            ),
        }

    @staticmethod
    def _profile_snapshot(blogger: Blogger) -> dict[str, Any]:
        return {
            "id": blogger.id,
            "name": blogger.name,
            "platform": blogger.platform,
            "content_types": _json(blogger.content_types_json, []),
            "style": blogger.style,
            "follower_band": blogger.follower_band,
            "monetization_types": _json(blogger.monetization_types_json, []),
            "routes": blogger.routes,
            "viral_topic": blogger.viral_topic,
            "frequency": blogger.frequency,
            "suit_type": blogger.suit_type,
            "knowledge_focus": getattr(blogger, "knowledge_focus", None),
            "updated_at": _serial(blogger.updated_at),
        }

    @staticmethod
    def _output_snapshot(output: Output) -> dict[str, Any]:
        return {
            "id": output.id,
            "blogger_id": output.blogger_id,
            "task_id": output.task_id,
            "type": output.type,
            "category": output.category,
            "title": output.title,
            "content": _json(output.content_json, {}),
            "status": output.status,
            "version": output.version,
            "manual_locked": output.manual_locked,
            "updated_at": _serial(output.updated_at),
            "deleted_at": _serial(output.deleted_at),
        }

    @staticmethod
    def _metric_snapshot(metric: Metric, output: Output) -> dict[str, Any]:
        return {
            "id": metric.id,
            "output_id": metric.output_id,
            "blogger_id": output.blogger_id,
            "category": output.category,
            "source_type": metric.source_type,
            "user_confirmed": bool(getattr(metric, "user_confirmed", False)),
            "views": metric.views,
            "likes": metric.likes,
            "comments": metric.comments,
            "collects": metric.collects,
            "shares": getattr(metric, "shares", 0),
            "actual_revenue": getattr(metric, "actual_revenue", None),
            "actual_cost": getattr(metric, "actual_cost", None),
            "collected_at": _serial(metric.collected_at),
        }

    @staticmethod
    def _asset_snapshot(asset: Asset, source_ids: list[int]) -> dict[str, Any]:
        return {
            "id": asset.id,
            "blogger_id": asset.blogger_id,
            "lib_type": asset.lib_type,
            "category": asset.category,
            "title": asset.title,
            "content": asset.content,
            "tags": _json(asset.tags_json, []),
            "source_type": asset.source_type,
            "credibility": asset.credibility,
            "source_document_ids": source_ids,
            "origin": asset.origin,
            "manual_locked": asset.manual_locked,
            "effect": getattr(asset, "effect", None),
            "effect_weight": getattr(asset, "effect_weight", None),
            "updated_at": _serial(asset.updated_at),
            "deleted_at": _serial(asset.deleted_at),
        }

    @staticmethod
    def _place_snapshot(place: Place, source: str, confidence: str) -> dict[str, Any]:
        return {
            "id": place.id,
            "blogger_id": place.blogger_id,
            "name": place.name,
            "category": place.category,
            "location": place.location,
            "source_type": place.source_type,
            "credibility": place.credibility,
            "like_level": place.like_level,
            "est_cost": place.est_cost,
            "est_benefit": place.est_benefit,
            "fits_koc": place.fits_koc,
            "fits_shoot": place.fits_shoot,
            "association_source": source,
            "association_confidence": confidence,
            "updated_at": _serial(place.updated_at),
            "deleted_at": _serial(place.deleted_at),
        }

    @staticmethod
    def _dedupe_places(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for item in values:
            result.setdefault(int(item["id"]), item)
        return list(result.values())

    def _task_snapshot(self, task_id: str | None, blogger_id: int) -> dict[str, Any]:
        if not task_id:
            return {}
        task = self.session.scalar(
            select(TaskSession).where(
                TaskSession.id == task_id, TaskSession.blogger_id == blogger_id
            )
        )
        if task is None:
            return {}
        return {
            "id": task.id,
            "blogger_id": task.blogger_id,
            "task_type": task.task_type,
            "title": task.title,
            "status": task.status,
            "current_context": task.current_context,
            "recovery_state": _json(task.recovery_state_json, {}),
            "updated_at": _serial(task.updated_at),
        }

    def _active_memories(self, blogger_id: int) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(MemoryRecord)
            .where(MemoryRecord.blogger_id == blogger_id, MemoryRecord.status == "active")
            .order_by(MemoryRecord.id)
        ).all()
        return [
            {
                "id": row.id,
                "blogger_id": row.blogger_id,
                "memory_type": row.memory_type,
                "title": row.title,
                "content": row.content,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "confidence": row.confidence,
                "status": row.status,
                "version": row.version,
                "updated_at": _serial(row.updated_at),
            }
            for row in rows
        ]

    @staticmethod
    def _normalize_place_updates(
        raw: Mapping[str | int, Mapping[str, Any]] | None,
        places: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if raw is None:
            return {}
        allowed_ids = {int(item["id"]) for item in places}
        result: dict[str, dict[str, Any]] = {}
        for raw_id, fields in raw.items():
            try:
                place_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise FeedbackAnalysisError(
                    "FEEDBACK_INVALID_PLACE_UPDATE", "用户确认地点 ID 非法"
                ) from exc
            if place_id not in allowed_ids:
                raise FeedbackAnalysisError(
                    "FEEDBACK_INVALID_PLACE_UPDATE", "用户确认商业更新不属于当前产出地点链路"
                )
            extra = set(fields) - COMMERCIAL_FIELDS
            if extra:
                raise FeedbackAnalysisError(
                    "FEEDBACK_INVALID_PLACE_UPDATE", f"地点商业更新含非白名单字段：{sorted(extra)}"
                )
            result[str(place_id)] = dict(fields)
        return result

    @staticmethod
    def _evidence(
        output: Output,
        metric: Metric,
        assets: list[dict[str, Any]],
        output_assets: list[dict[str, Any]],
        places: list[dict[str, Any]],
        output_places: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = [
            {
                "evidence_type": "output",
                "ref_id": output.id,
                "claim": "当前有效产出冻结快照",
                "snapshot": {"id": output.id, "version": output.version, "updated_at": _serial(output.updated_at)},
            },
            {
                "evidence_type": "metric",
                "ref_id": metric.id,
                "claim": "当前反馈主 Metric 冻结快照",
                "snapshot": {
                    "id": metric.id,
                    "source_type": metric.source_type,
                    "collected_at": _serial(metric.collected_at),
                },
            },
        ]
        values.extend(
            {
                "evidence_type": "asset",
                "ref_id": item["id"],
                "claim": "当前产出引用的可信未删除资产",
                "snapshot": item,
            }
            for item in assets
        )
        values.extend(
            {
                "evidence_type": "output_asset",
                "ref_id": item["id"],
                "claim": "Output 与资产的冻结关系",
                "snapshot": item,
            }
            for item in output_assets
        )
        values.extend(
            {
                "evidence_type": "place",
                "ref_id": item["id"],
                "claim": "按固定优先级解析的地点候选",
                "snapshot": item,
            }
            for item in places
        )
        values.extend(
            {
                "evidence_type": "output_place",
                "ref_id": item["id"],
                "claim": "Output 与地点的显式冻结关系",
                "snapshot": item,
            }
            for item in output_places
        )
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for item in values:
            unique.setdefault((str(item["evidence_type"]), int(item["ref_id"])), item)
        return list(unique.values())


__all__ = [
    "FeedbackAnalysisError",
    "FeedbackAnalysisService",
    "MIN_HISTORICAL_SAMPLES",
    "SNAPSHOT_HASH_FIELDS",
]

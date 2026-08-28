"""从白名单业务表构建可重算的确定性经营报告快照。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Blogger, Metric, OperationalIndicator, Output, OutputPlace, Place
from app.services.commercial_data_policy import (
    place_commercial_provenance_map,
    trusted_estimate_places,
)
from app.services.indicator_service import IndicatorService


class ReportDataError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _round(value: float) -> float:
    return round(float(value), 8)


class ReportDataService:
    """Agent 之前唯一允许计算经营数字的服务。"""

    def __init__(self, db: Session, indicator_service: IndicatorService | None = None) -> None:
        self.db = db
        self.indicators = indicator_service or IndicatorService(db)

    def build_snapshot(self, blogger_id: int, *, observed_at: datetime | None = None) -> dict[str, Any]:
        at = observed_at or datetime.utcnow()
        self._active_blogger(blogger_id)
        start = at - timedelta(days=30)
        metrics = self._metrics(blogger_id, start, at)
        outputs = self._outputs(blogger_id, start, at)
        places = self._places(blogger_id)
        indicators = list(
            self.db.scalars(
                select(OperationalIndicator)
                .where(
                    OperationalIndicator.blogger_id == blogger_id,
                    OperationalIndicator.active.is_(True),
                )
                .order_by(OperationalIndicator.category, OperationalIndicator.id)
            )
        )
        indicator_facts = []
        for indicator in indicators:
            result = self.indicators.evaluate(indicator, at)
            indicator_facts.append(
                {
                    "indicator_id": indicator.id,
                    "name": indicator.name,
                    "category": indicator.category,
                    "formula_key": indicator.formula_key,
                    "unit": indicator.unit,
                    "direction": indicator.direction,
                    "target_value": indicator.target_value,
                    "value": result.value,
                    "status": result.status,
                    "evidence": result.evidence,
                }
            )

        manual_metrics = [row for row in metrics if row.source_type == "manual"]
        simulated_metrics = [row for row in metrics if row.source_type == "simulated"]
        facts = {
            "money": self._money_fact(metrics, places),
            "traffic": self._traffic_fact(manual_metrics, simulated_metrics),
            "product": self._product_fact(outputs),
            "supplier": self._supplier_fact(blogger_id, metrics),
        }
        charts = {
            "traffic_line": self._traffic_chart(manual_metrics, simulated_metrics),
            "money_bar": self._money_chart(facts["money"]),
            "product_category_bar": self._product_chart(outputs),
            "supplier_top_bar": self._supplier_chart(facts["supplier"]),
        }
        if manual_metrics and simulated_metrics:
            charts["traffic_simulation_preview"] = self._simulation_traffic_chart(simulated_metrics)
        data_quality = self._data_quality(metrics, outputs, places, facts)
        evidence = self._evidence(metrics, outputs, places, facts, indicator_facts)
        evidence_whitelist = sorted({row["ref"] for row in evidence})
        payload: dict[str, Any] = {
            "blogger_id": blogger_id,
            "window": {"kind": "rolling_30d", "start": start.isoformat(), "end": at.isoformat()},
            "facts": facts,
            "charts": charts,
            "indicators": indicator_facts,
            "data_quality": data_quality,
            "evidence": evidence,
            "evidence_whitelist": evidence_whitelist,
            "place_names": [row.name for row in places],
            "indicator_names": [row.name for row in indicators],
            "feedback_runs": self._applied_feedback_ids(blogger_id),
        }
        hash_payload = dict(payload)
        # 滚动窗口的精确当前时刻不应让同一组业务行产生不同 hash。
        hash_payload["window"] = {"kind": "rolling_30d"}
        payload["snapshot_hash"] = hashlib.sha256(
            json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return payload

    build = build_snapshot

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise ReportDataError("BLOGGER_NOT_FOUND", "博主不存在或已删除")
        return row

    def _metrics(self, blogger_id: int, start: datetime, end: datetime) -> list[Metric]:
        return list(
            self.db.scalars(
                select(Metric)
                .join(Output, Output.id == Metric.output_id)
                .where(
                    Output.blogger_id == blogger_id,
                    Output.deleted_at.is_(None),
                    Output.status.in_(("succeeded", "draft")),
                    Metric.collected_at >= start,
                    Metric.collected_at <= end,
                )
                .order_by(Metric.collected_at, Metric.id)
            )
        )

    def _outputs(self, blogger_id: int, start: datetime, end: datetime) -> list[Output]:
        return list(
            self.db.scalars(
                select(Output)
                .where(
                    Output.blogger_id == blogger_id,
                    Output.deleted_at.is_(None),
                    Output.status.in_(("succeeded", "draft")),
                    Output.created_at >= start,
                    Output.created_at <= end,
                )
                .order_by(Output.created_at, Output.id)
            )
        )

    def _places(self, blogger_id: int) -> list[Place]:
        return list(
            self.db.scalars(
                select(Place).where(Place.blogger_id == blogger_id, Place.deleted_at.is_(None)).order_by(Place.id)
            )
        )

    @staticmethod
    def _confirmed(metrics: list[Metric]) -> list[Metric]:
        return [row for row in metrics if row.source_type == "manual" and row.user_confirmed]

    def _money_fact(self, metrics: list[Metric], places: list[Place]) -> dict[str, Any]:
        paired = [
            row for row in self._confirmed(metrics) if row.actual_revenue is not None and row.actual_cost is not None
        ]
        if paired:
            revenue = sum(row.actual_revenue for row in paired if row.actual_revenue is not None)
            cost = sum(row.actual_cost for row in paired if row.actual_cost is not None)
            net = revenue - cost
            return {
                "status": "actual",
                "conclusion": "profit" if net > 0 else "loss" if net < 0 else "break_even",
                "revenue": _round(revenue),
                "cost": _round(cost),
                "net": _round(net),
                "roi": _round(net / cost) if cost else None,
                "source_refs": [f"metric:{row.id}" for row in paired],
            }
        estimated = trusted_estimate_places(self.db, places)
        if estimated:
            revenue = sum(row.est_benefit for row in estimated if row.est_benefit is not None)
            cost = sum(row.est_cost for row in estimated if row.est_cost is not None)
            net = revenue - cost
            return {
                "status": "estimated",
                "conclusion": "estimated_profit"
                if net > 0
                else "estimated_loss"
                if net < 0
                else "estimated_break_even",
                "revenue": _round(revenue),
                "cost": _round(cost),
                "net": _round(net),
                "roi": _round(net / cost) if cost else None,
                "source_refs": [f"place:{row.id}" for row in estimated],
            }
        return {
            "status": "data_insufficient",
            "conclusion": "data_insufficient",
            "revenue": None,
            "cost": None,
            "net": None,
            "roi": None,
            "source_refs": [],
        }

    @classmethod
    def _traffic_fact(
        cls,
        manual_metrics: list[Metric],
        simulated_metrics: list[Metric],
    ) -> dict[str, Any]:
        if not manual_metrics and simulated_metrics:
            return {
                "status": "simulation_only",
                "views": None,
                "engagement_rate": None,
                "trend": "unknown",
                "source_refs": [],
                "simulation_preview": cls._traffic_values(simulated_metrics),
            }
        if not manual_metrics:
            return {
                "status": "data_insufficient",
                "views": None,
                "engagement_rate": None,
                "trend": "unknown",
                "source_refs": [],
                "simulation_preview": None,
            }
        actual = cls._traffic_values(manual_metrics)
        if len(manual_metrics) < 2:
            trend = "unknown"
        else:
            midpoint = len(manual_metrics) // 2
            old = sum(row.views for row in manual_metrics[:midpoint])
            new = sum(row.views for row in manual_metrics[midpoint:])
            trend = "up" if new > old else "down" if new < old else "flat"
        return {
            "status": "actual",
            "views": actual["views"],
            "engagement_rate": actual["engagement_rate"],
            "trend": trend,
            "source_refs": actual["source_refs"],
            "simulation_preview": cls._traffic_values(simulated_metrics) if simulated_metrics else None,
        }

    @staticmethod
    def _traffic_values(metrics: list[Metric]) -> dict[str, Any]:
        views = sum(row.views for row in metrics)
        engagement = sum(row.likes + row.comments + row.collects + row.shares for row in metrics)
        return {
            "views": float(views),
            "engagement_rate": _round(engagement / views) if views else None,
            "source_refs": [f"metric:{row.id}" for row in metrics],
        }

    @staticmethod
    def _product_fact(outputs: list[Output]) -> dict[str, Any]:
        distribution: dict[str, int] = defaultdict(int)
        for row in outputs:
            distribution[row.category] += 1
        return {
            "status": "actual",
            "output_count": len(outputs),
            "category_distribution": dict(sorted(distribution.items())),
            "source_refs": [f"output:{row.id}" for row in outputs],
        }

    def _supplier_fact(self, blogger_id: int, metrics: list[Metric]) -> dict[str, Any]:
        totals: dict[int, dict[str, Any]] = {}
        confirmed = self._confirmed(metrics)
        output_ids = {row.output_id for row in confirmed}
        places_by_output: dict[int, list[tuple[int, str]]] = defaultdict(list)
        if output_ids:
            for output_id, place_id, name in self.db.execute(
                select(OutputPlace.output_id, OutputPlace.place_id, Place.name)
                .join(Place, Place.id == OutputPlace.place_id)
                .where(
                    OutputPlace.output_id.in_(output_ids),
                    Place.blogger_id == blogger_id,
                    Place.deleted_at.is_(None),
                )
            ):
                places_by_output[output_id].append((place_id, name))
        for metric in confirmed:
            if metric.actual_revenue is None or metric.actual_cost is None:
                continue
            refs = places_by_output.get(metric.output_id, [])
            if len(refs) != 1:
                continue
            place_id, name = refs[0]
            item = totals.setdefault(
                place_id,
                {"place_id": place_id, "name": name, "net": 0.0, "source_refs": []},
            )
            item["net"] = _round(float(item["net"]) + metric.actual_revenue - metric.actual_cost)
            item["source_refs"].append(f"metric:{metric.id}")
        places = sorted(totals.values(), key=lambda row: (-float(row["net"]), int(row["place_id"])))
        return {
            "status": "actual" if places else "data_insufficient",
            "places": places,
            "source_refs": sorted({ref for row in places for ref in row["source_refs"]}),
        }

    @classmethod
    def _traffic_chart(
        cls,
        manual_metrics: list[Metric],
        simulated_metrics: list[Metric],
    ) -> dict[str, Any]:
        if manual_metrics:
            metrics = manual_metrics
            status = "actual"
            title = "流量趋势"
        elif simulated_metrics:
            metrics = simulated_metrics
            status = "simulation_only"
            title = "模拟流量预览（不计入实际流量）"
        else:
            metrics = []
            status = "data_insufficient"
            title = "流量趋势"
        return {
            "type": "line",
            "title": title,
            "status": status,
            "points": [
                {
                    "label": row.collected_at.isoformat(),
                    "value": float(row.views),
                    "unit": "views",
                    "source_refs": [f"metric:{row.id}"],
                }
                for row in metrics
            ],
        }

    @classmethod
    def _simulation_traffic_chart(cls, metrics: list[Metric]) -> dict[str, Any]:
        chart = cls._traffic_chart([], metrics)
        chart["title"] = "模拟流量预览（不计入实际流量）"
        return chart

    @staticmethod
    def _money_chart(fact: dict[str, Any]) -> dict[str, Any]:
        refs = list(fact["source_refs"])
        return {
            "type": "bar",
            "title": "收益、成本与净收益",
            "status": fact["status"],
            "points": [
                {"label": label, "value": fact[key], "unit": "currency", "source_refs": refs}
                for label, key in (("收益", "revenue"), ("成本", "cost"), ("净收益", "net"))
            ],
        }

    @staticmethod
    def _product_chart(outputs: list[Output]) -> dict[str, Any]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for row in outputs:
            grouped[row.category].append(row.id)
        return {
            "type": "bar",
            "title": "内容分类分布",
            "status": "ok",
            "points": [
                {
                    "label": category,
                    "value": len(ids),
                    "unit": "count",
                    "source_refs": [f"output:{output_id}" for output_id in ids],
                }
                for category, ids in sorted(grouped.items())
            ],
        }

    @staticmethod
    def _supplier_chart(fact: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "bar",
            "title": "地点确认净收益Top",
            "status": fact["status"],
            "points": [
                {
                    "label": row["name"],
                    "value": row["net"],
                    "unit": "currency",
                    "source_refs": [*row["source_refs"], f"place:{row['place_id']}"],
                }
                for row in fact["places"]
            ],
        }

    def _data_quality(
        self,
        metrics: list[Metric],
        outputs: list[Output],
        places: list[Place],
        facts: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        confirmed = self._confirmed(metrics)
        paired = [row for row in confirmed if row.actual_revenue is not None and row.actual_cost is not None]
        simulated = [row for row in metrics if row.source_type == "simulated"]
        provenance_by_place = place_commercial_provenance_map(places, self.db)
        untrusted_places = [
            row.id
            for row in places
            if row.est_cost is not None
            and row.est_benefit is not None
            and not {"est_cost", "est_benefit"} <= set(provenance_by_place.get(row.id, {}))
        ]
        return {
            "metric_count": len(metrics),
            "manual_metric_count": sum(row.source_type == "manual" for row in metrics),
            "simulated_metric_count": sum(row.source_type == "simulated" for row in metrics),
            "simulated_excluded_from_actual_traffic": len(simulated),
            "simulated_excluded_metric_ids": [row.id for row in simulated],
            "simulation_preview_available": bool(simulated),
            "untrusted_commercial_place_ids": untrusted_places,
            "untrusted_commercial_place_count": len(untrusted_places),
            "user_confirmed_metric_count": len(confirmed),
            "commercial_pair_count": len(paired),
            "output_count": len(outputs),
            "money_status": facts["money"]["status"],
            "supplier_status": facts["supplier"]["status"],
            "missing": [
                category
                for category, value in facts.items()
                if value["status"] in {"data_insufficient", "simulation_only"}
            ],
            "traffic_status": facts["traffic"]["status"],
            "simulated_not_used_for_actual_money": True,
            "simulated_not_used_for_actual_traffic": True,
            "null_preserved": True,
        }

    @staticmethod
    def _evidence(
        metrics: list[Metric],
        outputs: list[Output],
        places: list[Place],
        facts: dict[str, dict[str, Any]],
        indicators: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        values.extend(
            {
                "ref": f"metric:{row.id}",
                "evidence_type": "metric",
                "ref_id": row.id,
                "claim": "报告使用的原始指标",
                "snapshot": {
                    "output_id": row.output_id,
                    "source_type": row.source_type,
                    "user_confirmed": row.user_confirmed,
                    "views": row.views,
                    "likes": row.likes,
                    "comments": row.comments,
                    "collects": row.collects,
                    "shares": row.shares,
                    "actual_revenue": row.actual_revenue,
                    "actual_cost": row.actual_cost,
                    "collected_at": row.collected_at,
                },
            }
            for row in metrics
        )
        values.extend(
            {
                "ref": f"output:{row.id}",
                "evidence_type": "output",
                "ref_id": row.id,
                "claim": "报告使用的有效产出",
                "snapshot": {"category": row.category, "status": row.status, "version": row.version},
            }
            for row in outputs
        )
        referenced_place_ids = {
            int(ref.split(":", 1)[1]) for ref in facts["money"].get("source_refs", []) if str(ref).startswith("place:")
        }
        referenced_place_ids.update(int(row["place_id"]) for row in facts["supplier"].get("places", []))
        referenced_places = [row for row in places if row.id in referenced_place_ids]
        provenance_by_place = place_commercial_provenance_map(referenced_places)
        values.extend(
            {
                "ref": f"place:{row.id}",
                "evidence_type": "place",
                "ref_id": row.id,
                "claim": "报告商业数据关联地点",
                "snapshot": {
                    "name": row.name,
                    "est_benefit": row.est_benefit,
                    "est_cost": row.est_cost,
                    "source_type": row.source_type,
                    "origin": row.origin,
                    "credibility": row.credibility,
                    "manual_locked": row.manual_locked,
                    "commercial_provenance": provenance_by_place.get(row.id, {}),
                },
            }
            for row in referenced_places
        )
        values.extend(
            {
                "ref": f"indicator:{row['indicator_id']}",
                "evidence_type": "indicator",
                "ref_id": row["indicator_id"],
                "claim": "白名单公式计算的经营指标",
                "snapshot": row,
            }
            for row in indicators
        )
        return values

    def _applied_feedback_ids(self, blogger_id: int) -> list[int]:
        # 避免对反馈服务形成硬依赖；模型存在时才读取已应用运行。
        try:
            from app.models import FeedbackRun
        except ImportError:
            return []
        return list(
            self.db.scalars(
                select(FeedbackRun.id)
                .where(FeedbackRun.blogger_id == blogger_id, FeedbackRun.status == "applied")
                .order_by(FeedbackRun.id)
            )
        )


__all__ = ["ReportDataError", "ReportDataService"]

"""经营指标定义与白名单公式执行服务。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Blogger, IndicatorObservation, Metric, OperationalIndicator, Output, OutputPlace, Place


class IndicatorServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class FormulaResult:
    value: float | None
    status: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class FormulaDefinition:
    category: str
    name: str
    meaning: str
    unit: str
    direction: str
    source_tables: tuple[str, ...]
    executor: str


DEFAULT_FORMULAS: dict[str, FormulaDefinition] = {
    "traffic_views": FormulaDefinition(
        "traffic",
        "总播放量",
        "近30天有效指标播放量之和",
        "views",
        "higher_better",
        ("metric", "output"),
        "_traffic_views",
    ),
    "traffic_engagement_rate": FormulaDefinition(
        "traffic",
        "互动率",
        "近30天互动总数除以播放量",
        "ratio",
        "higher_better",
        ("metric", "output"),
        "_traffic_engagement_rate",
    ),
    "traffic_views_trend": FormulaDefinition(
        "traffic",
        "播放趋势",
        "最近7天与此前7天播放量变化率",
        "ratio",
        "higher_better",
        ("metric", "output"),
        "_traffic_views_trend",
    ),
    "product_output_count": FormulaDefinition(
        "product",
        "内容产出数",
        "近30天未删除成功或草稿产出数",
        "count",
        "higher_better",
        ("output",),
        "_product_output_count",
    ),
    "product_category_distribution": FormulaDefinition(
        "product",
        "内容分类分布",
        "近30天产出按category的确定性分布",
        "count",
        "neutral",
        ("output",),
        "_product_category_distribution",
    ),
    "money_actual_revenue": FormulaDefinition(
        "money",
        "实际收入",
        "用户确认manual指标中的实际收入",
        "currency",
        "higher_better",
        ("metric", "output"),
        "_money_actual_revenue",
    ),
    "money_actual_cost": FormulaDefinition(
        "money",
        "实际成本",
        "用户确认manual指标中的实际成本",
        "currency",
        "lower_better",
        ("metric", "output"),
        "_money_actual_cost",
    ),
    "money_actual_net": FormulaDefinition(
        "money",
        "实际净收益",
        "用户确认manual指标实际收入减实际成本",
        "currency",
        "higher_better",
        ("metric", "output"),
        "_money_actual_net",
    ),
    "money_roi": FormulaDefinition(
        "money",
        "实际ROI",
        "用户确认manual指标实际净收益除以实际成本",
        "ratio",
        "higher_better",
        ("metric", "output"),
        "_money_roi",
    ),
    "supplier_confirmed_net": FormulaDefinition(
        "supplier",
        "供应商确认净收益",
        "单一显式地点产出的已确认实际净收益",
        "currency",
        "higher_better",
        ("metric", "output", "output_place", "place"),
        "_supplier_confirmed_net",
    ),
    "supplier_top_places": FormulaDefinition(
        "supplier",
        "供应商Top地点",
        "按已确认实际净收益排序的地点",
        "currency",
        "higher_better",
        ("metric", "output", "output_place", "place"),
        "_supplier_top_places",
    ),
}


class IndicatorService:
    """只执行注册函数；不解析自由公式、不执行 SQL 文本。"""

    formula_registry = DEFAULT_FORMULAS

    def __init__(self, db: Session) -> None:
        self.db = db

    def initialize_defaults(self, blogger_id: int) -> list[OperationalIndicator]:
        self._active_blogger(blogger_id)
        existing = {
            row.formula_key: row
            for row in self.db.scalars(
                select(OperationalIndicator).where(OperationalIndicator.blogger_id == blogger_id)
            )
        }
        rows: list[OperationalIndicator] = []
        for formula_key, definition in DEFAULT_FORMULAS.items():
            row = existing.get(formula_key)
            if row is None:
                row = OperationalIndicator(
                    blogger_id=blogger_id,
                    category=definition.category,
                    name=definition.name,
                    meaning=definition.meaning,
                    formula_key=formula_key,
                    source_tables_json=json.dumps(
                        {
                            "tables": definition.source_tables,
                            "window": "rolling_30d; trend uses current_7d vs previous_7d",
                            "filters": (
                                "current blogger, non-deleted valid outputs; "
                                "actual values require manual+user_confirmed"
                            ),
                            "null_semantics": "missing remains NULL and yields data_insufficient when required",
                        },
                        ensure_ascii=False,
                    ),
                    unit=definition.unit,
                    direction=definition.direction,
                    target_value=None,
                    active=True,
                    version=1,
                )
                self.db.add(row)
            rows.append(row)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows

    init_defaults = initialize_defaults

    def create_indicator(
        self, blogger_id: int, values: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> OperationalIndicator:
        self._active_blogger(blogger_id)
        data = {**dict(values or {}), **kwargs}
        formula_key = str(data.get("formula_key") or "").strip()
        definition = self._definition(formula_key)
        category = str(data.get("category") or definition.category)
        if category != definition.category:
            raise IndicatorServiceError("INDICATOR_CATEGORY_MISMATCH", "指标分类与白名单公式不一致")
        row = OperationalIndicator(
            blogger_id=blogger_id,
            category=category,
            name=str(data.get("name") or definition.name).strip(),
            meaning=str(data.get("meaning") or definition.meaning).strip(),
            formula_key=formula_key,
            source_tables_json=json.dumps(
                data.get("source_tables", {"tables": definition.source_tables}), ensure_ascii=False
            ),
            unit=str(data.get("unit") or definition.unit),
            direction=str(data.get("direction") or definition.direction),
            target_value=data.get("target_value"),
            active=bool(data.get("active", True)),
            version=1,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    create = create_indicator

    def get_indicator(self, blogger_id: int, indicator_id: int) -> OperationalIndicator:
        self._active_blogger(blogger_id)
        row = self.db.scalar(
            select(OperationalIndicator).where(
                OperationalIndicator.id == indicator_id,
                OperationalIndicator.blogger_id == blogger_id,
            )
        )
        if row is None:
            raise IndicatorServiceError("INDICATOR_NOT_FOUND", "指标不存在")
        return row

    get = get_indicator

    def list_indicators(self, blogger_id: int, *, active: bool | None = None) -> list[OperationalIndicator]:
        self._active_blogger(blogger_id)
        statement = select(OperationalIndicator).where(OperationalIndicator.blogger_id == blogger_id)
        if active is not None:
            statement = statement.where(OperationalIndicator.active.is_(active))
        return list(self.db.scalars(statement.order_by(OperationalIndicator.category, OperationalIndicator.id)))

    def update_indicator(
        self,
        blogger_id: int,
        indicator_id: int,
        values: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> OperationalIndicator:
        row = self.get_indicator(blogger_id, indicator_id)
        data = {**dict(values or {}), **kwargs}
        formula_key = str(data.get("formula_key", row.formula_key))
        definition = self._definition(formula_key)
        category = str(data.get("category", row.category))
        if category != definition.category:
            raise IndicatorServiceError("INDICATOR_CATEGORY_MISMATCH", "指标分类与白名单公式不一致")
        for field in ("name", "meaning", "unit", "direction", "target_value", "active"):
            if field in data:
                setattr(row, field, data[field])
        row.formula_key = formula_key
        row.category = category
        if "source_tables" in data:
            row.source_tables_json = json.dumps(data["source_tables"], ensure_ascii=False)
        row.version += 1
        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row

    update = update_indicator

    def deactivate_indicator(self, blogger_id: int, indicator_id: int) -> OperationalIndicator:
        return self.update_indicator(blogger_id, indicator_id, active=False)

    deactivate = deactivate_indicator
    delete = deactivate_indicator

    def recompute(
        self,
        blogger_id: int,
        *,
        indicator_id: int | None = None,
        feedback_run_id: int | None = None,
        report_id: int | None = None,
        idempotency_key: str | None = None,
        observed_at: datetime | None = None,
    ) -> list[IndicatorObservation]:
        self._active_blogger(blogger_id)
        statement = select(OperationalIndicator).where(
            OperationalIndicator.blogger_id == blogger_id,
            OperationalIndicator.active.is_(True),
        )
        if indicator_id is not None:
            statement = statement.where(OperationalIndicator.id == indicator_id)
        indicators = list(self.db.scalars(statement.order_by(OperationalIndicator.id)))
        if indicator_id is not None and not indicators:
            raise IndicatorServiceError("INDICATOR_NOT_FOUND", "指标不存在或已停用")
        at = observed_at or datetime.utcnow()
        rows: list[IndicatorObservation] = []
        for indicator in indicators:
            existing = self._idempotent_observation(
                indicator.id,
                feedback_run_id,
                report_id,
                idempotency_key,
            )
            if existing is not None:
                rows.append(existing)
                continue
            result = self.evaluate(indicator, at)
            trend = self._trend(indicator.id, result)
            evidence = dict(result.evidence)
            if idempotency_key is not None:
                evidence["idempotency_key"] = idempotency_key
            row = IndicatorObservation(
                indicator_id=indicator.id,
                feedback_run_id=feedback_run_id,
                report_id=report_id,
                value=result.value,
                status=result.status,
                trend=trend,
                evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str),
                observed_at=at,
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows

    def evaluate(self, indicator: OperationalIndicator, observed_at: datetime | None = None) -> FormulaResult:
        definition = self._definition(indicator.formula_key)
        executor: Callable[[int, datetime], FormulaResult] = getattr(self, definition.executor)
        return executor(indicator.blogger_id, observed_at or datetime.utcnow())

    def observation_trend(self, indicator_id: int, result: FormulaResult) -> str:
        """根据最近一次不可变观察计算新观察趋势。"""

        return self._trend(indicator_id, result)

    def get_history(
        self,
        blogger_id: int,
        indicator_id: int,
        *,
        limit: int = 100,
    ) -> list[IndicatorObservation]:
        indicator = self.get_indicator(blogger_id, indicator_id)
        return list(
            self.db.scalars(
                select(IndicatorObservation)
                .where(IndicatorObservation.indicator_id == indicator.id)
                .order_by(IndicatorObservation.observed_at.desc(), IndicatorObservation.id.desc())
                .limit(max(1, min(limit, 1000)))
            )
        )

    history = get_history

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise IndicatorServiceError("BLOGGER_NOT_FOUND", "博主不存在或已删除")
        return row

    @staticmethod
    def _definition(formula_key: str) -> FormulaDefinition:
        try:
            return DEFAULT_FORMULAS[formula_key]
        except KeyError as exc:
            raise IndicatorServiceError("INDICATOR_FORMULA_NOT_ALLOWED", "formula_key 不在受控注册表中") from exc

    def _metric_rows(self, blogger_id: int, start: datetime, end: datetime) -> list[Metric]:
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

    def _output_rows(self, blogger_id: int, start: datetime, end: datetime) -> list[Output]:
        return list(
            self.db.scalars(
                select(Output).where(
                    Output.blogger_id == blogger_id,
                    Output.deleted_at.is_(None),
                    Output.status.in_(("succeeded", "draft")),
                    Output.created_at >= start,
                    Output.created_at <= end,
                )
            )
        )

    @staticmethod
    def _metric_evidence(rows: list[Metric]) -> dict[str, Any]:
        return {"metric_ids": [row.id for row in rows], "output_ids": sorted({row.output_id for row in rows})}

    def _real_traffic_rows(
        self,
        blogger_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[list[Metric], list[Metric]]:
        rows = self._metric_rows(blogger_id, start, end)
        return (
            [row for row in rows if row.source_type == "manual"],
            [row for row in rows if row.source_type == "simulated"],
        )

    def _real_traffic_evidence(
        self,
        manual: list[Metric],
        simulated: list[Metric],
    ) -> dict[str, Any]:
        return {
            **self._metric_evidence(manual),
            "source_scope": "manual_only",
            "simulated_available": bool(simulated),
            "simulated_excluded": len(simulated),
            "simulated_excluded_ids": [row.id for row in simulated],
        }

    def _traffic_views(self, blogger_id: int, at: datetime) -> FormulaResult:
        manual, simulated = self._real_traffic_rows(blogger_id, at - timedelta(days=30), at)
        evidence = self._real_traffic_evidence(manual, simulated)
        if not manual:
            return FormulaResult(None, "data_insufficient", {**evidence, "reason": "近30天无manual流量Metric"})
        return FormulaResult(float(sum(row.views for row in manual)), "ok", evidence)

    def _traffic_engagement_rate(self, blogger_id: int, at: datetime) -> FormulaResult:
        manual, simulated = self._real_traffic_rows(blogger_id, at - timedelta(days=30), at)
        evidence = self._real_traffic_evidence(manual, simulated)
        views = sum(row.views for row in manual)
        if not manual or views == 0:
            return FormulaResult(
                None, "data_insufficient", {**evidence, "reason": "无manual样本或分母为0"}
            )
        interactions = sum((row.likes + row.comments + row.collects + row.shares for row in manual), start=0)
        return FormulaResult(round(interactions / views, 8), "ok", evidence)

    def _traffic_views_trend(self, blogger_id: int, at: datetime) -> FormulaResult:
        previous, previous_simulated = self._real_traffic_rows(
            blogger_id, at - timedelta(days=14), at - timedelta(days=7)
        )
        current, current_simulated = self._real_traffic_rows(blogger_id, at - timedelta(days=7), at)
        old = sum(row.views for row in previous)
        new = sum(row.views for row in current)
        evidence = self._real_traffic_evidence(
            previous + current,
            previous_simulated + current_simulated,
        )
        evidence.update({"previous_7d_views": old, "current_7d_views": new})
        if not previous or not current or old == 0:
            evidence["reason"] = "两窗口样本不足或前窗分母为0"
            return FormulaResult(None, "data_insufficient", evidence)
        return FormulaResult(round((new - old) / old, 8), "ok", evidence)

    def _product_output_count(self, blogger_id: int, at: datetime) -> FormulaResult:
        rows = self._output_rows(blogger_id, at - timedelta(days=30), at)
        return FormulaResult(float(len(rows)), "ok", {"output_ids": [row.id for row in rows]})

    def _product_category_distribution(self, blogger_id: int, at: datetime) -> FormulaResult:
        rows = self._output_rows(blogger_id, at - timedelta(days=30), at)
        distribution: dict[str, int] = defaultdict(int)
        for row in rows:
            distribution[row.category] += 1
        return FormulaResult(
            float(len(rows)),
            "ok",
            {"output_ids": [row.id for row in rows], "distribution": dict(sorted(distribution.items()))},
        )

    def _confirmed_metrics(self, blogger_id: int, at: datetime) -> list[Metric]:
        return [
            row
            for row in self._metric_rows(blogger_id, at - timedelta(days=30), at)
            if row.source_type == "manual" and row.user_confirmed
        ]

    def _money(self, blogger_id: int, at: datetime, kind: str) -> FormulaResult:
        rows = self._confirmed_metrics(blogger_id, at)
        evidence = self._metric_evidence(rows)
        if kind == "revenue":
            values = [row.actual_revenue for row in rows if row.actual_revenue is not None]
            if not values:
                return FormulaResult(None, "data_insufficient", {**evidence, "reason": "无用户确认实际收入"})
            return FormulaResult(round(sum(values), 4), "ok", evidence)
        if kind == "cost":
            values = [row.actual_cost for row in rows if row.actual_cost is not None]
            if not values:
                return FormulaResult(None, "data_insufficient", {**evidence, "reason": "无用户确认实际成本"})
            return FormulaResult(round(sum(values), 4), "ok", evidence)
        paired = [row for row in rows if row.actual_revenue is not None and row.actual_cost is not None]
        if not paired:
            return FormulaResult(None, "data_insufficient", {**evidence, "reason": "收入成本未成对确认"})
        revenue = sum(row.actual_revenue for row in paired if row.actual_revenue is not None)
        cost = sum(row.actual_cost for row in paired if row.actual_cost is not None)
        evidence.update({"paired_metric_ids": [row.id for row in paired]})
        if kind == "net":
            return FormulaResult(round(revenue - cost, 4), "ok", evidence)
        if cost == 0:
            return FormulaResult(None, "data_insufficient", {**evidence, "reason": "ROI分母为0"})
        return FormulaResult(round((revenue - cost) / cost, 8), "ok", evidence)

    def _money_actual_revenue(self, blogger_id: int, at: datetime) -> FormulaResult:
        return self._money(blogger_id, at, "revenue")

    def _money_actual_cost(self, blogger_id: int, at: datetime) -> FormulaResult:
        return self._money(blogger_id, at, "cost")

    def _money_actual_net(self, blogger_id: int, at: datetime) -> FormulaResult:
        return self._money(blogger_id, at, "net")

    def _money_roi(self, blogger_id: int, at: datetime) -> FormulaResult:
        return self._money(blogger_id, at, "roi")

    def _supplier_rows(self, blogger_id: int, at: datetime) -> list[dict[str, Any]]:
        metrics = self._confirmed_metrics(blogger_id, at)
        totals: dict[int, dict[str, Any]] = {}
        output_ids = {row.output_id for row in metrics}
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
        for metric in metrics:
            if metric.actual_revenue is None or metric.actual_cost is None:
                continue
            refs = places_by_output.get(metric.output_id, [])
            # 多地点产出无法无损分摊商业值，因此不制造地点收益。
            if len(refs) != 1:
                continue
            place_id, name = refs[0]
            item = totals.setdefault(place_id, {"place_id": place_id, "name": name, "net": 0.0, "metric_ids": []})
            item["net"] = round(float(item["net"]) + metric.actual_revenue - metric.actual_cost, 4)
            item["metric_ids"].append(metric.id)
        return sorted(totals.values(), key=lambda row: (-float(row["net"]), int(row["place_id"])))

    def _supplier_confirmed_net(self, blogger_id: int, at: datetime) -> FormulaResult:
        rows = self._supplier_rows(blogger_id, at)
        if not rows:
            return FormulaResult(None, "data_insufficient", {"reason": "无可唯一归属地点的确认商业数据"})
        return FormulaResult(round(sum(float(row["net"]) for row in rows), 4), "ok", {"places": rows})

    def _supplier_top_places(self, blogger_id: int, at: datetime) -> FormulaResult:
        rows = self._supplier_rows(blogger_id, at)
        if not rows:
            return FormulaResult(None, "data_insufficient", {"reason": "无可排名的确认地点数据", "places": []})
        return FormulaResult(float(len(rows)), "ok", {"places": rows})

    def _idempotent_observation(
        self,
        indicator_id: int,
        feedback_run_id: int | None,
        report_id: int | None,
        idempotency_key: str | None,
    ) -> IndicatorObservation | None:
        statement = select(IndicatorObservation).where(IndicatorObservation.indicator_id == indicator_id)
        if report_id is not None:
            statement = statement.where(IndicatorObservation.report_id == report_id)
            return self.db.scalar(statement)
        if feedback_run_id is not None:
            statement = statement.where(IndicatorObservation.feedback_run_id == feedback_run_id)
            return self.db.scalar(statement)
        if idempotency_key is None:
            return None
        candidates = self.db.scalars(
            statement.where(
                IndicatorObservation.report_id.is_(None),
                IndicatorObservation.feedback_run_id.is_(None),
            )
        )
        for row in candidates:
            try:
                evidence = json.loads(row.evidence_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(evidence, Mapping) and evidence.get("idempotency_key") == idempotency_key:
                return row
        return None

    def _trend(self, indicator_id: int, result: FormulaResult) -> str:
        if result.status != "ok" or result.value is None:
            return "unknown"
        previous = self.db.scalar(
            select(IndicatorObservation)
            .where(
                IndicatorObservation.indicator_id == indicator_id,
                IndicatorObservation.status == "ok",
                IndicatorObservation.value.is_not(None),
            )
            .order_by(IndicatorObservation.observed_at.desc(), IndicatorObservation.id.desc())
        )
        if previous is None or previous.value is None:
            return "unknown"
        if result.value > previous.value:
            return "up"
        if result.value < previous.value:
            return "down"
        return "flat"


__all__ = [
    "DEFAULT_FORMULAS",
    "FormulaDefinition",
    "FormulaResult",
    "IndicatorService",
    "IndicatorServiceError",
]

"""体检 Agent 输出的后端校验、证据约束和综合评分。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


class AssessmentValidationError(ValueError):
    """体检报告不满足后端安全规则。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class AssessmentValidationService:
    """不信任 Agent 的结构化输出，所有评分和引用在后端重新确认。"""

    _INDICATOR_ALIASES = {
        "evaluation_reason": "reason",
        "scoring_reason": "reason",
        "score_reason": "reason",
        "weight_justification": "weight_reason",
        "evidence_refs": "evidence",
        "evidence_references": "evidence",
    }
    _REQUIRED_INDICATOR_FIELDS = (
        "name",
        "meaning",
        "score_logic",
        "business_meaning",
        "weight",
        "weight_reason",
        "score",
        "reason",
        "evidence",
    )
    _RELATION_WORDS = (
        "关系",
        "关联",
        "跨库",
        "协同",
        "三库",
        "relation",
        "library",
    )

    def __init__(self, snapshot: Mapping[str, Any] | None = None) -> None:
        self.snapshot = snapshot

    def validate_and_normalize(
        self,
        report: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """验证报告并归一化权重、证据引用和后端综合分。

        ``report`` 是 Agent 的 JSON 对象，``snapshot`` 是本轮冻结的库快照。
        为适配早期调用方，也接受把两个参数反过来传入，并依据 ``assets``/
        ``indicators`` 字段自动识别。
        """

        report, snapshot = self._resolve_report_snapshot(report, snapshot)
        if not isinstance(report, Mapping):
            raise AssessmentValidationError("AGENT_INVALID_JSON", "体检输出必须是对象")
        resolved_snapshot = self._require_snapshot(snapshot)
        raw_indicators = report.get("indicators", report.get("metrics"))
        if not isinstance(raw_indicators, Sequence) or isinstance(raw_indicators, (str, bytes, bytearray)):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "indicators 必须是数组")
        if len(raw_indicators) < 3:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "体检至少需要三个指标")

        normalized_indicators: list[dict[str, Any]] = []
        for ordinal, raw_indicator in enumerate(raw_indicators, start=1):
            indicator = self._normalize_indicator(raw_indicator, ordinal)
            normalized_indicators.append(indicator)

        self._validate_relation_coverage(normalized_indicators, resolved_snapshot)
        evidence_refs: list[dict[str, Any]] = []
        total_weight = sum(float(indicator["original_weight"]) for indicator in normalized_indicators)
        if total_weight <= 0 or not math.isfinite(total_weight):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标权重总和无效")
        for indicator in normalized_indicators:
            normalized_evidence = self.validate_evidence_refs(indicator["evidence"], resolved_snapshot)
            indicator["evidence"] = normalized_evidence
            indicator["weight"] = round(float(indicator["original_weight"]) / total_weight * 100, 6)
            indicator["weight_ratio"] = round(float(indicator["weight"]) / 100, 8)
            evidence_refs.extend(normalized_evidence)
        self._validate_feature_readiness(report, resolved_snapshot)
        overall_score = self.calculate_overall_score(normalized_indicators)

        normalized = dict(report)
        normalized["indicators"] = normalized_indicators
        normalized.pop("metrics", None)
        normalized["overall_score"] = overall_score
        normalized["evidence_refs"] = evidence_refs
        normalized["validation"] = {
            "valid": True,
            "indicator_count": len(normalized_indicators),
            "evidence_count": len(evidence_refs),
            "overall_score_recomputed": overall_score,
        }
        # 后续阶段还没有真实发布/效果数据；即使 Agent 尝试填数，也不能把
        # 这些字段当成本阶段的事实结果。
        if isinstance(normalized.get("library_analysis"), Mapping):
            library_analysis = dict(normalized["library_analysis"])
            library_analysis.setdefault("future_data", {"output": "暂无数据", "effect": "暂无数据"})
            normalized["library_analysis"] = library_analysis
        return normalized

    def calculate_overall_score(self, indicators: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> float:
        """按指标权重重新计算 0-100 综合分，不采信模型的 overall_score。"""

        if isinstance(indicators, Mapping):
            indicators = indicators.get("indicators", indicators.get("metrics", []))
        if not isinstance(indicators, Sequence) or isinstance(indicators, (str, bytes, bytearray)):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标必须是数组")
        if len(indicators) < 1:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标不能为空")
        weights: list[float] = []
        scores: list[float] = []
        for indicator in indicators:
            if not isinstance(indicator, Mapping):
                raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标必须是对象")
            weight = self._finite_number(indicator.get("weight"), "指标权重")
            score = self._finite_number(indicator.get("score"), "指标分数")
            if weight <= 0:
                raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标权重必须为正")
            if not 0 <= score <= 100:
                raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标分数必须在 0-100")
            weights.append(weight)
            scores.append(score)
        total_weight = sum(weights)
        if total_weight <= 0 or not math.isfinite(total_weight):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标权重总和无效")
        return round(sum(score * weight for score, weight in zip(scores, weights, strict=True)) / total_weight, 6)

    def validate_evidence_refs(
        self,
        evidence_refs: Any,
        snapshot: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """校验证据引用确实存在于当前快照，拒绝跨博主和虚假 ID。"""

        if isinstance(evidence_refs, Mapping) and "indicators" in evidence_refs:
            flattened: list[Any] = []
            for indicator in evidence_refs.get("indicators", []):
                if isinstance(indicator, Mapping):
                    flattened.extend(self._raw_evidence(indicator))
            evidence_refs = flattened
        if evidence_refs is None:
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "指标必须提供证据")
        if isinstance(evidence_refs, (str, bytes, bytearray, int)):
            evidence_refs = [evidence_refs]
        if not isinstance(evidence_refs, Iterable) or isinstance(evidence_refs, Mapping):
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "证据必须是数组")
        resolved_snapshot = self._require_snapshot(snapshot)
        assets = self._snapshot_assets(resolved_snapshot)
        asset_by_id = {
            self._as_int(item.get("id")): item for item in assets if self._as_int(item.get("id")) is not None
        }
        source_ids = self._snapshot_source_ids(resolved_snapshot, assets)
        relation_pairs = self._snapshot_relation_pairs(resolved_snapshot)
        normalized: list[dict[str, Any]] = []
        for raw in evidence_refs:
            item = self._normalize_evidence(raw)
            evidence_type = item["evidence_type"]
            supplied_asset_id = self._as_int(item.get("asset_id"))
            supplied_source_id = self._as_int(item.get("source_document_id", item.get("source_id")))
            if supplied_asset_id is not None and supplied_asset_id not in asset_by_id:
                raise AssessmentValidationError(
                    "EVIDENCE_REFERENCE_INVALID", f"资产证据不存在: {item.get('asset_id')}"
                )
            if supplied_source_id is not None and supplied_source_id not in source_ids:
                raise AssessmentValidationError(
                    "EVIDENCE_REFERENCE_INVALID", f"来源证据不存在: {supplied_source_id}"
                )
            if supplied_asset_id is not None and supplied_source_id is not None:
                linked_sources = self._asset_source_ids(asset_by_id[supplied_asset_id])
                if supplied_source_id not in linked_sources:
                    raise AssessmentValidationError(
                        "EVIDENCE_REFERENCE_INVALID",
                        f"资产 {supplied_asset_id} 未关联来源 {supplied_source_id}",
                    )
            if evidence_type == "asset":
                asset_id = supplied_asset_id
                if asset_id is None or asset_id not in asset_by_id:
                    raise AssessmentValidationError(
                        "EVIDENCE_REFERENCE_INVALID", f"资产证据不存在: {item.get('asset_id')}"
                    )
                item["asset_id"] = asset_id
                item.setdefault("claim", str(asset_by_id[asset_id].get("title") or ""))
            elif evidence_type in {"source", "source_document"}:
                source_id = supplied_source_id
                if source_id is None or source_id not in source_ids:
                    raise AssessmentValidationError(
                        "EVIDENCE_REFERENCE_INVALID", f"来源证据不存在: {item.get('source_id')}"
                    )
                item["evidence_type"] = "source_document"
                item["source_document_id"] = source_id
            elif evidence_type == "relation":
                from_id = self._as_int(item.get("from_asset_id"))
                to_id = self._as_int(item.get("to_asset_id"))
                if from_id is None or to_id is None or (from_id, to_id) not in relation_pairs:
                    raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "跨库关系证据不存在")
                if from_id not in asset_by_id or to_id not in asset_by_id:
                    raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "关系证据资产不属于当前快照")
                item["from_asset_id"] = from_id
                item["to_asset_id"] = to_id
            elif evidence_type == "profile":
                blogger = resolved_snapshot.get("blogger")
                if not isinstance(blogger, Mapping) and resolved_snapshot.get("blogger_id") is None:
                    raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "画像证据缺少当前博主")
            else:
                raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", f"不支持的证据类型: {evidence_type}")
            normalized.append(item)
        if not normalized:
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "指标至少需要一条证据")
        return normalized

    @classmethod
    def _asset_source_ids(cls, asset: Mapping[str, Any]) -> set[int]:
        result: set[int] = set()
        values = asset.get("source_document_ids", [])
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            result.update(value for value in (cls._as_int(item) for item in values) if value is not None)
        sources = asset.get("sources", [])
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
            for source in sources:
                if isinstance(source, Mapping):
                    source_id = cls._as_int(source.get("id", source.get("source_document_id")))
                    if source_id is not None:
                        result.add(source_id)
        return result

    def _normalize_indicator(self, raw: Any, ordinal: int) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"第 {ordinal} 个指标必须是对象")
        indicator = dict(raw)
        for source, target in self._INDICATOR_ALIASES.items():
            if target not in indicator and source in indicator:
                indicator[target] = indicator[source]
        missing = [field for field in self._REQUIRED_INDICATOR_FIELDS if field not in indicator]
        if missing:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"指标缺少字段: {','.join(missing)}")
        for field in self._REQUIRED_INDICATOR_FIELDS:
            if field in {"weight", "score", "evidence"}:
                continue
            value = indicator.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"指标字段不能为空: {field}")
            indicator[field] = value.strip()
        weight = self._finite_number(indicator.get("weight"), "指标权重")
        score = self._finite_number(indicator.get("score"), "指标分数")
        if weight <= 0:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标权重必须为正")
        if not 0 <= score <= 100:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标分数必须在 0-100")
        evidence = self._raw_evidence(indicator)
        if not evidence:
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", f"指标没有证据: {indicator.get('name')}")
        indicator["ordinal"] = int(indicator.get("ordinal") or ordinal)
        indicator["score"] = round(score, 6)
        indicator["original_weight"] = weight
        indicator["evidence"] = evidence
        return indicator

    @staticmethod
    def _raw_evidence(indicator: Mapping[str, Any]) -> list[Any]:
        value = indicator.get("evidence", indicator.get("evidence_refs", []))
        if isinstance(value, (str, bytes, bytearray, int, Mapping)):
            return [value]
        if isinstance(value, Iterable):
            return list(value)
        return []

    def _validate_relation_coverage(
        self,
        indicators: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
    ) -> None:
        libraries = snapshot.get("libraries", {})
        present = {
            str(lib_type)
            for lib_type, value in libraries.items()
            if isinstance(value, Mapping) and int(value.get("count", 0) or 0) > 0
        }
        if len(present.intersection({"knowledge", "material", "algorithm"})) < 3:
            # 三库不完整时由编排服务返回 THREE_LIBRARIES_INCOMPLETE；这里不把
            # 一个本来无法产生关系的快照误报成 Agent 格式错误。
            return
        covered = False
        for indicator in indicators:
            values = " ".join(
                str(indicator.get(key, "")) for key in ("name", "meaning", "score_logic", "business_meaning", "reason")
            ).lower()
            if any(word.lower() in values for word in self._RELATION_WORDS):
                covered = True
            for key in ("covers_libraries", "covered_libraries", "library_pairs"):
                raw = indicator.get(key)
                if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, bytearray)):
                    raw_text = " ".join(str(item) for item in raw)
                    if all(lib in raw_text for lib in ("knowledge", "material", "algorithm")):
                        covered = True
            if any(ref.get("evidence_type") == "relation" for ref in self._safe_normalized_evidence(indicator)):
                covered = True
        if not covered:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "指标必须覆盖三库关系")

    def _validate_feature_readiness(self, report: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        readiness = report.get("feature_readiness", report.get("readiness"))
        if readiness is None and isinstance(snapshot.get("feature_readiness"), Mapping):
            readiness = snapshot.get("feature_readiness")
        if readiness is None:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", "缺少功能就绪度")
        global_missing = report.get("missing_items", snapshot.get("missing_items", []))
        global_missing = (
            global_missing if isinstance(global_missing, Sequence) and not isinstance(global_missing, str) else []
        )
        rows = (
            readiness.items()
            if isinstance(readiness, Mapping)
            else enumerate(readiness)
            if isinstance(readiness, Sequence)
            else []
        )
        for name, raw in rows:
            if isinstance(raw, bool):
                ready = raw
                missing: Any = []
            elif isinstance(raw, Mapping):
                ready = bool(raw.get("ready", raw.get("is_ready", False)))
                missing = raw.get("missing_items", raw.get("missing", []))
            else:
                raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"功能就绪项格式错误: {name}")
            if not ready:
                if not isinstance(missing, Sequence) or isinstance(missing, str):
                    missing = []
                if not missing and not global_missing:
                    raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"未就绪功能缺少说明: {name}")

    def _safe_normalized_evidence(self, indicator: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return [self._normalize_evidence(item) for item in self._raw_evidence(indicator)]
        except AssessmentValidationError:
            return []

    @staticmethod
    def _normalize_evidence(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bool):
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "布尔值不是合法证据")
        if isinstance(raw, (int, str)):
            return {"evidence_type": "asset", "asset_id": raw}
        if not isinstance(raw, Mapping):
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "证据引用必须是对象或资产 ID")
        item = dict(raw)
        evidence_type = item.get("evidence_type", item.get("type"))
        if evidence_type is None:
            if "asset_id" in item or ("id" in item and "source_document_id" not in item):
                evidence_type = "asset"
            elif "source_document_id" in item or "source_id" in item:
                evidence_type = "source_document"
            elif "from_asset_id" in item and "to_asset_id" in item:
                evidence_type = "relation"
            else:
                raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "证据缺少 evidence_type")
        item["evidence_type"] = str(evidence_type).strip().lower()
        return item

    @staticmethod
    def _finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"{label}必须是数字")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"{label}必须是数字") from exc
        if not math.isfinite(number):
            raise AssessmentValidationError("INDICATOR_RULE_VIOLATION", f"{label}必须是有限数字")
        return number

    @staticmethod
    def _snapshot_assets(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        raw = snapshot.get("assets", [])
        return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, Sequence) else []

    @classmethod
    def _snapshot_source_ids(cls, snapshot: Mapping[str, Any], assets: Sequence[Mapping[str, Any]]) -> set[int]:
        result: set[int] = set()
        for source in snapshot.get("sources", []) if isinstance(snapshot.get("sources"), Sequence) else []:
            if isinstance(source, Mapping):
                source_id = cls._as_int(source.get("id", source.get("source_document_id")))
                if source_id is not None:
                    result.add(source_id)
        for asset in assets:
            values = asset.get("source_document_ids", [])
            if isinstance(values, Sequence) and not isinstance(values, str):
                result.update(value for value in (cls._as_int(item) for item in values) if value is not None)
            for source in asset.get("sources", []) if isinstance(asset.get("sources"), Sequence) else []:
                if isinstance(source, Mapping):
                    source_id = cls._as_int(source.get("id", source.get("source_document_id")))
                    if source_id is not None:
                        result.add(source_id)
        return result

    @classmethod
    def _snapshot_relation_pairs(cls, snapshot: Mapping[str, Any]) -> set[tuple[int, int]]:
        result: set[tuple[int, int]] = set()
        for relation in snapshot.get("relations", []) if isinstance(snapshot.get("relations"), Sequence) else []:
            if not isinstance(relation, Mapping):
                continue
            left = cls._as_int(relation.get("from_asset_id"))
            right = cls._as_int(relation.get("to_asset_id"))
            if left is not None and right is not None:
                result.add((left, right))
        return result

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _require_snapshot(snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if not isinstance(snapshot, Mapping):
            raise AssessmentValidationError("EVIDENCE_REFERENCE_INVALID", "缺少当前快照")
        return snapshot

    @staticmethod
    def _resolve_report_snapshot(
        report: Mapping[str, Any], snapshot: Mapping[str, Any] | None
    ) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        if (
            isinstance(report, Mapping)
            and "assets" in report
            and isinstance(snapshot, Mapping)
            and "indicators" in snapshot
        ):
            return snapshot, report
        return report, snapshot


__all__ = ["AssessmentValidationError", "AssessmentValidationService"]

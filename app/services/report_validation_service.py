"""经营报告 Agent 输出的严格验证。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:\.\d+)?%?(?![\w])")
_FORBIDDEN_KEYS = {
    "charts",
    "charts_json",
    "chart_data",
    "value",
    "values",
    "amount",
    "rank",
    "ranking",
    "current_value",
    "actual_revenue",
    "actual_cost",
    "actual_net",
    "roi",
}
_CATEGORIES = ("money", "traffic", "product", "supplier")


class ReportValidationError(ValueError):
    """Agent 输出越权或结构不合法。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _walk(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    result = [(path, value)]
    if isinstance(value, Mapping):
        for key, child in value.items():
            result.extend(_walk(child, (*path, str(key))))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            result.extend(_walk(child, (*path, str(index))))
    return result


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).rstrip("%"))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _canonical_number(value: Any) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    normalized = number.normalize()
    return format(normalized, "f")


class ReportValidationService:
    """保证 Agent 只解释冻结快照中的事实。"""

    def validate(
        self,
        payload: Mapping[str, Any],
        deterministic_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ReportValidationError("REPORT_INVALID_STRUCTURE", "Agent 输出必须是对象")
        if set(payload) != {"sections", "suggestions", "summary"}:
            raise ReportValidationError("REPORT_AGENT_OVERREACH", "Agent 顶层包含越权字段")
        sections = payload.get("sections")
        suggestions = payload.get("suggestions")
        summary = payload.get("summary")
        if not isinstance(sections, Mapping) or set(sections) != set(_CATEGORIES):
            raise ReportValidationError("REPORT_INVALID_STRUCTURE", "sections 必须且只能包含经营四分类")
        if not isinstance(suggestions, list) or not isinstance(summary, str) or not summary.strip():
            raise ReportValidationError("REPORT_INVALID_STRUCTURE", "报告必须包含 suggestions 和非空 summary")

        facts = deterministic_snapshot.get("facts")
        if not isinstance(facts, Mapping):
            raise ReportValidationError("REPORT_SNAPSHOT_INVALID", "确定性快照缺少 facts")
        for category in _CATEGORIES:
            section = sections.get(category)
            fact = facts.get(category)
            if not isinstance(section, Mapping) or not isinstance(fact, Mapping):
                raise ReportValidationError("REPORT_INVALID_STRUCTURE", f"{category} 区块结构不完整")
            if set(section) - {"status", "explanation", "evidence_refs"}:
                raise ReportValidationError("REPORT_AGENT_OVERREACH", f"{category} 区块包含越权字段")
            if str(section.get("status")) != str(fact.get("status")):
                raise ReportValidationError("REPORT_CONCLUSION_MISMATCH", f"{category} 结论状态与快照不一致")
            if not isinstance(section.get("explanation"), str) or not str(section["explanation"]).strip():
                raise ReportValidationError("REPORT_INVALID_STRUCTURE", f"{category} 缺少解释")
            self._validate_refs(section.get("evidence_refs"), deterministic_snapshot)

        for suggestion in suggestions:
            if not isinstance(suggestion, Mapping):
                raise ReportValidationError("REPORT_INVALID_STRUCTURE", "建议必须是对象")
            if set(suggestion) - {"action", "priority", "reason", "evidence_refs"}:
                raise ReportValidationError("REPORT_AGENT_OVERREACH", "建议包含越权字段")
            if not all(
                isinstance(suggestion.get(key), str) and str(suggestion[key]).strip()
                for key in ("action", "priority", "reason")
            ):
                raise ReportValidationError("REPORT_INVALID_STRUCTURE", "建议字段不完整")
            self._validate_refs(suggestion.get("evidence_refs"), deterministic_snapshot)

        self._reject_forbidden_keys(payload)
        self._validate_numbers(payload, deterministic_snapshot)
        self._validate_named_references(payload, deterministic_snapshot)
        return dict(payload)

    @staticmethod
    def _allowed_refs(snapshot: Mapping[str, Any]) -> set[str]:
        values = snapshot.get("evidence_whitelist", [])
        refs = {str(value) for value in values} if isinstance(values, list) else set()
        refs.update({"data_quality", *(f"fact:{category}" for category in _CATEGORIES)})
        return refs

    def _validate_refs(self, refs: Any, snapshot: Mapping[str, Any]) -> None:
        if not isinstance(refs, list) or not refs:
            raise ReportValidationError("REPORT_EVIDENCE_INVALID", "每项解释必须包含 evidence_refs")
        allowed = self._allowed_refs(snapshot)
        invalid = [str(value) for value in refs if str(value) not in allowed]
        if invalid:
            raise ReportValidationError("REPORT_EVIDENCE_INVALID", f"存在快照外证据引用: {invalid[0]}")

    @staticmethod
    def _reject_forbidden_keys(payload: Mapping[str, Any]) -> None:
        for path, _value in _walk(payload):
            if path and path[-1].lower() in _FORBIDDEN_KEYS:
                raise ReportValidationError("REPORT_AGENT_OVERREACH", f"Agent 不得输出字段 {path[-1]}")

    @staticmethod
    def _snapshot_numbers(snapshot: Mapping[str, Any]) -> set[str]:
        allowed: set[str] = set()
        numeric_sources = {
            "facts": snapshot.get("facts", {}),
            "charts": snapshot.get("charts", {}),
            "indicators": [
                {
                    "value": row.get("value"),
                    "target_value": row.get("target_value"),
                }
                for row in snapshot.get("indicators", [])
                if isinstance(row, Mapping)
            ],
        }
        for path, value in _walk(numeric_sources):
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                # 实体 ID 和版本号不是可用于经营解释的业务数值。
                if path and (path[-1].endswith("_id") or path[-1] in {"id", "version"}):
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                normalized = _canonical_number(value)
                if normalized is not None:
                    allowed.add(normalized)
        return allowed

    def _validate_numbers(self, payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        allowed = self._snapshot_numbers(snapshot)
        for path, value in _walk(payload):
            if "evidence_refs" in path:
                continue
            candidates: list[str] = []
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                normalized = _canonical_number(value)
                if normalized is not None:
                    candidates.append(normalized)
            elif isinstance(value, str):
                candidates.extend(
                    normalized
                    for token in _NUMBER.findall(value)
                    if (normalized := _canonical_number(token)) is not None
                )
            for candidate in candidates:
                if candidate not in allowed:
                    raise ReportValidationError("REPORT_NUMBER_NOT_IN_SNAPSHOT", "Agent 输出了快照外数字")

    @staticmethod
    def _validate_named_references(payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        allowed_places = {str(value) for value in snapshot.get("place_names", [])}
        allowed_indicators = {str(value) for value in snapshot.get("indicator_names", [])}
        for path, value in _walk(payload):
            if not path or not isinstance(value, str):
                continue
            key = path[-1].lower()
            if key in {"place", "place_name"} and value not in allowed_places:
                raise ReportValidationError("REPORT_PLACE_NOT_IN_SNAPSHOT", "Agent 引用了快照外地点")
            if key in {"indicator", "indicator_name"} and value not in allowed_indicators:
                raise ReportValidationError("REPORT_INDICATOR_NOT_IN_SNAPSHOT", "Agent 引用了快照外指标")


__all__ = ["ReportValidationError", "ReportValidationService"]

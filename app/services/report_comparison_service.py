"""不可变经营报告的确定性比较。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Blogger, Report


class ReportComparisonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if isinstance(value, str) else default
    except (TypeError, json.JSONDecodeError):
        return default


def _delta(left: Any, right: Any) -> float | None:
    if isinstance(left, bool) or isinstance(right, bool):
        return None
    try:
        return round(float(right) - float(left), 8)
    except (TypeError, ValueError):
        return None


class ReportComparisonService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def compare(self, blogger_id: int, left_id: int, right_id: int) -> dict[str, Any]:
        self._active_blogger(blogger_id)
        left = self._report(blogger_id, left_id)
        right = left if left_id == right_id else self._report(blogger_id, right_id)
        left_snapshot = _json(left.snapshot_json, {})
        right_snapshot = _json(right.snapshot_json, {})
        left_facts = left_snapshot.get("facts", {}) if isinstance(left_snapshot, Mapping) else {}
        right_facts = right_snapshot.get("facts", {}) if isinstance(right_snapshot, Mapping) else {}
        return {
            "blogger_id": blogger_id,
            "left_id": left.id,
            "right_id": right.id,
            "indicator_changes": self._indicator_changes(left_snapshot, right_snapshot),
            "conclusion_changes": self._mapping_changes(left_facts, right_facts),
            "chart_changes": self._chart_changes(left_snapshot, right_snapshot),
            "data_quality_changes": self._mapping_changes(
                _json(left.data_quality_json, {}), _json(right.data_quality_json, {})
            ),
            "feedback_changes": self._feedback_changes(left_snapshot, right_snapshot),
            "left_snapshot_hash": left.snapshot_hash,
            "right_snapshot_hash": right.snapshot_hash,
        }

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise ReportComparisonError("BLOGGER_NOT_FOUND", "博主不存在或已删除")
        return row

    def _report(self, blogger_id: int, report_id: int) -> Report:
        row = self.db.scalar(select(Report).where(Report.id == report_id, Report.blogger_id == blogger_id))
        if row is None:
            raise ReportComparisonError("REPORT_NOT_FOUND", "报告不存在")
        if row.status != "succeeded":
            raise ReportComparisonError("REPORT_NOT_SUCCEEDED", "只能比较成功报告")
        return row

    @staticmethod
    def _mapping_changes(left: Any, right: Any) -> dict[str, Any]:
        left_map = left if isinstance(left, Mapping) else {}
        right_map = right if isinstance(right, Mapping) else {}
        changes: dict[str, Any] = {}
        for key in sorted(set(left_map) | set(right_map)):
            old = left_map.get(key)
            new = right_map.get(key)
            if old == new:
                continue
            if isinstance(old, Mapping) or isinstance(new, Mapping):
                changes[str(key)] = ReportComparisonService._mapping_changes(old, new)
            else:
                changes[str(key)] = {"left": old, "right": new, "delta": _delta(old, new)}
        return changes

    @staticmethod
    def _indicator_changes(left: Any, right: Any) -> list[dict[str, Any]]:
        left_rows = left.get("indicators", []) if isinstance(left, Mapping) else []
        right_rows = right.get("indicators", []) if isinstance(right, Mapping) else []
        left_map = {row.get("formula_key"): row for row in left_rows if isinstance(row, Mapping)}
        right_map = {row.get("formula_key"): row for row in right_rows if isinstance(row, Mapping)}
        return [
            {
                "formula_key": key,
                "left_value": left_map.get(key, {}).get("value"),
                "right_value": right_map.get(key, {}).get("value"),
                "delta": _delta(left_map.get(key, {}).get("value"), right_map.get(key, {}).get("value")),
                "left_status": left_map.get(key, {}).get("status"),
                "right_status": right_map.get(key, {}).get("status"),
            }
            for key in sorted(set(left_map) | set(right_map), key=str)
        ]

    @staticmethod
    def _chart_changes(left: Any, right: Any) -> dict[str, Any]:
        left_charts = left.get("charts", {}) if isinstance(left, Mapping) else {}
        right_charts = right.get("charts", {}) if isinstance(right, Mapping) else {}
        result: dict[str, Any] = {}
        for key in sorted(set(left_charts) | set(right_charts)):
            old = left_charts.get(key, {})
            new = right_charts.get(key, {})
            old_points = old.get("points", []) if isinstance(old, Mapping) else []
            new_points = new.get("points", []) if isinstance(new, Mapping) else []
            result[str(key)] = [
                {
                    "index": index,
                    "left_label": old.get("label") if isinstance(old, Mapping) else None,
                    "right_label": new.get("label") if isinstance(new, Mapping) else None,
                    "left": old.get("value") if isinstance(old, Mapping) else None,
                    "right": new.get("value") if isinstance(new, Mapping) else None,
                    "delta": _delta(
                        old.get("value") if isinstance(old, Mapping) else None,
                        new.get("value") if isinstance(new, Mapping) else None,
                    ),
                }
                for index, (old, new) in enumerate(
                    zip(
                        [*old_points, *({} for _ in range(max(0, len(new_points) - len(old_points))))],
                        [*new_points, *({} for _ in range(max(0, len(old_points) - len(new_points))))],
                        strict=True,
                    )
                )
            ]
        return result

    @staticmethod
    def _feedback_changes(left: Any, right: Any) -> dict[str, list[int]]:
        old = set(left.get("feedback_runs", [])) if isinstance(left, Mapping) else set()
        new = set(right.get("feedback_runs", [])) if isinstance(right, Mapping) else set()
        return {
            "left": sorted(old),
            "right": sorted(new),
            "applied_between": sorted(new - old),
            "no_longer_present": sorted(old - new),
        }


__all__ = ["ReportComparisonError", "ReportComparisonService"]

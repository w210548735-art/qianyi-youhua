"""第二阶段体检历史比较服务。

比较只读取两份已经成功保存的 Assessment 快照。所有查询都带上当前
``blogger_id``，因此即使调用方传入另一位博主的 assessment id，也只会
得到统一的 ``ASSESSMENT_NOT_FOUND``，不会暴露跨博主是否存在记录。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Assessment, AssessmentIndicator, Blogger


class AssessmentComparisonError(RuntimeError):
    """历史比较失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _load_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _delta(left: Any, right: Any) -> float | None:
    left_value = _number(left)
    right_value = _number(right)
    if left_value is None or right_value is None:
        return None
    return round(right_value - left_value, 4)


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _assessment_value(assessment: Any, key: str, default: Any = None) -> Any:
    if isinstance(assessment, Mapping):
        return assessment.get(key, default)
    return getattr(assessment, key, default)


def _assessment_json(assessment: Any, key: str, default: Any) -> Any:
    return _load_json(_assessment_value(assessment, key), default)


def _library_metrics(assessment: Any) -> dict[str, dict[str, Any]]:
    analysis = _assessment_json(assessment, "library_analysis_json", {})
    if not isinstance(analysis, Mapping):
        analysis = {}
    structure = _mapping(analysis.get("library_structure", analysis.get("libraries", analysis)))
    # 某些分析服务把三库放在 libraries 下，某些直接放在 library_structure 下。
    libraries = _mapping(structure.get("libraries", structure))
    counts = _mapping(analysis.get("library_counts"))
    result: dict[str, dict[str, Any]] = {}
    for lib_type in ("knowledge", "material", "algorithm"):
        raw = libraries.get(lib_type)
        if isinstance(raw, Mapping):
            count = raw.get("count", counts.get(lib_type, 0))
            credibility = raw.get("credibility", raw.get("credibility_distribution", {}))
        else:
            count = counts.get(lib_type, 0)
            credibility = {}
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        result[lib_type] = {
            "count": max(0, count),
            "credibility": credibility if isinstance(credibility, (Mapping, list)) else {},
        }
    return result


def _weak_points(assessment: Any) -> list[Any]:
    analysis = _assessment_json(assessment, "library_analysis_json", {})
    if isinstance(analysis, Mapping):
        for key in ("weak_points", "weak_categories", "weak_assets", "missing_items"):
            report_weak = _list(analysis.get(key))
            if report_weak:
                return report_weak
    # 兼容将弱项保存到 suggestions_json 的早期结构。
    suggestions = _assessment_json(assessment, "suggestions_json", {})
    if isinstance(suggestions, Mapping):
        return _list(suggestions.get("weak_points"))
    return []


def _readiness(assessment: Any) -> dict[str, Any]:
    raw = _assessment_json(assessment, "feature_readiness_json", {})
    if isinstance(raw, Mapping) and isinstance(raw.get("feature_readiness"), Mapping):
        raw = raw["feature_readiness"]
    return dict(raw) if isinstance(raw, Mapping) else {}


def _normalise_indicator(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        value = dict(row)
    else:
        value = {
            key: getattr(row, key, None)
            for key in (
                "id",
                "ordinal",
                "name",
                "meaning",
                "score_logic",
                "business_meaning",
                "weight",
                "weight_reason",
                "score",
                "reason",
                "evidence_json",
            )
        }
    evidence = _load_json(value.get("evidence_json", value.get("evidence_refs", [])), [])
    value["evidence_refs"] = evidence if isinstance(evidence, list) else []
    return value


class AssessmentComparisonService:
    """按博主隔离地比较两次历史体检。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def compare(self, blogger_id: int, left_id: int, right_id: int) -> dict[str, Any]:
        self._active_blogger(blogger_id)
        if left_id == right_id:
            # 同一份快照的比较没有泄漏风险，仍返回完整、可复用的差异结构。
            left = self._get_assessment(blogger_id, left_id)
            right = left
        else:
            left = self._get_assessment(blogger_id, left_id)
            right = self._get_assessment(blogger_id, right_id)
        left_indicators = self._indicators(left)
        right_indicators = self._indicators(right)

        left_libraries = _library_metrics(left)
        right_libraries = _library_metrics(right)
        library_metrics = {
            lib_type: {
                "left_count": left_libraries[lib_type]["count"],
                "right_count": right_libraries[lib_type]["count"],
                "count_delta": right_libraries[lib_type]["count"] - left_libraries[lib_type]["count"],
                "left_credibility": left_libraries[lib_type]["credibility"],
                "right_credibility": right_libraries[lib_type]["credibility"],
            }
            for lib_type in ("knowledge", "material", "algorithm")
        }
        library_counts = {
            lib_type: {
                "left": values["left_count"],
                "right": values["right_count"],
                "delta": values["count_delta"],
            }
            for lib_type, values in library_metrics.items()
        }
        credibility = {
            lib_type: {
                "left": values["left_credibility"],
                "right": values["right_credibility"],
            }
            for lib_type, values in library_metrics.items()
        }
        left_weak = _weak_points(left)
        right_weak = _weak_points(right)
        left_weak_keys = {_key(item.get("name") if isinstance(item, Mapping) else item) for item in left_weak}
        right_weak_keys = {_key(item.get("name") if isinstance(item, Mapping) else item) for item in right_weak}
        weak_added = [
            item
            for item in right_weak
            if _key(item.get("name") if isinstance(item, Mapping) else item) not in left_weak_keys
        ]
        weak_removed = [
            item
            for item in left_weak
            if _key(item.get("name") if isinstance(item, Mapping) else item) not in right_weak_keys
        ]
        left_readiness = _readiness(left)
        right_readiness = _readiness(right)
        readiness_changes = self._compare_readiness(left_readiness, right_readiness)
        left_score = _assessment_value(left, "overall_score")
        right_score = _assessment_value(right, "overall_score")
        indicators = self._compare_indicators(left_indicators, right_indicators)

        result = {
            "blogger_id": blogger_id,
            "left_id": _assessment_value(left, "id"),
            "right_id": _assessment_value(right, "id"),
            "left": self._summary(left),
            "right": self._summary(right),
            "overall_score": {
                "left": left_score,
                "right": right_score,
                "delta": _delta(left_score, right_score),
            },
            "overall_score_delta": _delta(left_score, right_score),
            "library_metrics": library_metrics,
            "library_scale": library_metrics,
            "library_counts": library_counts,
            "credibility": credibility,
            "weak_points": {
                "left": left_weak,
                "right": right_weak,
                "added": weak_added,
                "removed": weak_removed,
            },
            "feature_readiness": {
                "left": left_readiness,
                "right": right_readiness,
                "changes": readiness_changes,
            },
            "readiness": {
                "left": left_readiness,
                "right": right_readiness,
                "changes": readiness_changes,
            },
            "indicator_changes": indicators,
            "indicators": indicators,
            "indicator_system": {
                "added": indicators["added"],
                "removed": indicators["removed"],
            },
            "summary": self._summary_text(
                left_score,
                right_score,
                library_metrics,
                weak_added,
                weak_removed,
                indicators,
            ),
        }
        return result

    def _active_blogger(self, blogger_id: int) -> Any:
        blogger = self.db.scalar(
            select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None))
        )
        if blogger is None:
            raise AssessmentComparisonError("BLOGGER_NOT_FOUND", "博主不存在或已删除")
        return blogger

    def _get_assessment(self, blogger_id: int, assessment_id: int) -> Any:
        assessment = self.db.scalar(
            select(Assessment).where(
                Assessment.id == assessment_id,
                Assessment.blogger_id == blogger_id,
            )
        )
        if assessment is None:
            raise AssessmentComparisonError("ASSESSMENT_NOT_FOUND", "体检记录不存在")
        if _text(_assessment_value(assessment, "status")) != "succeeded":
            raise AssessmentComparisonError("ASSESSMENT_NOT_SUCCEEDED", "只能比较成功的体检记录")
        return assessment

    def _indicators(self, assessment: Any) -> list[dict[str, Any]]:
        rows: Iterable[Any]
        relationship = getattr(assessment, "indicators", None)
        if relationship is not None:
            rows = relationship
        else:
            rows = self.db.scalars(
                select(AssessmentIndicator)
                .where(AssessmentIndicator.assessment_id == _assessment_value(assessment, "id"))
                .order_by(AssessmentIndicator.ordinal.asc())
            )
        return [_normalise_indicator(row) for row in rows]

    @staticmethod
    def _summary(assessment: Any) -> dict[str, Any]:
        created_at = _assessment_value(assessment, "created_at")
        return {
            "id": _assessment_value(assessment, "id"),
            "status": _assessment_value(assessment, "status"),
            "created_at": str(created_at) if created_at else None,
            "snapshot_hash": _assessment_value(assessment, "snapshot_hash"),
            "overall_score": _assessment_value(assessment, "overall_score"),
            "summary": _assessment_value(assessment, "summary"),
        }

    @staticmethod
    def _compare_indicators(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
        left_map = {_key(row.get("name")): row for row in left if _key(row.get("name"))}
        right_map = {_key(row.get("name")): row for row in right if _key(row.get("name"))}
        matched: list[dict[str, Any]] = []
        for name in sorted(left_map.keys() & right_map.keys()):
            old = left_map[name]
            new = right_map[name]
            matched.append(
                {
                    "name": new.get("name") or old.get("name"),
                    "left_score": old.get("score"),
                    "right_score": new.get("score"),
                    "score_delta": _delta(old.get("score"), new.get("score")),
                    "left_weight": old.get("weight"),
                    "right_weight": new.get("weight"),
                    "weight_delta": _delta(old.get("weight"), new.get("weight")),
                }
            )
        added = [right_map[name] for name in sorted(right_map.keys() - left_map.keys())]
        removed = [left_map[name] for name in sorted(left_map.keys() - right_map.keys())]
        return {"matched": matched, "added": added, "removed": removed}

    @staticmethod
    def _compare_readiness(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for feature in sorted(set(left) | set(right)):
            left_value = _mapping(left.get(feature))
            right_value = _mapping(right.get(feature))
            old = left_value.get("ready", left_value.get("status"))
            new = right_value.get("ready", right_value.get("status"))
            if old != new or left_value.get("missing_items") != right_value.get("missing_items"):
                changes.append(
                    {
                        "feature": feature,
                        "left": dict(left_value),
                        "right": dict(right_value),
                    }
                )
        return changes

    @staticmethod
    def _summary_text(
        left_score: Any,
        right_score: Any,
        library_metrics: Mapping[str, Mapping[str, Any]],
        added: list[Any],
        removed: list[Any],
        indicator_changes: Mapping[str, Any],
    ) -> str:
        score_delta = _delta(left_score, right_score)
        total_delta = sum(int(value.get("count_delta", 0)) for value in library_metrics.values())
        return (
            f"综合分变化{score_delta if score_delta is not None else '暂无数据'}；"
            f"三库规模变化{total_delta:+d}；"
            f"新增薄弱项{len(added)}项、移除薄弱项{len(removed)}项；"
            f"同名指标{len(indicator_changes.get('matched', []))}项，"
            f"新增指标{len(indicator_changes.get('added', []))}项，"
            f"移除指标{len(indicator_changes.get('removed', []))}项。"
        )


# 兼容较短的导入命名。
ComparisonService = AssessmentComparisonService

__all__ = [
    "AssessmentComparisonError",
    "AssessmentComparisonService",
    "ComparisonService",
]

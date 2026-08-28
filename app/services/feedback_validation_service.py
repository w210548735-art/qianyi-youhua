"""反馈候选的结构、证据、样本质量和商业边界验证。"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

LIBRARY_TYPES = {"knowledge", "material", "algorithm"}
EVIDENCE_TYPES = {
    "metric",
    "output",
    "asset",
    "place",
    "output_asset",
    "output_place",
    "decision",
}
COMMERCIAL_FIELDS = {"est_benefit", "est_cost", "like_level", "fits_koc", "fits_shoot"}
TOP_LEVEL_FIELDS = {
    "data_quality",
    "suit_type_candidates",
    "knowledge_focus_candidates",
    "pitfalls",
    "asset_effects",
    "place_effects",
    "library_evolution",
    "main_direction_candidates",
    "insufficient_reason",
    "summary",
}
LIST_FIELDS = (
    "suit_type_candidates",
    "knowledge_focus_candidates",
    "pitfalls",
    "asset_effects",
    "place_effects",
    "library_evolution",
    "main_direction_candidates",
)


class FeedbackValidationError(ValueError):
    """验证失败时供 API/编排层映射为稳定 422。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _deleted(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _finite_number(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", f"{field} 必须是有限数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", f"{field} 必须是有限数字") from exc
    if not math.isfinite(number):
        raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", f"{field} 必须是有限数字")
    return number


class FeedbackValidationService:
    """验证并标准化 Agent 候选，但不执行任何数据库写回。"""

    def validate_and_normalize(
        self, payload: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", "反馈结果必须是对象")
        if not isinstance(snapshot, Mapping):
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "缺少冻结快照")
        extra = set(payload) - TOP_LEVEL_FIELDS
        if extra:
            raise FeedbackValidationError(
                "FEEDBACK_STRUCTURE_INVALID", f"反馈结果含未允许字段：{sorted(extra)}"
            )
        self._validate_snapshot(snapshot)
        whitelist = self._evidence_whitelist(snapshot)
        assets = self._asset_index(snapshot)
        places = self._place_index(snapshot)
        source_type = _text(_mapping(snapshot.get("primary_metric")).get("source_type"))
        user_confirmed = bool(_mapping(snapshot.get("primary_metric")).get("user_confirmed"))
        quality = _text(_mapping(payload.get("data_quality")).get("status"))
        expected_quality = _text(
            _mapping(snapshot.get("deterministic_analysis")).get("overall_status")
        )
        if quality not in {"ok", "data_insufficient"} or quality != expected_quality:
            raise FeedbackValidationError(
                "FEEDBACK_SAMPLE_INSUFFICIENT", "Agent 数据质量状态与确定性预分析不一致"
            )
        result: dict[str, Any] = {
            "data_quality": {"status": quality},
            "summary": self._required_text(payload.get("summary"), "summary"),
        }
        for field in LIST_FIELDS:
            value = payload.get(field)
            if not isinstance(value, list):
                raise FeedbackValidationError(
                    "FEEDBACK_STRUCTURE_INVALID", f"{field} 必须是数组"
                )
        if quality == "data_insufficient":
            reason = self._required_text(payload.get("insufficient_reason"), "insufficient_reason")
            if any(payload.get(field) for field in LIST_FIELDS):
                raise FeedbackValidationError(
                    "FEEDBACK_SAMPLE_INSUFFICIENT", "样本不足时不得输出武断分类或可应用候选"
                )
            result.update({field: [] for field in LIST_FIELDS})
            result["insufficient_reason"] = reason
            return result

        result["suit_type_candidates"] = self._validate_direction_items(
            payload["suit_type_candidates"], "value", whitelist, source_type
        )
        result["knowledge_focus_candidates"] = self._validate_direction_items(
            payload["knowledge_focus_candidates"], "value", whitelist, source_type
        )
        result["pitfalls"] = self._validate_direction_items(
            payload["pitfalls"], "pitfall", whitelist, source_type
        )
        result["main_direction_candidates"] = self._validate_direction_items(
            payload["main_direction_candidates"], "value", whitelist, source_type
        )
        result["asset_effects"] = self._validate_asset_effects(
            payload["asset_effects"], whitelist, assets, source_type
        )
        result["place_effects"] = self._validate_place_effects(
            payload["place_effects"],
            whitelist,
            places,
            snapshot,
            source_type,
            user_confirmed,
        )
        result["library_evolution"] = self._validate_library_evolution(
            payload["library_evolution"], whitelist, assets, source_type
        )
        return result

    validate = validate_and_normalize

    def _validate_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        blogger_id = _integer(snapshot.get("blogger_id"))
        profile = _mapping(snapshot.get("profile"))
        output = _mapping(snapshot.get("output"))
        metric = _mapping(snapshot.get("primary_metric"))
        if blogger_id is None or _integer(profile.get("id")) != blogger_id:
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "画像不属于当前博主")
        if _integer(output.get("blogger_id")) != blogger_id or _deleted(output.get("deleted_at")):
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "产出不属于当前博主或已删除")
        if _integer(metric.get("output_id")) != _integer(output.get("id")):
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "Metric 不属于当前 Output")
        if _text(metric.get("source_type")) not in {"manual", "simulated"}:
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "Metric 来源无效")
        source_type = _text(metric.get("source_type"))
        confirmed = bool(metric.get("user_confirmed"))
        actual_values = (metric.get("actual_revenue"), metric.get("actual_cost"))
        if source_type == "simulated" and (confirmed or any(item is not None for item in actual_values)):
            raise FeedbackValidationError(
                "FEEDBACK_SNAPSHOT_INVALID", "simulated Metric 不得伪装为用户确认的实际商业证据"
            )
        if any(item is not None for item in actual_values) and not (
            source_type == "manual" and confirmed
        ):
            raise FeedbackValidationError(
                "FEEDBACK_SNAPSHOT_INVALID", "实际商业值仅接受用户确认的 manual Metric"
            )
        for key in ("assets", "places", "active_memories"):
            for row in _rows(snapshot.get(key)):
                owner = _integer(row.get("blogger_id"))
                if owner is not None and owner != blogger_id:
                    raise FeedbackValidationError(
                        "FEEDBACK_SNAPSHOT_INVALID", f"{key} 含跨博主数据"
                    )
                if _deleted(row.get("deleted_at")):
                    raise FeedbackValidationError(
                        "FEEDBACK_SNAPSHOT_INVALID", f"{key} 含软删除数据"
                    )
        for asset in _rows(snapshot.get("assets")):
            credibility = asset.get("credibility")
            source_ids = asset.get("source_document_ids", [])
            has_source = isinstance(source_ids, list) and bool(source_ids)
            if credibility is not None and _finite_number(credibility, "credibility") < 3 and not has_source:
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "冻结快照含低可信且无来源资产"
                )
        asset_ids = set(self._asset_index(snapshot))
        place_ids = set(self._place_index(snapshot))
        output_id = _integer(output.get("id"))
        for relation in _rows(snapshot.get("output_assets")):
            if (
                _integer(relation.get("output_id")) != output_id
                or _integer(relation.get("asset_id")) not in asset_ids
            ):
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "OutputAsset 不属于当前可信产出链路"
                )
        for relation in _rows(snapshot.get("output_places")):
            if (
                _integer(relation.get("output_id")) != output_id
                or _integer(relation.get("place_id")) not in place_ids
            ):
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "OutputPlace 不属于当前地点链路"
                )

    @staticmethod
    def _asset_index(snapshot: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
        return {
            asset_id: item
            for item in _rows(snapshot.get("assets"))
            if (asset_id := _integer(item.get("id"))) is not None
        }

    @staticmethod
    def _place_index(snapshot: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
        return {
            place_id: item
            for item in _rows(snapshot.get("places"))
            if (place_id := _integer(item.get("id"))) is not None
        }

    @staticmethod
    def _evidence_whitelist(snapshot: Mapping[str, Any]) -> set[tuple[str, int]]:
        result: set[tuple[str, int]] = set()
        output = _mapping(snapshot.get("output"))
        metric = _mapping(snapshot.get("primary_metric"))
        expected: set[tuple[str, int]] = set()
        output_id = _integer(output.get("id"))
        metric_id = _integer(metric.get("id"))
        if output_id is not None:
            expected.add(("output", output_id))
        if metric_id is not None:
            expected.add(("metric", metric_id))
        for evidence_type, key in (
            ("asset", "assets"),
            ("place", "places"),
            ("output_asset", "output_assets"),
            ("output_place", "output_places"),
            ("decision", "decisions"),
        ):
            expected.update(
                (evidence_type, ref_id)
                for item in _rows(snapshot.get(key))
                if (ref_id := _integer(item.get("id"))) is not None
            )
        for item in _rows(snapshot.get("evidence_whitelist")):
            evidence_type = _text(item.get("evidence_type"))
            ref_id = _integer(item.get("ref_id"))
            if evidence_type not in EVIDENCE_TYPES or ref_id is None:
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "冻结快照证据白名单结构无效"
                )
            if (evidence_type, ref_id) not in expected:
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "冻结快照证据白名单含非当前 Output 链路引用"
                )
            result.add((evidence_type, ref_id))
        if not result:
            raise FeedbackValidationError("FEEDBACK_SNAPSHOT_INVALID", "冻结快照没有证据白名单")
        return result

    def _normalize_refs(
        self, value: Any, whitelist: set[tuple[str, int]]
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise FeedbackValidationError("FEEDBACK_EVIDENCE_INVALID", "候选缺少 evidence_refs")
        result: list[dict[str, Any]] = []
        for raw in value:
            if isinstance(raw, str) and ":" in raw:
                evidence_type, ref_text = raw.split(":", 1)
                ref_id = _integer(ref_text)
            elif isinstance(raw, Mapping):
                evidence_type = _text(raw.get("evidence_type", raw.get("type")))
                ref_id = _integer(raw.get("ref_id", raw.get("id")))
            else:
                evidence_type, ref_id = "", None
            key = (evidence_type, ref_id) if ref_id is not None else None
            if key is None or key not in whitelist:
                raise FeedbackValidationError(
                    "FEEDBACK_EVIDENCE_INVALID", "证据引用不属于当前冻结 Output/Metric 链路"
                )
            normalized = {"evidence_type": evidence_type, "ref_id": ref_id}
            if normalized not in result:
                result.append(normalized)
        return result

    def _base_candidate(
        self,
        item: Any,
        *,
        allowed: set[str],
        whitelist: set[tuple[str, int]],
        source_type: str,
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        if not isinstance(item, Mapping):
            raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", "候选必须是对象")
        extra = set(item) - allowed
        if extra:
            raise FeedbackValidationError(
                "FEEDBACK_STRUCTURE_INVALID", f"候选含未允许字段：{sorted(extra)}"
            )
        result = {
            "reason": self._required_text(item.get("reason"), "reason"),
            "evidence_refs": self._normalize_refs(item.get("evidence_refs"), whitelist),
            "simulation_only": bool(item.get("simulation_only", False)),
        }
        if source_type == "simulated" and not result["simulation_only"]:
            raise FeedbackValidationError(
                "FEEDBACK_SIMULATION_BOUNDARY", "simulated 方向候选必须明确 simulation_only"
            )
        return result, item

    def _validate_direction_items(
        self,
        values: list[Any],
        value_field: str,
        whitelist: set[tuple[str, int]],
        source_type: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        allowed = {value_field, "reason", "evidence_refs", "simulation_only"}
        for raw in values:
            item, original = self._base_candidate(
                raw, allowed=allowed, whitelist=whitelist, source_type=source_type
            )
            item[value_field] = self._required_text(original.get(value_field), value_field)
            result.append(item)
        return result

    def _validate_asset_effects(
        self,
        values: list[Any],
        whitelist: set[tuple[str, int]],
        assets: Mapping[int, Mapping[str, Any]],
        source_type: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        allowed = {
            "asset_id",
            "effect",
            "effect_weight",
            "reason",
            "evidence_refs",
            "simulation_only",
        }
        for raw in values:
            item, original = self._base_candidate(
                raw, allowed=allowed, whitelist=whitelist, source_type=source_type
            )
            asset_id = _integer(original.get("asset_id"))
            if asset_id is None or asset_id not in assets or ("asset", asset_id) not in whitelist:
                raise FeedbackValidationError(
                    "FEEDBACK_EVIDENCE_INVALID", "资产效果候选不属于当前产出链路"
                )
            effect = _text(original.get("effect"))
            if effect not in {"effective", "review", "unassessed"}:
                raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", "资产 effect 非法")
            weight = _finite_number(original.get("effect_weight"), "effect_weight")
            if not 0 <= weight <= 1:
                raise FeedbackValidationError(
                    "FEEDBACK_RANGE_INVALID", "effect_weight 必须在 0 到 1 之间"
                )
            item.update({"asset_id": asset_id, "effect": effect, "effect_weight": weight})
            result.append(item)
        return result

    def _validate_place_effects(
        self,
        values: list[Any],
        whitelist: set[tuple[str, int]],
        places: Mapping[int, Mapping[str, Any]],
        snapshot: Mapping[str, Any],
        source_type: str,
        user_confirmed: bool,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        allowed = {
            "place_id",
            "commercial_field",
            "adjust",
            "before",
            "after",
            "association_confidence",
            "applicable",
            "simulation_only",
            "reason",
            "evidence_refs",
        }
        explicit: Mapping[Any, Any] = _mapping(snapshot.get("user_confirmed_place_updates"))
        for raw in values:
            item, original = self._base_candidate(
                raw, allowed=allowed, whitelist=whitelist, source_type=source_type
            )
            place_id = _integer(original.get("place_id"))
            if place_id is None or place_id not in places or ("place", place_id) not in whitelist:
                raise FeedbackValidationError(
                    "FEEDBACK_EVIDENCE_INVALID", "地点候选不属于当前产出链路"
                )
            place = places[place_id]
            field = _text(original.get("commercial_field"))
            adjust = _text(original.get("adjust"))
            if field not in COMMERCIAL_FIELDS or adjust not in {"up", "down", "hold"}:
                raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", "地点商业字段或 adjust 非法")
            current = place.get(field)
            before = original.get("before")
            if before != current:
                raise FeedbackValidationError(
                    "FEEDBACK_SNAPSHOT_INVALID", "地点 before 与冻结商业字段不一致"
                )
            confidence = _text(
                place.get("association_confidence", original.get("association_confidence"))
            )
            applicable = bool(original.get("applicable", False))
            after = original.get("after")
            simulation_only = bool(item["simulation_only"])
            if confidence != "high" and (applicable or after is not None):
                raise FeedbackValidationError(
                    "FEEDBACK_PLACE_ASSOCIATION_UNCONFIRMED", "低置信名称匹配不得成为可应用地点变更"
                )
            if source_type == "simulated" and (
                after is not None or applicable or not simulation_only
            ):
                raise FeedbackValidationError(
                    "FEEDBACK_SIMULATION_BOUNDARY", "simulated 不得生成可应用商业收益变更"
                )
            if after is not None:
                if source_type != "manual" or not user_confirmed:
                    raise FeedbackValidationError(
                        "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED", "商业 after 仅接受用户确认的 manual 来源"
                    )
                expected_row = _mapping(explicit.get(str(place_id), explicit.get(place_id)))
                if field not in expected_row or expected_row.get(field) != after:
                    raise FeedbackValidationError(
                        "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED", "Agent 建议不能替代用户明确提供的 after 值"
                    )
                self._validate_commercial_value(field, after)
                if not applicable:
                    raise FeedbackValidationError(
                        "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED", "有明确 after 时必须标记为待确认可应用候选"
                    )
            elif applicable:
                raise FeedbackValidationError(
                    "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED", "缺少 after 的商业候选不可应用"
                )
            if after is None and adjust != "hold":
                raise FeedbackValidationError(
                    "FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED", "无明确 after 时 adjust 只能 hold"
                )
            item.update(
                {
                    "place_id": place_id,
                    "commercial_field": field,
                    "adjust": adjust,
                    "before": copy.deepcopy(before),
                    "after": copy.deepcopy(after),
                    "association_confidence": confidence,
                    "applicable": applicable,
                }
            )
            result.append(item)
        return result

    def _validate_library_evolution(
        self,
        values: list[Any],
        whitelist: set[tuple[str, int]],
        assets: Mapping[int, Mapping[str, Any]],
        source_type: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        allowed = {
            "lib_type",
            "action",
            "target_asset_id",
            "candidate",
            "reason",
            "evidence_refs",
            "simulation_only",
        }
        covered: set[str] = set()
        for raw in values:
            item, original = self._base_candidate(
                raw, allowed=allowed, whitelist=whitelist, source_type=source_type
            )
            lib_type = _text(original.get("lib_type"))
            action = _text(original.get("action"))
            if lib_type not in LIBRARY_TYPES or action not in {"add", "reinforce", "review"}:
                raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", "三库类型或进化 action 非法")
            target_id = _integer(original.get("target_asset_id"))
            candidate = _mapping(original.get("candidate"))
            if action == "add":
                if target_id is not None:
                    raise FeedbackValidationError(
                        "FEEDBACK_STRUCTURE_INVALID", "add 候选不得指定 target_asset_id"
                    )
                for field in ("category", "title", "content"):
                    self._required_text(candidate.get(field), f"candidate.{field}")
                safe_candidate = {
                    "category": _text(candidate.get("category")),
                    "title": _text(candidate.get("title")),
                    "content": _text(candidate.get("content")),
                    "tags": list(candidate.get("tags", []))
                    if isinstance(candidate.get("tags", []), list)
                    else [],
                }
            else:
                target = assets.get(target_id or -1)
                if target is None or _text(target.get("lib_type")) != lib_type:
                    raise FeedbackValidationError(
                        "FEEDBACK_EVIDENCE_INVALID", "reinforce/review 目标资产不属于对应库"
                    )
                if target_id is None or ("asset", target_id) not in whitelist:
                    raise FeedbackValidationError(
                        "FEEDBACK_EVIDENCE_INVALID", "三库进化目标不属于当前产出证据链"
                    )
                if any(key in candidate for key in ("title", "content")):
                    raise FeedbackValidationError(
                        "FEEDBACK_STRUCTURE_INVALID", "reinforce/review 不得改写资产标题或正文"
                    )
                safe_candidate = {}
            item.update(
                {
                    "lib_type": lib_type,
                    "action": action,
                    "target_asset_id": target_id,
                    "candidate": safe_candidate,
                }
            )
            covered.add(lib_type)
            result.append(item)
        if covered != LIBRARY_TYPES:
            raise FeedbackValidationError(
                "FEEDBACK_LIBRARY_COVERAGE_INVALID", "样本充分时三库进化必须覆盖知识、素材、算法"
            )
        return result

    @staticmethod
    def _validate_commercial_value(field: str, value: Any) -> None:
        if field in {"fits_koc", "fits_shoot"}:
            if not isinstance(value, bool):
                raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", f"{field} 必须是布尔值")
            return
        number = _finite_number(value, field)
        if field == "like_level" and not 0 <= number <= 5:
            raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", "like_level 必须在 0 到 5 之间")
        if field in {"est_benefit", "est_cost"} and number < 0:
            raise FeedbackValidationError("FEEDBACK_RANGE_INVALID", f"{field} 不得为负数")

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        result = _text(value)
        if not result:
            raise FeedbackValidationError("FEEDBACK_STRUCTURE_INVALID", f"缺少 {field}")
        return result


__all__ = ["FeedbackValidationError", "FeedbackValidationService"]

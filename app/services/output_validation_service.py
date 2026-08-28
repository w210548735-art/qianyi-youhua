"""第三阶段输出的后端确定性校验。

模型只负责提出候选内容，不能决定候选引用哪些实体、平台是否属于当前博主
或是否把缺失的商业数据当成事实。本服务对脚本、分镜、路线和排期使用同一
套博主隔离和 ``source_refs`` 规则，并返回可直接持久化的规范化字典。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from app.services.output_agent import (
    _asset_is_trusted,
    _asset_rows,
    _int,
    _place_rows,
    _profile,
    _source_ids,
    _text,
)


class OutputValidationError(ValueError):
    """输出未通过确定性安全校验。"""

    def __init__(self, code: str, message: str, *, details: Any | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"{code}: {message}")


_SCRIPT_REQUIRED = ("category", "title", "hook", "body", "ending", "tags", "style", "platform")
_SHOT_REQUIRED = ("sequence", "visual", "dialogue", "duration", "bgm", "transition")
_FORBIDDEN_COMMERCIAL_KEYS = {
    "price",
    "cost",
    "benefit",
    "revenue",
    "income",
    "est_cost",
    "est_benefit",
    "estimated_cost",
    "estimated_benefit",
    "commercial_value",
    "报价",
    "价格",
    "成本",
    "收益",
    "营收",
    "收入",
}
_COMMERCIAL_TEXT = re.compile(r"(?:¥|￥|\b\d+(?:\.\d+)?\s*元|价格\s*\d|成本\s*\d|收益\s*\d|报价\s*\d)", re.I)
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def _list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _output_type(value: Mapping[str, Any]) -> str:
    return _text(value.get("type", value.get("output_type", value.get("content_type"))))


def _source_refs(value: Mapping[str, Any]) -> list[Any]:
    for key in ("source_refs", "asset_refs", "evidence_refs", "references"):
        if key in value:
            raw = value[key]
            if isinstance(raw, Mapping):
                return [raw]
            return _list(raw)
    return []


def _asset_map(snapshot: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        parsed: row
        for row in _asset_rows(snapshot)
        for parsed in [_int(row.get("id", row.get("asset_id")))]
        if parsed is not None
    }


def _place_map(snapshot: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        parsed: row
        for row in _place_rows(snapshot)
        for parsed in [_int(row.get("id", row.get("place_id")))]
        if parsed is not None
    }


def _snapshot_source_ids(snapshot: Mapping[str, Any], assets: Iterable[Mapping[str, Any]]) -> set[int]:
    result: set[int] = set()
    for raw in _list(snapshot.get("sources", snapshot.get("source_documents", []))):
        parsed = _int(raw.get("id", raw.get("source_document_id")) if isinstance(raw, Mapping) else raw)
        if parsed is not None:
            result.add(parsed)
    for asset in assets:
        result.update(_source_ids(asset))
    return result


def _profile_platforms(snapshot: Mapping[str, Any]) -> set[str]:
    value = _profile(snapshot).get("platform", snapshot.get("platform"))
    values = _list(value) if isinstance(value, (list, tuple)) else re.split(r"[,，、/|]", _text(value))
    return {item.strip().lower() for item in values if item and item.strip()}


def _profile_style(snapshot: Mapping[str, Any]) -> str:
    return _text(_profile(snapshot).get("style", snapshot.get("style")))


def _ensure_mapping(value: Any, code: str = "OUTPUT_INVALID_JSON") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OutputValidationError(code, "输出顶层必须是 JSON 对象")
    return dict(value)


def _ensure_text(row: Mapping[str, Any], field: str, *, code: str = "OUTPUT_INVALID_JSON") -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OutputValidationError(code, f"输出字段 {field} 不能为空")
    return value.strip()


class OutputValidationService:
    """对输出候选执行无副作用、可重复的规范化和校验。"""

    def validate_and_normalize(
        self,
        output: Mapping[str, Any] | Any,
        output_type: str | Mapping[str, Any] | None = None,
        snapshot: Mapping[str, Any] | None = None,
        *,
        script: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 兼容调用方的 ``(output_type, output, snapshot)`` 形式。
        if isinstance(output, str) and isinstance(output_type, Mapping):
            output, output_type = output_type, output
        resolved = _ensure_mapping(output)
        if snapshot is None:
            raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "缺少冻结输入快照")
        kind = _text(output_type) if isinstance(output_type, str) else _output_type(resolved)
        if not kind:
            raise OutputValidationError("OUTPUT_INVALID_JSON", "缺少输出类型")
        if kind in {"script", "脚本"}:
            return self.validate_script(resolved, snapshot)
        if kind in {"storyboard", "分镜"}:
            return self.validate_storyboard(resolved, script, snapshot)
        if kind in {"route_rec", "route", "路线"}:
            return self.validate_route(resolved, snapshot)
        if kind in {"schedule", "排期"}:
            return self.validate_schedule(resolved, snapshot)
        raise OutputValidationError("OUTPUT_INVALID_JSON", f"不支持的输出类型: {kind}")

    # 英美拼写和简短命名均保留，便于 API/服务层注入。
    normalize = validate_and_normalize
    normalise = validate_and_normalize
    validate = validate_and_normalize
    validate_output = validate_and_normalize

    def validate_script(self, output: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = _ensure_mapping(output)
        for field in _SCRIPT_REQUIRED:
            if field == "tags":
                tags = result.get(field)
                if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)) or not tags:
                    raise OutputValidationError("OUTPUT_INVALID_JSON", "脚本 tags 必须是非空数组")
                if any(not isinstance(item, str) or not item.strip() for item in tags):
                    raise OutputValidationError("OUTPUT_INVALID_JSON", "脚本 tags 必须是非空字符串")
            else:
                _ensure_text(result, field)
        self._validate_platform_and_style(result, snapshot)
        self._reject_commercial_data(result)
        refs = self.validate_evidence_refs(_source_refs(result), snapshot, require_knowledge=True)
        result["type"] = "script"
        result["output_type"] = "script"
        result["source_refs"] = refs
        return _json_clone(result)

    def validate_storyboard(
        self,
        output: Mapping[str, Any],
        script: Mapping[str, Any] | None,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = _ensure_mapping(output)
        if script is None or not isinstance(script, Mapping):
            raise OutputValidationError("STORYBOARD_SCRIPT_REQUIRED", "分镜必须关联有效脚本版本")
        script_id = script.get("id", script.get("output_id", script.get("script_id")))
        supplied_script_id = result.get("script_id", result.get("script_output_id", result.get("parent_output_id")))
        if supplied_script_id is None or (script_id is not None and str(supplied_script_id) != str(script_id)):
            raise OutputValidationError("STORYBOARD_SCRIPT_REQUIRED", "分镜脚本 ID 不存在或与脚本版本不匹配")
        script_version = script.get("version", script.get("script_version"))
        supplied_version = result.get("script_version", result.get("version_ref"))
        if script_version is not None and supplied_version is not None and str(script_version) != str(supplied_version):
            raise OutputValidationError("STORYBOARD_SCRIPT_REQUIRED", "分镜未关联当前脚本版本")
        raw_shots = result.get("shots", result.get("storyboard", result.get("items")))
        if not isinstance(raw_shots, Sequence) or isinstance(raw_shots, (str, bytes, bytearray)) or not raw_shots:
            raise OutputValidationError("OUTPUT_INVALID_JSON", "分镜 shots 必须是非空数组")
        normalized_shots: list[dict[str, Any]] = []
        sequences: set[int] = set()
        all_refs: list[Any] = []
        for index, raw_shot in enumerate(raw_shots, start=1):
            shot = _ensure_mapping(raw_shot)
            for field in _SHOT_REQUIRED:
                if field == "sequence":
                    sequence_value = _int(shot.get(field))
                    if sequence_value is None or sequence_value <= 0 or sequence_value in sequences:
                        raise OutputValidationError("OUTPUT_INVALID_JSON", f"分镜 sequence 无效: {index}")
                    sequences.add(sequence_value)
                    shot[field] = sequence_value
                elif field == "duration":
                    duration_value = _number(shot.get(field))
                    if duration_value is None or duration_value <= 0 or duration_value > 3600:
                        raise OutputValidationError("OUTPUT_INVALID_JSON", f"分镜 duration 无效: {index}")
                    shot[field] = duration_value
                else:
                    _ensure_text(shot, field)
            refs = self.validate_evidence_refs(_source_refs(shot), snapshot, require_knowledge=False)
            if not refs:
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"分镜第 {index} 镜头缺少 source_refs")
            shot["source_refs"] = refs
            all_refs.extend(refs)
            normalized_shots.append(shot)
        # 分镜应保留脚本的事实证据；单镜头引用可能只引用素材，因此再检查整体知识引用。
        self.validate_evidence_refs(all_refs, snapshot, require_knowledge=True)
        if "platform" in result:
            self._validate_platform_and_style(result, snapshot, check_style=False)
        self._reject_commercial_data(result)
        result["type"] = "storyboard"
        result["output_type"] = "storyboard"
        result["shots"] = normalized_shots
        result["script_id"] = supplied_script_id
        if supplied_version is not None:
            result["script_version"] = supplied_version
        result["source_refs"] = self._dedupe_refs(all_refs)
        return _json_clone(result)

    def validate_schedule(self, output: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = _ensure_mapping(output)
        raw_items = result.get("items", result.get("schedules", result.get("entries")))
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)) or not raw_items:
            raise OutputValidationError("OUTPUT_INVALID_JSON", "排期 items 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        platforms = _profile_platforms(snapshot)
        for index, raw_item in enumerate(raw_items, start=1):
            item = _ensure_mapping(raw_item)
            plan_date = _text(item.get("plan_date", item.get("date")))
            parsed_date = self._parse_date(plan_date)
            if parsed_date is None:
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"排期日期无效: {index}")
            platform = _ensure_text(item, "platform")
            if platforms and platform.lower() not in platforms:
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"排期平台不匹配画像: {platform}")
            content_type = _text(item.get("content_type", item.get("type")))
            if content_type not in {"script", "storyboard", "route_rec", "script_output", "storyboard_output"}:
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"排期内容类型无效: {content_type}")
            title = _ensure_text(item, "title")
            item["plan_date"] = parsed_date.isoformat()
            item["platform"] = platform
            item["content_type"] = content_type
            item["title"] = title
            normalized.append(item)
        self._reject_commercial_data(result)
        result["type"] = "schedule"
        result["output_type"] = "schedule"
        result["items"] = normalized
        return _json_clone(result)

    def validate_route(self, output: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = _ensure_mapping(output)
        places = _place_map(snapshot)
        raw_stops = result.get("stops", result.get("places", result.get("items")))
        if not isinstance(raw_stops, Sequence) or isinstance(raw_stops, (str, bytes, bytearray)) or not raw_stops:
            raise OutputValidationError("OUTPUT_INVALID_JSON", "路线 stops 必须是非空数组")
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()
        all_refs: list[Any] = []
        for index, raw_stop in enumerate(raw_stops, start=1):
            stop = _ensure_mapping(raw_stop)
            place_id = _int(stop.get("place_id", stop.get("id")))
            if place_id is None or place_id not in places:
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"路线地点不属于当前博主: {place_id}")
            if place_id in seen:
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"路线地点重复: {place_id}")
            seen.add(place_id)
            sequence = _int(stop.get("sequence", index))
            if sequence is None or sequence <= 0:
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"路线顺序无效: {index}")
            place = places[place_id]
            self._validate_place_commercial_fields(stop, place)
            refs = self.validate_evidence_refs(_source_refs(stop), snapshot, require_knowledge=False)
            all_refs.extend(refs)
            normalized.append({**stop, "place_id": place_id, "sequence": sequence, "source_refs": refs})
        if all_refs:
            self.validate_evidence_refs(all_refs, snapshot, require_knowledge=False)
        result["type"] = "route_rec"
        result["output_type"] = "route_rec"
        result["stops"] = normalized
        result["source_refs"] = self._dedupe_refs(all_refs)
        return _json_clone(result)

    def validate_evidence_refs(
        self,
        refs: Any,
        snapshot: Mapping[str, Any],
        *,
        require_knowledge: bool = False,
    ) -> list[dict[str, Any]]:
        """校验 source_refs 是当前博主未删除实体，且事实资产可信。"""

        if isinstance(refs, Mapping) or isinstance(refs, (str, int)):
            refs = [refs]
        if not isinstance(refs, Iterable) or isinstance(refs, (str, bytes, bytearray)):
            raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "source_refs 必须是数组")
        assets = _asset_map(snapshot)
        sources = _snapshot_source_ids(snapshot, assets.values())
        normalized: list[dict[str, Any]] = []
        has_trusted_knowledge = False
        for raw in refs:
            if isinstance(raw, bool):
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "布尔值不是合法证据引用")
            if isinstance(raw, (int, str)):
                raw = {"asset_id": raw}
            if not isinstance(raw, Mapping):
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "证据引用必须是对象或 ID")
            item = dict(raw)
            evidence_type = _text(item.get("evidence_type", item.get("type")), "asset").lower()
            if evidence_type in {"asset", "knowledge", "material", "algorithm"}:
                asset_id = _int(item.get("asset_id", item.get("id")))
                if asset_id is None or asset_id not in assets:
                    raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"资产证据不存在: {item.get('asset_id')}")
                asset = assets[asset_id]
                if not _asset_is_trusted(asset, snapshot):
                    raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"资产不可作为可信输出证据: {asset_id}")
                supplied_source = item.get("source_document_id", item.get("source_id"))
                if supplied_source is not None:
                    source_id = _int(supplied_source)
                    if source_id is None or source_id not in sources or source_id not in _source_ids(asset):
                        raise OutputValidationError(
                            "OUTPUT_EVIDENCE_INVALID", f"资产来源未关联当前资产: {supplied_source}"
                        )
                    item["source_document_id"] = source_id
                item["evidence_type"] = "asset"
                item["asset_id"] = asset_id
                item.setdefault("claim", _text(asset.get("title"), "当前快照资产"))
                has_trusted_knowledge |= _text(asset.get("lib_type"), "knowledge") == "knowledge"
            elif evidence_type in {"source", "source_document"}:
                source_id = _int(item.get("source_document_id", item.get("source_id")))
                if source_id is None or source_id not in sources:
                    raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"来源证据不存在: {source_id}")
                item["evidence_type"] = "source_document"
                item["source_document_id"] = source_id
                item.setdefault("claim", "当前快照来源")
            else:
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"不支持的证据类型: {evidence_type}")
            normalized.append(item)
        if not normalized:
            raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "输出至少需要一条 source_refs")
        if require_knowledge and not has_trusted_knowledge:
            raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "脚本事实必须引用至少一条可信知识资产")
        return self._dedupe_refs(normalized)

    @staticmethod
    def _dedupe_refs(refs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in refs:
            item = dict(raw)
            key = json.dumps(
                {
                    "evidence_type": item.get("evidence_type"),
                    "asset_id": item.get("asset_id"),
                    "source_document_id": item.get("source_document_id"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def _validate_platform_and_style(
        self,
        output: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        check_style: bool = True,
    ) -> None:
        platforms = _profile_platforms(snapshot)
        platform = _text(output.get("platform"))
        if platforms and platform.lower() not in platforms:
            raise OutputValidationError("OUTPUT_INVALID_JSON", f"输出平台与当前画像不匹配: {platform}")
        if check_style:
            expected = _profile_style(snapshot)
            actual = _text(output.get("style"))
            if (
                expected
                and actual
                and expected.lower() not in actual.lower()
                and actual.lower() not in expected.lower()
            ):
                raise OutputValidationError("OUTPUT_INVALID_JSON", f"输出风格与当前画像不匹配: {actual}")

    def _reject_commercial_data(self, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key).strip().lower()
                if key_text in {item.lower() for item in _FORBIDDEN_COMMERCIAL_KEYS} and child is not None:
                    raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"输出不得编造商业字段: {key}")
                self._reject_commercial_data(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                self._reject_commercial_data(child)
        elif isinstance(value, str) and _COMMERCIAL_TEXT.search(value):
            raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", "输出包含未经证实的商业数值")

    @staticmethod
    def _validate_place_commercial_fields(stop: Mapping[str, Any], place: Mapping[str, Any]) -> None:
        for field in ("est_cost", "est_benefit", "like_level", "fits_koc", "fits_shoot"):
            if field not in stop:
                continue
            supplied = stop[field]
            actual = place.get(field)
            if supplied != actual:
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"地点商业字段不是当前地点事实: {field}")
        for field in ("est_cost", "est_benefit"):
            value = place.get(field)
            if value is not None and (_number(value) is None or (_number(value) or 0) < 0):
                raise OutputValidationError("OUTPUT_EVIDENCE_INVALID", f"地点商业字段无效: {field}")

    @staticmethod
    def _parse_date(value: str) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            for fmt in _DATE_FORMATS[1:]:
                try:
                    from datetime import datetime

                    return datetime.strptime(value, fmt).date()
                except ValueError:
                    continue
        return None


__all__ = ["OutputValidationError", "OutputValidationService"]

"""知识库体检 Agent。

本模块只负责将确定性库分析转换为结构化体检报告。库数量、关系和证据
归属由 ``LibraryAnalysisService`` 提供；Agent 不得自行创造资产、来源或
博主记忆引用。生产实现调用 ``deepseek-v4-flash``，离线测试使用 Fake。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx

from app.core.config import settings

LIBRARY_TYPES = ("knowledge", "material", "algorithm")
MAX_PROMPT_ASSETS_PER_LIBRARY = 50
REQUIRED_INDICATOR_FIELDS = (
    "name",
    "meaning",
    "score_logic",
    "business_meaning",
    "weight",
    "weight_reason",
    "score",
    "reason",
    "evidence_refs",
)


class AssessmentAgentError(RuntimeError):
    """体检 Agent 的安全、可观测错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        super().__init__(f"{code}: {message}")


class AssessmentAgent(Protocol):
    """可注入的体检 Agent 协议。

    ``context_messages`` 必须由调用方先通过 ``ContextService`` 组装，顺序
    固定为系统规则、当前任务短期记忆、当前博主长期记忆和本轮输入。
    """

    def assess(
        self,
        context_messages: list[dict[str, str]],
        analysis: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        ...


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_score(value: Any) -> float:
    return max(0.0, min(100.0, _number(value)))


def _json_load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _asset_id(item: Any) -> str | int | None:
    if isinstance(item, Mapping):
        value = item.get("id", item.get("asset_id"))
    else:
        value = getattr(item, "id", getattr(item, "asset_id", None))
    if value is None or str(value).strip() == "":
        return None
    return value


def _source_id(item: Any) -> str | int | None:
    if isinstance(item, Mapping):
        value = item.get("source_document_id", item.get("source_id"))
        if value is None:
            values = item.get("source_document_ids")
            if isinstance(values, (list, tuple)) and values:
                value = values[0]
            elif isinstance(item.get("sources"), list) and item["sources"]:
                source = item["sources"][0]
                if isinstance(source, Mapping):
                    value = source.get("id", source.get("source_document_id", source.get("source_id")))
                else:
                    value = getattr(source, "id", getattr(source, "source_document_id", None))
    else:
        value = getattr(item, "source_document_id", getattr(item, "source_id", None))
    if value is None or str(value).strip() == "":
        return None
    return value


def _asset_title(item: Any) -> str:
    if isinstance(item, Mapping):
        return _text(item.get("title"), "未命名资产")
    return _text(getattr(item, "title", None), "未命名资产")


def _asset_content(item: Any) -> str:
    if isinstance(item, Mapping):
        return _text(item.get("content"))
    return _text(getattr(item, "content", None))


def _belongs_to_analysis(item: Any, analysis: Mapping[str, Any]) -> bool:
    expected = analysis.get("blogger_id")
    if expected is None and isinstance(analysis.get("blogger"), Mapping):
        expected = analysis["blogger"].get("id")
    if expected is None:
        return True
    actual = item.get("blogger_id") if isinstance(item, Mapping) else getattr(item, "blogger_id", None)
    if actual is None:
        return True
    try:
        return int(actual) == int(expected)
    except (TypeError, ValueError):
        return False


def _library_items(analysis: Mapping[str, Any], lib_type: str) -> list[Any]:
    """兼容分析服务的几种公开快照形状。"""

    libraries = _mapping(analysis.get("libraries", analysis.get("library_structure")))
    value = libraries.get(lib_type)
    if isinstance(value, Mapping):
        for key in ("assets", "items", "records", "rows"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if _belongs_to_analysis(item, analysis)]
    if isinstance(value, list):
        return [item for item in value if _belongs_to_analysis(item, analysis)]
    assets_by_library = _mapping(analysis.get("assets_by_library"))
    value = assets_by_library.get(lib_type)
    if isinstance(value, list):
        return [item for item in value if _belongs_to_analysis(item, analysis)]
    all_assets = _as_list(analysis.get("assets"))
    return [
        item
        for item in all_assets
        if _belongs_to_analysis(item, analysis)
        if _text(item.get("lib_type") if isinstance(item, Mapping) else getattr(item, "lib_type", None))
        == lib_type
    ]


def _library_count(analysis: Mapping[str, Any], lib_type: str, items: Sequence[Any]) -> int:
    counts = _mapping(analysis.get("library_counts"))
    if not counts:
        counts = _mapping(analysis.get("counts"))
    if lib_type in counts:
        try:
            return max(0, int(counts[lib_type]))
        except (TypeError, ValueError):
            pass
    libraries = _mapping(analysis.get("libraries", analysis.get("library_structure")))
    value = libraries.get(lib_type)
    if isinstance(value, Mapping) and value.get("count") is not None:
        try:
            return max(0, int(value["count"]))
        except (TypeError, ValueError):
            pass
    return len(items)


def _source_coverage(analysis: Mapping[str, Any]) -> float:
    for key in ("source_coverage", "source_coverage_rate", "knowledge_source_coverage"):
        if key in analysis:
            raw = analysis[key]
            if isinstance(raw, Mapping):
                value = _number(raw.get("ratio", raw.get("rate", raw.get("percentage"))))
                if value is None:
                    total = _number(raw.get("with_source"), 0.0) + _number(raw.get("without_source"), 0.0)
                    value = _number(raw.get("with_source"), 0.0) / total if total else 0.0
            else:
                value = _number(raw)
            value = value or 0.0
            # 分析服务可以返回比例或百分数。
            return _clamp_score(value * 100 if 0 <= value <= 1 else value)
    knowledge = _library_items(analysis, "knowledge")
    if not knowledge:
        return 0.0
    covered = sum(1 for item in knowledge if _source_id(item) is not None)
    return covered / len(knowledge) * 100


def _relations(analysis: Mapping[str, Any]) -> list[Any]:
    for key in ("library_relations", "relations", "cross_library_relations", "semantic_relations"):
        value = analysis.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _relation_pairs(analysis: Mapping[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for relation in _relations(analysis):
        if not isinstance(relation, Mapping):
            continue
        left = relation.get("from_asset_id", relation.get("left_asset_id"))
        right = relation.get("to_asset_id", relation.get("right_asset_id"))
        if left is not None and right is not None:
            pairs.add((str(left), str(right)))
    return pairs


def _relation_count(analysis: Mapping[str, Any], relations: Sequence[Any]) -> int:
    for key in ("relation_count", "cross_library_relation_count"):
        if key in analysis:
            try:
                return max(0, int(analysis[key]))
            except (TypeError, ValueError):
                pass
    return len(relations)


def _profile_coverage(analysis: Mapping[str, Any]) -> float:
    for key in ("profile_coverage", "profile_direction_coverage", "direction_coverage"):
        value = analysis.get(key)
        if isinstance(value, Mapping):
            for subkey in ("score", "rate", "ratio", "percentage"):
                if subkey in value:
                    number = _number(value[subkey])
                    return _clamp_score(number * 100 if number <= 1 else number)
        elif value is not None:
            number = _number(value)
            if number is not None:
                return _clamp_score(number * 100 if number <= 1 else number)
        if isinstance(value, Mapping):
            directions = value.get("directions")
            if isinstance(directions, Mapping) and directions:
                covered = sum(bool(_mapping(item).get("covered")) for item in directions.values())
                return covered / len(directions) * 100
    return 0.0


def _weak_points(analysis: Mapping[str, Any]) -> list[Any]:
    for key in ("weak_points", "weak_categories", "weak_assets", "gaps", "missing_items"):
        value = analysis.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _core_assets(analysis: Mapping[str, Any]) -> list[Any]:
    for key in ("core_assets", "key_assets", "featured_assets"):
        value = analysis.get(key)
        if isinstance(value, list):
            return list(value)
    # 没有显式核心资产时只从已有资产中选取，不创造新引用。
    result: list[Any] = []
    for lib_type in LIBRARY_TYPES:
        items = _library_items(analysis, lib_type)
        result.extend(items[:2])
    return result


def _evidence_pool(analysis: Mapping[str, Any]) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    """返回当前分析快照中允许 Agent 使用的资产、来源和证据引用。"""

    asset_ids: set[str] = set()
    source_ids: set[str] = set()
    refs: list[dict[str, Any]] = []

    def add_item(item: Any, evidence_type: str = "asset") -> None:
        aid = _asset_id(item)
        sid = _source_id(item)
        if aid is not None:
            aid_key = str(aid)
            if aid_key not in asset_ids:
                asset_ids.add(aid_key)
                ref: dict[str, Any] = {
                    "evidence_type": evidence_type,
                    "asset_id": aid,
                    "claim": _asset_title(item),
                }
                if sid is not None:
                    source_ids.add(str(sid))
                    ref["source_document_id"] = sid
                refs.append(ref)
        elif sid is not None:
            sid_key = str(sid)
            source_ids.add(sid_key)
            refs.append(
                {
                    "evidence_type": "source_document",
                    "source_document_id": sid,
                    "claim": _asset_title(item),
                }
            )

    for lib_type in LIBRARY_TYPES:
        for item in _library_items(analysis, lib_type):
            add_item(item)
    for relation in _relations(analysis):
        if not isinstance(relation, Mapping):
            continue
        left = relation.get("from_asset_id", relation.get("left_asset_id"))
        right = relation.get("to_asset_id", relation.get("right_asset_id"))
        if left is None or right is None or str(left) not in asset_ids or str(right) not in asset_ids:
            continue
        refs.append(
            {
                "evidence_type": "relation",
                "from_asset_id": left,
                "to_asset_id": right,
                "claim": _text(relation.get("relation_type"), "当前快照中的跨库关系"),
            }
        )
    for item in _as_list(analysis.get("evidence_refs")):
        if not isinstance(item, Mapping):
            continue
        aid = item.get("asset_id")
        sid = item.get("source_document_id", item.get("source_id"))
        if aid is not None and str(aid) not in asset_ids:
            continue
        if sid is not None and str(sid) not in source_ids and aid is None:
            continue
        ref = dict(item)
        if aid is not None:
            asset_ids.add(str(aid))
        if sid is not None:
            source_ids.add(str(sid))
        refs.append(ref)
    return asset_ids, source_ids, refs


def _normalise_ref(
    value: Any,
    *,
    asset_ids: set[str],
    source_ids: set[str],
    relation_pairs: set[tuple[str, str]],
) -> dict[str, Any] | None:
    if isinstance(value, (str, int)):
        if str(value) in asset_ids:
            return {"evidence_type": "asset", "asset_id": value, "claim": "当前快照资产"}
        if str(value) in source_ids:
            return {"evidence_type": "source_document", "source_document_id": value, "claim": "当前快照来源"}
        return None
    if not isinstance(value, Mapping):
        return None
    evidence_type = _text(value.get("evidence_type", value.get("type")), "").lower()
    if evidence_type == "relation" or ("from_asset_id" in value and "to_asset_id" in value):
        left = value.get("from_asset_id")
        right = value.get("to_asset_id")
        if left is None or right is None or (str(left), str(right)) not in relation_pairs:
            return None
        return {
            "evidence_type": "relation",
            "from_asset_id": left,
            "to_asset_id": right,
            "claim": _text(value.get("claim"), "当前快照中的跨库关系"),
        }
    aid = value.get("asset_id")
    sid = value.get("source_document_id", value.get("source_id"))
    if aid is not None and str(aid) not in asset_ids:
        return None
    if sid is not None and str(sid) not in source_ids:
        return None
    if aid is None and sid is None:
        return None
    result: dict[str, Any] = {
        "evidence_type": _text(value.get("evidence_type"), "asset"),
        "claim": _text(value.get("claim"), "当前快照证据"),
    }
    if aid is not None:
        result["asset_id"] = aid
    if sid is not None:
        result["source_document_id"] = sid
    return result


def _verified_structure(analysis: Mapping[str, Any]) -> dict[str, Any]:
    libraries: dict[str, Any] = {}
    for lib_type in LIBRARY_TYPES:
        items = _library_items(analysis, lib_type)
        raw = _mapping(_mapping(analysis.get("libraries", analysis.get("library_structure"))).get(lib_type))
        entry = dict(raw)
        entry["count"] = _library_count(analysis, lib_type, items)
        # 详情只引用真实快照中的轻量字段，避免将动态 ORM 对象塞入提示词或结果。
        entry["asset_ids"] = [item_id for item_id in (_asset_id(item) for item in items) if item_id is not None]
        if "assets" not in entry:
            entry["assets"] = [
                {"id": _asset_id(item), "title": _asset_title(item)}
                for item in items[:20]
                if _asset_id(item) is not None
            ]
        libraries[lib_type] = entry
    return {
        "libraries": libraries,
        "total_count": sum(int(libraries[lib]["count"]) for lib in LIBRARY_TYPES),
        "source_coverage": _source_coverage(analysis),
    }


def _verified_relations(analysis: Mapping[str, Any], asset_ids: set[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for relation in _relations(analysis):
        if isinstance(relation, Mapping):
            left = relation.get(
                "left_asset_id",
                relation.get("source_asset_id", relation.get("from_asset_id", relation.get("asset_id"))),
            )
            right = relation.get("right_asset_id", relation.get("target_asset_id", relation.get("to_asset_id")))
            if left is not None and str(left) not in asset_ids:
                continue
            if right is not None and str(right) not in asset_ids:
                continue
            if left is None and right is None and not relation.get("from") and not relation.get("to"):
                continue
            output.append(dict(relation))
        elif isinstance(relation, str) and relation.strip():
            # 文字关系可能只包含库名，不含证据 ID；它仍可用于展示结构关系。
            output.append({"description": relation.strip()})
    return output


def _readiness(analysis: Mapping[str, Any], weak_points: Sequence[Any]) -> tuple[dict[str, Any], list[str]]:
    """输出后续功能的就绪状态，不把未实现功能伪装成已实现。"""

    raw = _mapping(analysis.get("feature_readiness"))
    counts = {lib: _library_count(analysis, lib, _library_items(analysis, lib)) for lib in LIBRARY_TYPES}
    missing = [_text(item) for item in weak_points if _text(item)]
    result: dict[str, Any] = {}
    feature_requirements = {
        "script_generation": ("material", "素材库"),
        "route_recommendation": ("knowledge", "知识库"),
        "publishing": ("algorithm", "算法库"),
        "feedback_learning": ("algorithm", "反馈数据"),
        "operating_report": ("knowledge", "收益与经营数据"),
    }
    for feature, (lib_type, label) in feature_requirements.items():
        supplied = _mapping(raw.get(feature))
        explicit_missing = [_text(item) for item in _as_list(supplied.get("missing_items")) if _text(item)]
        if feature in raw and "ready" in supplied:
            ready = bool(supplied["ready"])
        else:
            ready = counts.get(lib_type, 0) > 0 and feature not in {"feedback_learning", "operating_report"}
        feature_missing = list(explicit_missing)
        if not ready and not feature_missing:
            feature_missing.append(f"缺少{label}或必要的可信数据")
        result[feature] = {
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "missing_items": feature_missing,
            "reason": _text(supplied.get("reason"), "当前阶段仅评估就绪度，后续功能尚未实现"),
        }
        missing.extend(f"{feature}: {item}" for item in feature_missing)
    # 去重且保持稳定顺序。
    return result, list(dict.fromkeys(item for item in missing if item))


def _default_indicators(analysis: Mapping[str, Any], evidence_refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = {lib: _library_count(analysis, lib, _library_items(analysis, lib)) for lib in LIBRARY_TYPES}
    total = sum(counts.values())
    source_score = _source_coverage(analysis)
    relations = _relations(analysis)
    relation_score = 100.0 if relations else 0.0
    if total:
        relation_score = min(100.0, max(relation_score, _relation_count(analysis, relations) / max(total, 1) * 100))
    structure_score = 100.0 if all(counts.values()) else sum(bool(count) for count in counts.values()) / 3 * 100
    refs = list(evidence_refs[:3])
    relation_refs = [ref for ref in evidence_refs if ref.get("evidence_type") == "relation"]
    # 每项至少引用一个真实快照证据；没有资产时由上层服务阻止体检，避免伪造证据。
    return [
        {
            "name": "三库结构完整度",
            "meaning": "衡量知识、素材、算法三库是否形成可用的基础结构",
            "score_logic": "三个库均有资产得100分，按非空库数量等比例计分",
            "business_meaning": "结构完整度决定后续功能是否有基本输入",
            "weight": 35.0,
            "weight_reason": "三库是体检的基础门槛，权重最高",
            "score": structure_score,
            "reason": f"知识库{counts['knowledge']}条、素材库{counts['material']}条、算法库{counts['algorithm']}条",
            "evidence_refs": copy.deepcopy(refs),
            "evidence": copy.deepcopy(refs),
        },
        {
            "name": "可信来源覆盖度",
            "meaning": "衡量知识资产能否追溯到可信来源",
            "score_logic": "有可信来源的知识资产占比乘以100",
            "business_meaning": "来源覆盖越高，事实型内容越适合进入后续生产流程",
            "weight": 35.0,
            "weight_reason": "证据链可信度直接影响结论风险",
            "score": source_score,
            "reason": f"当前知识资产来源覆盖度为{source_score:g}%",
            "evidence_refs": copy.deepcopy(refs),
            "evidence": copy.deepcopy(refs),
        },
        {
            "name": "三库关联度",
            "meaning": "衡量三库之间是否存在真实可追溯的语义关系",
            "score_logic": "按确定性分析得到的跨库关系数量计算，至少有关系才得分",
            "business_meaning": "关联度决定知识能否支撑素材和算法协同使用",
            "weight": 30.0,
            "weight_reason": "跨库关系是从资产走向可用能力的关键，但不覆盖基础结构",
            "score": relation_score,
            "reason": f"确定性分析发现{_relation_count(analysis, relations)}条跨库关系",
            "evidence_refs": copy.deepcopy(relation_refs[:1] or refs),
            "evidence": copy.deepcopy(relation_refs[:1] or refs),
        },
    ]


def _normalise_indicators(
    raw: Any,
    *,
    analysis: Mapping[str, Any],
    asset_ids: set[str],
    source_ids: set[str],
    relation_pairs: set[tuple[str, str]],
    fallback_refs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _as_list(raw)
    if len(rows) < 3:
        return _default_indicators(analysis, fallback_refs)
    indicators: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        refs_raw = item.get("evidence_refs", item.get("evidence", item.get("evidence_json", [])))
        refs = [
            ref
            for ref in (
                _normalise_ref(
                    value,
                    asset_ids=asset_ids,
                    source_ids=source_ids,
                    relation_pairs=relation_pairs,
                )
                for value in _as_list(refs_raw)
            )
            if ref is not None
        ]
        if not refs:
            refs = [copy.deepcopy(fallback_refs[index % len(fallback_refs)])] if fallback_refs else []
        indicators.append(
            {
                "name": name,
                "meaning": _text(item.get("meaning"), "基于当前知识库快照评估该维度"),
                "score_logic": _text(item.get("score_logic"), "按照当前快照中的可验证资产计算0-100分"),
                "business_meaning": _text(item.get("business_meaning"), "用于判断后续功能输入是否充分"),
                "weight": max(0.01, _number(item.get("weight"), 1.0)),
                "weight_reason": _text(item.get("weight_reason"), "根据该维度对体检结论的影响设置"),
                "score": _clamp_score(item.get("score")),
                "reason": _text(item.get("reason"), "评分依据见证据引用"),
                "evidence_refs": refs,
                "evidence": copy.deepcopy(refs),
                "ordinal": int(item.get("ordinal", index + 1)),
            }
        )
    return indicators if len(indicators) >= 3 else _default_indicators(analysis, fallback_refs)


def normalise_assessment_output(
    payload: Mapping[str, Any],
    analysis: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """清理 Agent 输出，并强制证据引用属于当前分析快照。"""

    if not isinstance(payload, Mapping):
        raise AssessmentAgentError("AGENT_INVALID_JSON", "体检 Agent 顶层结果必须是 JSON 对象", request_id=request_id)
    asset_ids, source_ids, pool = _evidence_pool(analysis)
    relation_pairs = _relation_pairs(analysis)
    if not pool:
        raise AssessmentAgentError(
            "AGENT_INVALID_JSON",
            "当前体检分析没有可引用的真实资产或来源",
            request_id=request_id,
        )
    structure = _verified_structure(analysis)
    relations = _verified_relations(analysis, asset_ids)
    core = _core_assets(analysis)
    core_assets = []
    for item in core:
        aid = _asset_id(item)
        if aid is not None and str(aid) in asset_ids:
            core_assets.append({"asset_id": aid, "title": _asset_title(item)})
    weak = [_text(item) if not isinstance(item, Mapping) else dict(item) for item in _weak_points(analysis)]
    readiness, missing = _readiness(analysis, weak)
    indicators = _normalise_indicators(
        payload.get("indicators"),
        analysis=analysis,
        asset_ids=asset_ids,
        source_ids=source_ids,
        relation_pairs=relation_pairs,
        fallback_refs=pool,
    )
    total_weight = sum(_number(item["weight"]) for item in indicators)
    if total_weight <= 0:
        raise AssessmentAgentError("AGENT_INVALID_JSON", "指标权重必须为正数", request_id=request_id)
    for index, indicator in enumerate(indicators, start=1):
        indicator["ordinal"] = index
        indicator["weight"] = indicator["weight"] / total_weight * 100
    overall = sum(_clamp_score(item["score"]) * item["weight"] / 100 for item in indicators)
    suggestions = [
        _text(item) if not isinstance(item, Mapping) else dict(item)
        for item in _as_list(payload.get("suggestions", payload.get("improvement_suggestions")))
        if _text(item if not isinstance(item, Mapping) else item.get("suggestion", item.get("title", "")))
    ]
    if not suggestions:
        suggestions = [
            "优先补齐薄弱分类，并为新增知识事实绑定可核验来源。",
            "建立知识、素材、算法之间的可追溯关系后再进入后续生产功能。",
        ]
    summary = _text(payload.get("summary"), f"当前三库综合评估分为{overall:.1f}分，共发现{len(missing)}项就绪缺失。")
    return {
        "request_id": request_id,
        "library_structure": structure,
        "three_library_structure": structure,
        "library_relations": relations,
        "relations": relations,
        "core_assets": core_assets,
        "weak_points": weak,
        "feature_readiness": readiness,
        "missing_items": missing,
        "suggestions": suggestions,
        "summary": summary,
        "indicators": indicators,
        "overall_score": round(overall, 4),
        "model_overall_score": payload.get("overall_score"),
    }


def _parse_json_content(content: Any, *, request_id: str | None = None) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise AssessmentAgentError("AGENT_INVALID_JSON", "模型响应不是 JSON 对象或字符串", request_id=request_id)
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise AssessmentAgentError("AGENT_INVALID_JSON", "模型响应无法解析为 JSON", request_id=request_id) from None
    if not isinstance(parsed, dict):
        raise AssessmentAgentError("AGENT_INVALID_JSON", "模型响应顶层必须是 JSON 对象", request_id=request_id)
    return parsed


def _analysis_for_prompt(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """仅把确定性分析摘要放入提示词，避免把 ORM/全部历史数据发送给模型。"""

    result = {
        key: value
        for key, value in analysis.items()
        if key
        in {
            "blogger_id",
            "snapshot_hash",
            "libraries",
            "library_structure",
            "library_counts",
            "library_relations",
            "relations",
            "core_assets",
            "weak_points",
            "source_coverage",
            "profile_coverage",
            "feature_readiness",
        }
    }
    # 资产条目只保留 ID、标题、分类等用于证据选择的必要字段。
    for lib_type in LIBRARY_TYPES:
        items = _library_items(analysis, lib_type)
        if items:
            result.setdefault("assets_by_library", {})[lib_type] = [
                {
                    "id": _asset_id(item),
                    "title": _asset_title(item),
                    "content": _asset_content(item)[:500],
                    "source_document_id": _source_id(item),
                }
                for item in items[:MAX_PROMPT_ASSETS_PER_LIBRARY]
                if _asset_id(item) is not None
            ]
    return result


class _IdempotentAssessmentAgent:
    """为 Fake 和生产 Agent 提供成功结果幂等。"""

    def __init__(self) -> None:
        self._result_cache: dict[str, dict[str, Any]] = {}

    def assess(
        self,
        context_messages: list[dict[str, str]],
        analysis: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(context_messages, list):
            raise AssessmentAgentError("AGENT_INVALID_CONTEXT", "体检上下文必须是消息列表", request_id=request_id)
        normalized_id = request_id or self._request_id(context_messages, analysis)
        if normalized_id in self._result_cache:
            return copy.deepcopy(self._result_cache[normalized_id])
        result = self._assess(context_messages, analysis, request_id=normalized_id)
        self._result_cache[normalized_id] = copy.deepcopy(result)
        return result

    def _assess(
        self,
        context_messages: list[dict[str, str]],
        analysis: Mapping[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _request_id(context_messages: Sequence[Mapping[str, str]], analysis: Mapping[str, Any]) -> str:
        raw = json.dumps(
            {"context": list(context_messages), "snapshot": analysis.get("snapshot_hash")},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FakeAssessmentAgent(_IdempotentAssessmentAgent):
    """离线体检 Agent，可注入自定义响应和失败，供单元测试使用。"""

    def __init__(
        self,
        *,
        response: Mapping[str, Any] | None = None,
        fail_with: AssessmentAgentError | None = None,
    ) -> None:
        super().__init__()
        self.response = dict(response) if response is not None else None
        self.fail_with = fail_with
        self.call_count = 0
        self.model_name = "fake-assessment-agent"

    def _assess(
        self,
        context_messages: list[dict[str, str]],
        analysis: Mapping[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        del context_messages
        if self.fail_with is not None:
            raise self.fail_with
        self.call_count += 1
        payload = self.response or {}
        return normalise_assessment_output(payload, analysis, request_id=request_id)


class DeepSeekAssessmentAgent(_IdempotentAssessmentAgent):
    """生产体检 Agent，使用配置的 ``deepseek-v4-flash`` 模型。"""

    def __init__(
        self,
        timeout_seconds: float = 90.0,
        *,
        post: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self._post = post or httpx.post
        self.call_count = 0
        self.model_name = settings.deepseek_model

    def _assess(
        self,
        context_messages: list[dict[str, str]],
        analysis: Mapping[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        if not context_messages:
            raise AssessmentAgentError("AGENT_INVALID_CONTEXT", "体检上下文不能为空", request_id=request_id)
        try:
            key_file = settings.deepseek_key_file
            if not key_file.exists():
                raise AssessmentAgentError("DEEPSEEK_KEY_NOT_FOUND", "DeepSeek key 文件不存在", request_id=request_id)
            api_key = key_file.read_text(encoding="utf-8").strip()
            if not api_key:
                raise AssessmentAgentError("DEEPSEEK_KEY_EMPTY", "DeepSeek key 文件为空", request_id=request_id)
            prompt = self._prompt(context_messages, analysis, request_id)
            first_content = self._call(api_key, prompt)
            self.call_count += 1
            try:
                parsed = _parse_json_content(first_content, request_id=request_id)
                parsed = self._canonicalize_report_payload(parsed)
                self._ensure_report_shape(parsed, request_id)
                return normalise_assessment_output(parsed, analysis, request_id=request_id)
            except AssessmentAgentError as first_error:
                if first_error.code != "AGENT_INVALID_JSON":
                    raise
                # 只允许一次格式修复，修复请求也必须经过同一模型和证据白名单。
                repair_prompt = {
                    "task": "修复上一条体检结果为合法 JSON",
                    "invalid_response": str(first_content)[:20000],
                    "required": (
                        "只输出符合 output_schema 的 JSON 对象；顶层字段必须使用 indicators，"
                        "不得增加当前分析中不存在的资产、来源或记忆引用"
                    ),
                    "output_schema": self._output_schema(),
                    "analysis": _analysis_for_prompt(analysis),
                }
                repaired_content = self._call(api_key, repair_prompt)
                self.call_count += 1
                try:
                    repaired = _parse_json_content(repaired_content, request_id=request_id)
                    repaired = self._canonicalize_report_payload(repaired)
                    self._ensure_report_shape(repaired, request_id)
                    return normalise_assessment_output(repaired, analysis, request_id=request_id)
                except AssessmentAgentError as repaired_error:
                    raise AssessmentAgentError(
                        "AGENT_INVALID_JSON",
                        "体检 Agent JSON 在一次修复后仍不合法",
                        request_id=request_id,
                    ) from repaired_error
        except AssessmentAgentError:
            raise
        except httpx.TimeoutException as exc:
            raise AssessmentAgentError(
                "AGENT_TIMEOUT", "DeepSeek 体检请求超时", retryable=True, request_id=request_id
            ) from exc
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise AssessmentAgentError(
                "AGENT_REQUEST_FAILED",
                f"DeepSeek 体检请求失败：{exc.__class__.__name__}",
                retryable=True,
                request_id=request_id,
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AssessmentAgentError(
                "AGENT_INVALID_JSON", "DeepSeek 响应缺少合法 choices/message/content 结构", request_id=request_id
            ) from exc

    def _call(self, api_key: str, prompt: Mapping[str, Any]) -> Any:
        response = self._post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": settings.deepseek_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是严谨的贵州文旅知识库体检助手，只能引用输入分析中的真实资产和来源，"
                            "必须输出合法 JSON；不得生成脚本、路线、发布或经营报告。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    @staticmethod
    def _canonicalize_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """在严格校验前收敛模型常见的等价顶层命名。"""

        canonical = dict(payload)
        nested = next(
            (
                value
                for key in ("assessment", "assessment_report", "report", "result")
                if isinstance((value := canonical.get(key)), Mapping)
            ),
            None,
        )
        if nested is not None:
            for key, value in nested.items():
                canonical.setdefault(str(key), value)
        if not isinstance(canonical.get("indicators"), list):
            for alias in (
                "assessment_indicators",
                "custom_indicators",
                "dynamic_indicators",
                "metrics",
                "评估指标",
                "自创指标",
                "指标",
            ):
                value = canonical.get(alias)
                if isinstance(value, list):
                    canonical["indicators"] = value
                    break
        return canonical

    @staticmethod
    def _ensure_report_shape(payload: Mapping[str, Any], request_id: str) -> None:
        indicators = payload.get("indicators")
        if not isinstance(indicators, list) or len(indicators) < 3:
            raise AssessmentAgentError(
                "AGENT_INVALID_JSON",
                "体检 Agent 必须返回至少三项指标",
                request_id=request_id,
            )
        required = {
            "name",
            "meaning",
            "score_logic",
            "business_meaning",
            "weight",
            "weight_reason",
            "score",
            "reason",
        }
        for indicator in indicators:
            if not isinstance(indicator, Mapping) or not required.issubset(indicator):
                raise AssessmentAgentError(
                    "AGENT_INVALID_JSON",
                    "体检 Agent 指标字段不完整",
                    request_id=request_id,
                )
            evidence = indicator.get("evidence_refs", indicator.get("evidence"))
            if not isinstance(evidence, list) or not evidence:
                raise AssessmentAgentError(
                    "AGENT_INVALID_JSON",
                    "体检 Agent 指标必须包含证据引用",
                    request_id=request_id,
                )

    @staticmethod
    def _prompt(
        context_messages: Sequence[Mapping[str, str]],
        analysis: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "context_messages": [dict(message) for message in context_messages],
            "library_analysis": _analysis_for_prompt(analysis),
            "requirements": [
                "自创至少3个指标，每项必须包含name、meaning、score_logic、business_meaning、weight、weight_reason、score、reason、evidence_refs",
                "输出三库结构、库间关系、核心资产、薄弱点、功能就绪度、缺失项、改进建议和总结",
                "evidence_refs只能引用输入快照中存在的asset_id或source_document_id",
                "未知的后续数据必须写暂无数据，不得推断为0效果",
            ],
            "output_schema": DeepSeekAssessmentAgent._output_schema(),
        }

    @staticmethod
    def _output_schema() -> dict[str, Any]:
        return {
            "indicators": [
                {
                    "name": "字符串",
                    "meaning": "字符串",
                    "score_logic": "字符串",
                    "business_meaning": "字符串",
                    "weight": "正数，所有指标权重合计100",
                    "weight_reason": "字符串",
                    "score": "0到100",
                    "reason": "字符串",
                    "evidence_refs": [
                        {
                            "evidence_type": "asset或source_document或relation",
                            "asset_id": "快照中已有ID或null",
                            "source_document_id": "快照中已有ID或null",
                            "claim": "字符串",
                        }
                    ],
                }
            ],
            "library_structure": "对象",
            "library_relations": "数组",
            "core_assets": "数组",
            "weak_points": "数组",
            "feature_readiness": "对象；未就绪项必须有missing_items",
            "suggestions": "数组",
            "summary": "字符串",
        }


ProductionAssessmentAgent = DeepSeekAssessmentAgent

__all__ = [
    "AssessmentAgent",
    "AssessmentAgentError",
    "DeepSeekAssessmentAgent",
    "FakeAssessmentAgent",
    "ProductionAssessmentAgent",
    "normalise_assessment_output",
]

"""第三阶段内容输出 Agent。

本模块只负责从调用方提供的冻结快照生成结构化候选，不直接读取或修改数据库。
所有事实、资产和地点引用都在 ``OutputValidationService`` 中再次校验；因此
Agent 的自然语言能力不会绕过博主隔离、可信来源或第三阶段边界。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Protocol

import httpx

from app.core.config import settings

OUTPUT_TYPES = ("script", "storyboard", "route_rec")
MAX_PROMPT_ASSETS = 80
MAX_PROMPT_PLACES = 50
MAX_PROMPT_MEMORIES = 20


class OutputAgentError(RuntimeError):
    """输出 Agent 的安全、可重试错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
        missing_fields: Sequence[str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.missing_fields = list(missing_fields or [])
        super().__init__(f"{code}: {message}")


class OutputAgent(Protocol):
    """可注入的内容输出 Agent 协议。"""

    def generate_script(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    def generate_storyboard(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        script: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    def generate_schedule(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        output: Mapping[str, Any],
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


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy_deleted(value: Any) -> bool:
    if value is None or value is False or value == 0 or value == "":
        return False
    return True


def _json_load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default


def _profile(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("profile", "blogger", "blogger_profile"):
        value = snapshot.get(key)
        if isinstance(value, Mapping):
            return value
    return snapshot


def _blogger_id(snapshot: Mapping[str, Any]) -> int | None:
    value = snapshot.get("blogger_id")
    if value is None:
        value = _profile(snapshot).get("id", _profile(snapshot).get("blogger_id"))
    return _int(value)


def _platform(snapshot: Mapping[str, Any]) -> str:
    value = _profile(snapshot).get("platform", snapshot.get("platform"))
    if isinstance(value, (list, tuple)):
        return _text(value[0]) if value else "抖音"
    return _text(value, "抖音")


def _style(snapshot: Mapping[str, Any]) -> str:
    return _text(_profile(snapshot).get("style", snapshot.get("style")), "口播")


def _content_category(snapshot: Mapping[str, Any]) -> str:
    profile = _profile(snapshot)
    value = profile.get("knowledge_focus") or profile.get("suit_type")
    if value:
        return _text(str(value).split(",")[0].split("/")[0], "贵州文旅")
    value = profile.get("content_types", snapshot.get("content_types"))
    if isinstance(value, (list, tuple)) and value:
        return _text(value[0], "贵州文旅")
    if value:
        return _text(value, "贵州文旅")
    return "贵州文旅"


def _asset_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从快照中提取资产，并按博主/软删除规则预先隔离。"""

    values: list[Any] = []
    for key in ("trusted_assets", "assets"):
        if isinstance(snapshot.get(key), Sequence) and not isinstance(snapshot.get(key), (str, bytes, bytearray)):
            values.extend(snapshot[key])
            if key == "trusted_assets":
                break
    if not values:
        libraries = _mapping(snapshot.get("libraries", snapshot.get("library_structure")))
        for lib_type, library in libraries.items():
            if isinstance(library, Mapping):
                library_rows = library.get("assets", library.get("items", []))
            else:
                library_rows = library
            for row in _as_list(library_rows):
                if isinstance(row, Mapping):
                    values.append({**row, "lib_type": row.get("lib_type", lib_type)})
    if not values:
        by_library = _mapping(snapshot.get("assets_by_library"))
        for lib_type, library_rows in by_library.items():
            for row in _as_list(library_rows):
                if isinstance(row, Mapping):
                    values.append({**row, "lib_type": row.get("lib_type", lib_type)})
    expected = _blogger_id(snapshot)
    rows: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        actual = _int(row.get("blogger_id"))
        if expected is not None and actual is not None and actual != expected:
            continue
        if _truthy_deleted(row.get("deleted_at", row.get("deleted"))):
            continue
        asset_id = _int(row.get("id", row.get("asset_id")))
        if asset_id is None:
            continue
        row["id"] = asset_id
        row["lib_type"] = _text(row.get("lib_type"), "knowledge")
        rows.append(row)
    # 同一个资产可能同时出现在 trusted_assets 和 libraries 中；去重保持快照顺序。
    unique: dict[int, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    return list(unique.values())


def _place_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = snapshot.get("trusted_places", snapshot.get("places", []))
    if not values:
        by_category = _mapping(snapshot.get("places_by_category"))
        values = [row for rows in by_category.values() for row in _as_list(rows)]
    expected = _blogger_id(snapshot)
    rows: list[dict[str, Any]] = []
    for raw in _as_list(values):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        actual = _int(row.get("blogger_id"))
        if expected is not None and actual is not None and actual != expected:
            continue
        if _truthy_deleted(row.get("deleted_at", row.get("deleted"))):
            continue
        place_id = _int(row.get("id", row.get("place_id")))
        if place_id is None:
            continue
        row["id"] = place_id
        rows.append(row)
    unique: dict[int, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    return list(unique.values())


def _source_ids(asset: Mapping[str, Any]) -> list[int]:
    values: list[Any] = []
    for key in ("source_document_ids", "source_ids"):
        values.extend(_as_list(asset.get(key)))
    for source in _as_list(asset.get("sources")):
        if isinstance(source, Mapping):
            values.append(source.get("id", source.get("source_document_id", source.get("source_id"))))
        else:
            values.append(source)
    for key in ("source_document_id", "source_id"):
        if asset.get(key) is not None:
            values.append(asset[key])
    result: list[int] = []
    for value in values:
        parsed = _int(value)
        if parsed is not None and parsed not in result:
            result.append(parsed)
    return result


def _asset_is_trusted(asset: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    """判断一个资产能否作为事实引用。

    知识资产必须有可信来源且 credibility 至少为 3；素材/算法可以作为
    结构模板引用，但仍必须来自当前未删除快照。调用方可以显式提供
    ``trusted``/``is_trusted``，显式否定永远优先。
    """

    if asset.get("trusted") is False or asset.get("is_trusted") is False:
        return False
    if _text(asset.get("lib_type"), "knowledge") != "knowledge":
        return True
    credibility = _float(asset.get("credibility"), 0.0) or 0.0
    sources = _source_ids(asset)
    allowed_source_ids = {
        parsed
        for raw in _as_list(snapshot.get("sources", snapshot.get("source_documents", [])))
        for parsed in [_int(raw.get("id", raw.get("source_document_id")) if isinstance(raw, Mapping) else raw)]
        if parsed is not None
    }
    if allowed_source_ids:
        sources = [source for source in sources if source in allowed_source_ids]
    return credibility >= 3 and bool(sources)


def _trusted_assets(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in _asset_rows(snapshot) if _asset_is_trusted(row, snapshot)]


def _asset_ref(asset: Mapping[str, Any], claim: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "evidence_type": "asset",
        "asset_id": _int(asset.get("id", asset.get("asset_id"))),
        "claim": claim or _text(asset.get("title"), "当前快照资产"),
    }
    sources = _source_ids(asset)
    if sources:
        result["source_document_id"] = sources[0]
    return result


def _reference_assets(snapshot: Mapping[str, Any], count: int = 3) -> list[dict[str, Any]]:
    trusted = _trusted_assets(snapshot)
    knowledge = [row for row in trusted if row.get("lib_type") == "knowledge"]
    other = [row for row in trusted if row.get("lib_type") != "knowledge"]
    selected = (knowledge + other)[:count]
    return [_asset_ref(row) for row in selected]


def _snapshot_for_prompt(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """只把必要且已隔离的轻量快照交给模型。"""

    profile = dict(_profile(snapshot))
    assets = _asset_rows(snapshot)
    places = _place_rows(snapshot)
    assessment = snapshot.get("assessment", snapshot.get("assessment_report", {}))
    memories = snapshot.get("active_memories", snapshot.get("long_term_memory", []))
    return {
        "blogger_id": _blogger_id(snapshot),
        "profile": profile,
        "assessment": assessment,
        "assets": [
            {
                "id": row["id"],
                "blogger_id": row.get("blogger_id", _blogger_id(snapshot)),
                "lib_type": row.get("lib_type"),
                "category": row.get("category"),
                "title": row.get("title"),
                "content": _text(row.get("content"))[:1000],
                "credibility": row.get("credibility"),
                "source_document_ids": _source_ids(row),
            }
            for row in assets[:MAX_PROMPT_ASSETS]
        ],
        "places": [
            {
                "id": row["id"],
                "blogger_id": row.get("blogger_id", _blogger_id(snapshot)),
                "name": row.get("name"),
                "category": row.get("category"),
                "location": row.get("location"),
                "specialty": row.get("specialty"),
                "tags": row.get("tags", row.get("tags_json", [])),
                # 商业数据是输入事实；不存在时保持 null，不向模型暗示 0。
                "like_level": row.get("like_level"),
                "est_cost": row.get("est_cost"),
                "est_benefit": row.get("est_benefit"),
                "fits_koc": row.get("fits_koc"),
                "fits_shoot": row.get("fits_shoot"),
            }
            for row in places[:MAX_PROMPT_PLACES]
        ],
        "task_memory": snapshot.get("task_memory", snapshot.get("short_term_memory", {})),
        "active_memories": _as_list(memories)[:MAX_PROMPT_MEMORIES],
        "user_instruction": snapshot.get("user_instruction", ""),
    }


def _parse_json_content(content: Any, *, request_id: str | None = None) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise OutputAgentError("OUTPUT_INVALID_JSON", "模型响应不是 JSON 对象", request_id=request_id)
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise OutputAgentError("OUTPUT_INVALID_JSON", "模型响应无法解析为 JSON", request_id=request_id) from None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise OutputAgentError("OUTPUT_INVALID_JSON", "模型响应 JSON 结构损坏", request_id=request_id) from None
    if not isinstance(payload, Mapping):
        raise OutputAgentError("OUTPUT_INVALID_JSON", "模型响应顶层必须是 JSON 对象", request_id=request_id)
    return dict(payload)


def _normalise_script(payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.setdefault("type", "script")
    result.setdefault("output_type", "script")
    result.setdefault("category", _content_category(snapshot))
    result.setdefault("style", _style(snapshot))
    result.setdefault("platform", _platform(snapshot))
    result.setdefault("tags", [_content_category(snapshot), _style(snapshot)])
    result.setdefault("source_refs", result.get("evidence_refs", []))
    return result


def _normalise_storyboard(
    payload: Mapping[str, Any], snapshot: Mapping[str, Any], script: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(payload)
    result.setdefault("type", "storyboard")
    result.setdefault("output_type", "storyboard")
    if "shots" not in result:
        result["shots"] = result.get("storyboard", result.get("items", []))
    result.setdefault("script_id", script.get("output_id", script.get("id", script.get("script_id"))))
    result.setdefault("script_version", script.get("version", script.get("script_version", 1)))
    result.setdefault("source_refs", result.get("evidence_refs", script.get("source_refs", [])))
    result.setdefault("platform", script.get("platform", _platform(snapshot)))
    return result


def _normalise_schedule(
    payload: Mapping[str, Any], snapshot: Mapping[str, Any], output: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(payload)
    result.setdefault("type", "schedule")
    result.setdefault("output_type", "schedule")
    if "items" not in result:
        result["items"] = result.get("schedules", result.get("entries", []))
    if not result["items"]:
        result["items"] = [
            {
                "plan_date": date.today().isoformat(),
                "platform": _platform(snapshot),
                "content_type": _text(output.get("type", output.get("output_type")), "script"),
                "title": _text(output.get("title"), "贵州文旅内容"),
            }
        ]
    return result


def _normalise_route(payload: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.setdefault("type", "route_rec")
    result.setdefault("output_type", "route_rec")
    if "stops" not in result:
        result["stops"] = result.get("places", result.get("items", []))
    return result


def _normalise_generated(
    kind: str,
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    script: Mapping[str, Any] | None = None,
    output: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if kind == "script":
        return _normalise_script(payload, snapshot)
    if kind == "storyboard":
        return _normalise_storyboard(payload, snapshot, script or {})
    if kind == "schedule":
        return _normalise_schedule(payload, snapshot, output or {})
    return _normalise_route(payload, snapshot)


def _ensure_generated_structure(kind: str, payload: Mapping[str, Any], request_id: str | None) -> None:
    """在返回服务层前识别“合法 JSON 但结构不完整”，使其进入唯一一次修复。"""

    if kind == "script":
        required_text = ("category", "title", "hook", "body", "ending", "style", "platform")
        missing_fields = [field for field in required_text if not _text(payload.get(field))]
        if not _as_list(payload.get("tags")):
            missing_fields.append("tags")
        if not _as_list(payload.get("source_refs")):
            missing_fields.append("source_refs")
        if missing_fields:
            joined = ", ".join(missing_fields)
            raise OutputAgentError(
                "OUTPUT_INVALID_JSON",
                f"脚本字段缺失或为空: {joined}",
                request_id=request_id,
                missing_fields=missing_fields,
            )
        return
    if kind == "storyboard":
        shots = _as_list(payload.get("shots"))
        required_shot = ("sequence", "visual", "dialogue", "duration", "bgm", "transition", "source_refs")
        if not shots or any(
            not isinstance(shot, Mapping) or any(shot.get(field) in (None, "", []) for field in required_shot)
            for shot in shots
        ):
            raise OutputAgentError("OUTPUT_INVALID_JSON", "分镜镜头结构不完整", request_id=request_id)
        return
    if kind == "schedule" and not _as_list(payload.get("items")):
        raise OutputAgentError("OUTPUT_INVALID_JSON", "排期缺少 items", request_id=request_id)
    if kind == "route_rec" and not _as_list(payload.get("stops")):
        raise OutputAgentError("OUTPUT_INVALID_JSON", "路线缺少 stops", request_id=request_id)


def _repair_requirement(kind: str) -> str:
    requirements = {
        "script": (
            "返回且仅返回对象，必须包含 category/title/hook/body/ending/tags/style/platform/source_refs；"
            "tags、source_refs 为非空数组，source_refs 只能使用快照中的真实 asset_id/source_document_id"
        ),
        "storyboard": (
            "返回 script_id/script_version/shots；每个 shot 必须包含 sequence/visual/dialogue/duration/"
            "bgm/transition/source_refs"
        ),
        "schedule": "返回非空 items，每项包含 plan_date/platform/content_type/title",
        "route_rec": "返回非空 stops，每项只引用快照内 place_id；不要补造商业字段",
    }
    return requirements.get(kind, "返回完整合法 JSON")


def _output_contract(kind: str) -> dict[str, Any]:
    """返回首轮模型必须遵守的显式结构契约，不生成任何业务文案。"""

    if kind != "script":
        return {"requirement": _repair_requirement(kind)}
    required_fields = [
        "category",
        "title",
        "hook",
        "body",
        "ending",
        "tags",
        "style",
        "platform",
        "source_refs",
    ]
    text_property = {"type": "string", "non_empty": True}
    return {
        "type": "object",
        "required_fields": required_fields,
        "additional_properties": True,
        "properties": {
            "category": dict(text_property),
            "title": dict(text_property),
            "hook": dict(text_property),
            "body": dict(text_property),
            "ending": dict(text_property),
            "tags": {"type": "array", "non_empty": True, "items": "non-empty string"},
            "style": dict(text_property),
            "platform": dict(text_property),
            "source_refs": {
                "type": "array",
                "non_empty": True,
                "evidence_only": True,
                "items": "object containing a snapshot asset_id and optional source_document_id",
            },
        },
        "constraints": [
            "所有必填字段必须显式出现在响应中，不得省略 hook/body/ending",
            "source_refs 只能引用 input_snapshot 中存在且允许使用的证据",
            "不要输出 Markdown 代码围栏或 JSON 之外的解释",
        ],
    }


class _IdempotentOutputAgent:
    def __init__(self) -> None:
        self._result_cache: dict[str, dict[str, Any]] = {}

    def _cached(
        self,
        kind: str,
        context_messages: list[dict[str, str]],
        snapshot: Mapping[str, Any],
        request_id: str | None,
        factory: Callable[[str], dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(context_messages, list):
            raise OutputAgentError("OUTPUT_CONTEXT_INVALID", "Agent上下文必须是消息列表", request_id=request_id)
        key = f"{kind}:{request_id or self._request_id(kind, context_messages, snapshot)}"
        if key in self._result_cache:
            return copy.deepcopy(self._result_cache[key])
        result = factory(key)
        self._result_cache[key] = copy.deepcopy(result)
        return result

    @staticmethod
    def _request_id(kind: str, context_messages: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any]) -> str:
        raw = json.dumps(
            {"kind": kind, "context": list(context_messages), "snapshot": snapshot.get("snapshot_hash")},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FakeOutputAgent(_IdempotentOutputAgent):
    """离线确定性 Agent；不会调用网络，也不会创造快照外实体。"""

    def __init__(
        self,
        *,
        response: Mapping[str, Any] | None = None,
        fail_with: OutputAgentError | None = None,
    ) -> None:
        super().__init__()
        self.response = dict(response) if response is not None else None
        self.fail_with = fail_with
        self.call_count = 0
        self.model_name = "fake-output-agent"

    def _custom(self, kind: str) -> dict[str, Any] | None:
        if self.response is None:
            return None
        # 支持 {script: {...}}、{storyboard: {...}} 和直接传入单类型 payload。
        value = self.response.get(kind)
        if isinstance(value, Mapping):
            return dict(value)
        if kind == "storyboard":
            value = self.response.get("shots")
            if isinstance(value, list):
                return {"shots": value}
        return dict(self.response)

    def _before_call(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.call_count += 1

    def generate_script(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        del user_instruction

        def factory(_key: str) -> dict[str, Any]:
            self._before_call()
            custom = self._custom("script")
            if custom is not None:
                return _normalise_script(custom, input_snapshot)
            refs = _reference_assets(input_snapshot)
            knowledge = next((ref for ref in refs if ref.get("asset_id") is not None), None)
            title = _text(knowledge.get("claim") if knowledge else None, "贵州文旅可信知识分享")
            return {
                "type": "script",
                "output_type": "script",
                "category": _content_category(input_snapshot),
                "title": f"{title}：{_style(input_snapshot)}体验",
                "hook": f"你知道贵州{_content_category(input_snapshot)}还有这样的故事吗？",
                "body": f"围绕可信资产“{title}”组织{_style(input_snapshot)}内容，只陈述已有来源事实。",
                "ending": "如果想继续了解，请关注后续贵州文旅内容。",
                "tags": [_content_category(input_snapshot), "贵州文旅"],
                "style": _style(input_snapshot),
                "platform": _platform(input_snapshot),
                "source_refs": refs[:2],
            }

        return self._cached("script", context_messages, input_snapshot, request_id, factory)

    def generate_storyboard(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        script: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        def factory(_key: str) -> dict[str, Any]:
            self._before_call()
            custom = self._custom("storyboard")
            if custom is not None:
                return _normalise_storyboard(custom, input_snapshot, script)
            refs = _as_list(script.get("source_refs")) or _reference_assets(input_snapshot, 1)
            return {
                "type": "storyboard",
                "output_type": "storyboard",
                "script_id": script.get("output_id", script.get("id", script.get("script_id"))),
                "script_version": script.get("version", script.get("script_version", 1)),
                "platform": _text(script.get("platform"), _platform(input_snapshot)),
                "source_refs": copy.deepcopy(refs),
                "shots": [
                    {
                        "sequence": 1,
                        "visual": "贵州场景与主题标题，使用真实素材或拍摄画面",
                        "dialogue": _text(script.get("hook"), "今天分享一个贵州文旅知识"),
                        "duration": 3,
                        "bgm": "轻快民族风（待确认）",
                        "transition": "开场淡入",
                        "source_refs": copy.deepcopy(refs),
                    },
                    {
                        "sequence": 2,
                        "visual": "展示可信知识对应的真实内容",
                        "dialogue": _text(script.get("body"), "只依据已核验内容进行介绍"),
                        "duration": 8,
                        "bgm": "轻快民族风（待确认）",
                        "transition": "自然切换",
                        "source_refs": copy.deepcopy(refs),
                    },
                ],
            }

        return self._cached("storyboard", context_messages, input_snapshot, request_id, factory)

    def generate_schedule(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        output: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        def factory(_key: str) -> dict[str, Any]:
            self._before_call()
            custom = self._custom("schedule")
            if custom is not None:
                return _normalise_schedule(custom, input_snapshot, output)
            return _normalise_schedule({}, input_snapshot, output)

        return self._cached("schedule", context_messages, input_snapshot, request_id, factory)

    def generate_route(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        def factory(_key: str) -> dict[str, Any]:
            self._before_call()
            custom = self._custom("route_rec")
            if custom is not None:
                return _normalise_route(custom, input_snapshot)
            places = _place_rows(input_snapshot)
            return {
                "type": "route_rec",
                "output_type": "route_rec",
                "stops": [
                    {"place_id": row["id"], "sequence": index, "claim": _text(row.get("name"))}
                    for index, row in enumerate(places[:5], start=1)
                ],
                "source_refs": _reference_assets(input_snapshot, 1),
                "summary": "仅基于当前博主未删除地点生成候选路线；商业数据不足时需人工补充。",
            }

        return self._cached("route_rec", context_messages, input_snapshot, request_id, factory)


class DeepSeekOutputAgent(_IdempotentOutputAgent):
    """生产 OutputAgent，默认使用 ``deepseek-v4-flash``。"""

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

    def _api_key(self, request_id: str | None) -> str:
        key_file = settings.deepseek_key_file
        if not key_file.exists():
            raise OutputAgentError("DEEPSEEK_KEY_NOT_FOUND", "DeepSeek key 文件不存在", request_id=request_id)
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise OutputAgentError("DEEPSEEK_KEY_EMPTY", "DeepSeek key 文件为空", request_id=request_id)
        return key

    def _call(self, api_key: str, prompt: Mapping[str, Any], request_id: str | None) -> Any:
        try:
            response = self._post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是黔衣有话内容执行助手，只能引用输入快照的资产和地点；"
                                "不得编造价格、成本、收益、店铺、平台数据或跨博主事实。只输出 JSON。"
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
        except httpx.TimeoutException as exc:
            raise OutputAgentError(
                "AGENT_TIMEOUT", "内容 Agent 请求超时", retryable=True, request_id=request_id
            ) from exc
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise OutputAgentError(
                "AGENT_REQUEST_FAILED",
                f"内容 Agent 请求失败：{exc.__class__.__name__}",
                retryable=True,
                request_id=request_id,
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OutputAgentError(
                "OUTPUT_INVALID_JSON", "DeepSeek 响应缺少合法 choices/message/content 结构", request_id=request_id
            ) from exc

    def _generate(
        self,
        kind: str,
        context_messages: list[dict[str, str]],
        snapshot: Mapping[str, Any],
        *,
        request_id: str | None,
        instruction: str,
        script: Mapping[str, Any] | None = None,
        output: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        def factory(_key: str) -> dict[str, Any]:
            api_key = self._api_key(request_id)
            prompt: dict[str, Any] = {
                "task": kind,
                "user_instruction": instruction,
                "context_messages": [dict(message) for message in context_messages],
                "input_snapshot": _snapshot_for_prompt(snapshot),
                "output_contract": _output_contract(kind),
                "rules": [
                    "只使用当前博主快照中的未删除资产、地点和长期记忆",
                    "知识事实必须引用可信来源；source_refs 必须返回 asset_id 或 source_document_id",
                    "不得引用低可信或无来源知识资产，不得编造商业数据",
                    "第三阶段不实现真实发布、反馈学习、画像回写或经营报告",
                ],
            }
            if script is not None:
                prompt["script"] = dict(script)
            if output is not None:
                prompt["output"] = dict(output)
            first_content = self._call(api_key, prompt, request_id)
            self.call_count += 1
            try:
                parsed = _parse_json_content(first_content, request_id=request_id)
                normalized = _normalise_generated(
                    kind, parsed, snapshot, script=script, output=output
                )
                _ensure_generated_structure(kind, normalized, request_id)
            except OutputAgentError as first_error:
                if first_error.code != "OUTPUT_INVALID_JSON":
                    raise
                repair_prompt = {
                    "task": f"修复上一条 {kind} 输出为合法且字段完整的 JSON",
                    "invalid_response": str(first_content)[:20000],
                    "input_snapshot": _snapshot_for_prompt(snapshot),
                    "required": _repair_requirement(kind),
                    "validation_error": first_error.message,
                    "missing_fields": first_error.missing_fields,
                    "repair_instruction": (
                        "保留所有已经合法的字段和值；只修复校验错误并补齐 missing_fields，"
                        "不要重写已有事实，不要添加快照外证据或未经确认的业务数据。"
                    ),
                }
                if script is not None:
                    repair_prompt["script"] = dict(script)
                if output is not None:
                    repair_prompt["output"] = dict(output)
                repaired_content = self._call(api_key, repair_prompt, request_id)
                self.call_count += 1
                try:
                    parsed = _parse_json_content(repaired_content, request_id=request_id)
                    normalized = _normalise_generated(
                        kind, parsed, snapshot, script=script, output=output
                    )
                    _ensure_generated_structure(kind, normalized, request_id)
                except OutputAgentError as repaired_error:
                    raise OutputAgentError(
                        "OUTPUT_INVALID_JSON",
                        f"输出 JSON 在一次修复后仍不合法: {repaired_error.message}",
                        request_id=request_id,
                        missing_fields=repaired_error.missing_fields,
                    ) from repaired_error
            return normalized

        return self._cached(kind, context_messages, snapshot, request_id, factory)

    def generate_script(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._generate(
            "script", context_messages, input_snapshot, request_id=request_id, instruction=user_instruction
        )

    def generate_storyboard(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        script: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._generate(
            "storyboard",
            context_messages,
            input_snapshot,
            request_id=request_id,
            instruction="根据给定脚本生成分镜，不改变脚本事实",
            script=script,
        )

    def generate_schedule(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        output: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._generate(
            "schedule",
            context_messages,
            input_snapshot,
            request_id=request_id,
            instruction="根据博主更新频率提出排期建议；日期和平台必须可被后端校验",
            output=output,
        )

    def generate_route(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._generate(
            "route_rec",
            context_messages,
            input_snapshot,
            request_id=request_id,
            instruction="只解释后端已经排序的地点路线，不自行计算或编造商业收益",
        )


ProductionOutputAgent = DeepSeekOutputAgent

__all__ = [
    "DeepSeekOutputAgent",
    "FakeOutputAgent",
    "OUTPUT_TYPES",
    "OutputAgent",
    "OutputAgentError",
    "OutputAgentProtocol",
    "OutputGenerationError",
    "ProductionOutputAgent",
]

# 兼容服务层常用的异常/协议命名；实现仍只有一个错误字段协议。
OutputAgentProtocol = OutputAgent
OutputGenerationError = OutputAgentError

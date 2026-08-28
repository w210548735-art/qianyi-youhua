"""第四阶段反馈分析 Agent。

Agent 只消费后端生成的冻结快照并提出候选，不读取数据库，也不应用画像、
资产、地点或三库变更。所有引用和数值边界都由
``FeedbackValidationService`` 在落候选前再次校验。
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
PROMPT_VERSION = "phase4-feedback-v1"
MAX_PROMPT_ASSETS = 80
MAX_PROMPT_PLACES = 50
MAX_PROMPT_MEMORIES = 20


class FeedbackAgentError(RuntimeError):
    """反馈 Agent 的稳定、可观测错误。"""

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


class FeedbackAgent(Protocol):
    """可注入反馈 Agent 协议。"""

    prompt_version: str
    model_name: str

    def analyze(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        ...


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value).strip()


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _deleted(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _blogger_id(snapshot: Mapping[str, Any]) -> int | None:
    profile = _mapping(snapshot.get("profile"))
    return _integer(snapshot.get("blogger_id", profile.get("id")))


def _eligible_rows(snapshot: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    expected = _blogger_id(snapshot)
    result: list[dict[str, Any]] = []
    for raw in _rows(snapshot.get(key)):
        row = dict(raw)
        owner = _integer(row.get("blogger_id"))
        if expected is not None and owner is not None and owner != expected:
            continue
        if _deleted(row.get("deleted_at")):
            continue
        if key == "assets":
            credibility = row.get("credibility")
            source_ids = row.get("source_document_ids", [])
            try:
                weak = credibility is not None and float(credibility) < 3
            except (TypeError, ValueError):
                weak = True
            if weak and not source_ids:
                continue
        result.append(row)
    return result


def _prompt_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """只透传确定性分析、冻结链路和当前任务记忆。"""

    output = dict(_mapping(snapshot.get("output")))
    metric = dict(_mapping(snapshot.get("primary_metric")))
    profile = dict(_mapping(snapshot.get("profile")))
    expected = _blogger_id(snapshot)
    if (
        expected is None
        or _integer(profile.get("id")) != expected
        or _integer(output.get("blogger_id")) != expected
        or _integer(metric.get("output_id")) != _integer(output.get("id"))
        or _deleted(output.get("deleted_at"))
    ):
        raise FeedbackAgentError("FEEDBACK_SNAPSHOT_INVALID", "冻结快照归属或 Output/Metric 链路无效")
    return {
        "blogger_id": _blogger_id(snapshot),
        "snapshot_hash": snapshot.get("snapshot_hash"),
        "profile": profile,
        "output": output,
        "primary_metric": metric,
        "deterministic_analysis": copy.deepcopy(_mapping(snapshot.get("deterministic_analysis"))),
        "assets": _eligible_rows(snapshot, "assets")[:MAX_PROMPT_ASSETS],
        "places": _eligible_rows(snapshot, "places")[:MAX_PROMPT_PLACES],
        "evidence_whitelist": [dict(item) for item in _rows(snapshot.get("evidence_whitelist"))],
        "task_memory": copy.deepcopy(_mapping(snapshot.get("task_memory"))),
        "active_memories": _eligible_rows(snapshot, "active_memories")[:MAX_PROMPT_MEMORIES],
        "user_confirmed_place_updates": copy.deepcopy(
            _mapping(snapshot.get("user_confirmed_place_updates"))
        ),
    }


def _parse_json(content: Any, request_id: str | None) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "模型响应不是 JSON 对象", request_id=request_id
        )
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise FeedbackAgentError(
                "FEEDBACK_INVALID_JSON", "模型响应无法解析为 JSON", request_id=request_id
            ) from None
        try:
            payload = json.loads(value[start : end + 1])
        except json.JSONDecodeError:
            raise FeedbackAgentError(
                "FEEDBACK_INVALID_JSON", "模型响应 JSON 结构损坏", request_id=request_id
            ) from None
    if not isinstance(payload, Mapping):
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "模型响应顶层必须是 JSON 对象", request_id=request_id
        )
    return dict(payload)


_LIST_FIELDS = (
    "suit_type_candidates",
    "knowledge_focus_candidates",
    "pitfalls",
    "asset_effects",
    "place_effects",
    "library_evolution",
    "main_direction_candidates",
)


def _ensure_structure(payload: Mapping[str, Any], request_id: str | None) -> None:
    if not _text(payload.get("summary")):
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "反馈输出缺少 summary", request_id=request_id
        )
    data_quality = _mapping(payload.get("data_quality"))
    if _text(data_quality.get("status")) not in {"ok", "data_insufficient"}:
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "反馈输出缺少合法 data_quality.status", request_id=request_id
        )
    for field in _LIST_FIELDS:
        value = payload.get(field)
        if not isinstance(value, list):
            raise FeedbackAgentError(
                "FEEDBACK_INVALID_JSON", f"反馈输出字段 {field} 必须是数组", request_id=request_id
            )
        for item in value:
            if not isinstance(item, Mapping) or not _text(item.get("reason")):
                raise FeedbackAgentError(
                    "FEEDBACK_INVALID_JSON", f"反馈候选 {field} 缺少 reason", request_id=request_id
                )
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                raise FeedbackAgentError(
                    "FEEDBACK_INVALID_JSON", f"反馈候选 {field} 缺少 evidence_refs", request_id=request_id
                )
    if data_quality.get("status") == "data_insufficient" and not _text(
        payload.get("insufficient_reason")
    ):
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "样本不足时必须说明 insufficient_reason", request_id=request_id
        )


def _ensure_quality_matches(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    request_id: str | None,
) -> None:
    expected = _text(_mapping(snapshot.get("deterministic_analysis")).get("overall_status"))
    actual = _text(_mapping(payload.get("data_quality")).get("status"))
    if expected in {"ok", "data_insufficient"} and actual != expected:
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON",
            "data_quality.status 必须逐字复制后端确定性预分析状态",
            request_id=request_id,
        )


def _ensure_library_targets(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    request_id: str | None,
) -> None:
    if _text(_mapping(payload.get("data_quality")).get("status")) == "data_insufficient":
        return
    assets = {
        _integer(item.get("id")): item
        for item in _eligible_rows(snapshot, "assets")
        if _integer(item.get("id")) is not None
    }
    covered: set[str] = set()
    for item in _rows(payload.get("library_evolution")):
        lib_type = _text(item.get("lib_type"))
        action = _text(item.get("action"))
        target_id = _integer(item.get("target_asset_id"))
        candidate = _mapping(item.get("candidate"))
        if lib_type not in LIBRARY_TYPES or action not in {"add", "reinforce", "review"}:
            raise FeedbackAgentError(
                "FEEDBACK_INVALID_JSON", "三库候选类型或 action 非法", request_id=request_id
            )
        if action == "add":
            if target_id is not None or any(
                not _text(candidate.get(field)) for field in ("category", "title", "content")
            ):
                raise FeedbackAgentError(
                    "FEEDBACK_INVALID_JSON",
                    "add 候选必须无 target 且含完整 candidate",
                    request_id=request_id,
                )
        else:
            target = assets.get(target_id)
            if target is None or _text(target.get("lib_type")) != lib_type:
                raise FeedbackAgentError(
                    "FEEDBACK_INVALID_JSON",
                    "reinforce/review 必须引用同库冻结资产",
                    request_id=request_id,
                )
        covered.add(lib_type)
    if covered != set(LIBRARY_TYPES):
        raise FeedbackAgentError(
            "FEEDBACK_INVALID_JSON", "样本充分时三库进化必须覆盖三个库", request_id=request_id
        )


class _IdempotentFeedbackAgent:
    prompt_version = PROMPT_VERSION

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self.last_request_id: str | None = None

    def _cache_key(
        self,
        context_messages: Sequence[Mapping[str, str]],
        snapshot: Mapping[str, Any],
        instruction: str,
        request_id: str | None,
    ) -> str:
        # 即使调用方给了幂等 request_id，也必须先做最小归属校验，避免把
        # 跨博主快照绕过提示词过滤后送入 Fake/生产 Agent。
        prompt_snapshot = _prompt_snapshot(snapshot)
        if request_id:
            return request_id
        serialized = json.dumps(
            {
                "context": list(context_messages),
                "snapshot": prompt_snapshot,
                "instruction": instruction,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _cached(
        self,
        context_messages: list[dict[str, str]],
        snapshot: Mapping[str, Any],
        instruction: str,
        request_id: str | None,
        factory: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        key = self._cache_key(context_messages, snapshot, instruction, request_id)
        self.last_request_id = request_id
        if key not in self._cache:
            self._cache[key] = copy.deepcopy(factory())
        return copy.deepcopy(self._cache[key])


def _ref(evidence_type: str, ref_id: Any) -> str:
    return f"{evidence_type}:{ref_id}"


def _output_contract(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """给真实模型的严格 JSON 形状；示例只含结构，不提供业务结论或数值。"""

    analysis = _mapping(snapshot.get("deterministic_analysis"))
    status = _text(analysis.get("overall_status"), "data_insufficient")
    metric = _mapping(snapshot.get("primary_metric"))
    output = _mapping(snapshot.get("output"))
    refs = [_ref("metric", metric.get("id")), _ref("output", output.get("id"))]
    if status == "data_insufficient":
        return {
            "data_quality": {"status": "data_insufficient"},
            "suit_type_candidates": [],
            "knowledge_focus_candidates": [],
            "pitfalls": [],
            "asset_effects": [],
            "place_effects": [],
            "library_evolution": [],
            "main_direction_candidates": [],
            "insufficient_reason": "说明后端确定性预分析为何判定样本不足",
            "summary": "不含新增数字的总结",
        }
    assets = _eligible_rows(snapshot, "assets")
    library_items: list[dict[str, Any]] = []
    for lib_type in sorted(LIBRARY_TYPES):
        target = next((item for item in assets if item.get("lib_type") == lib_type), None)
        if target is None:
            library_items.append(
                {
                    "lib_type": lib_type,
                    "action": "add",
                    "target_asset_id": None,
                    "candidate": {
                        "category": "基于当前产出类别的候选分类",
                        "title": f"{lib_type} 待确认候选",
                        "content": "不含新增数字或商业事实的待确认候选内容",
                    },
                    "reason": "必须基于确定性预分析",
                    "evidence_refs": refs,
                    "simulation_only": False,
                }
            )
        else:
            library_items.append(
                {
                    "lib_type": lib_type,
                    "action": "reinforce",
                    "target_asset_id": target.get("id"),
                    "candidate": {},
                    "reason": "必须基于确定性预分析",
                    "evidence_refs": [*refs, _ref("asset", target.get("id"))],
                    "simulation_only": False,
                }
            )
    return {
        "data_quality": {"status": status},
        "suit_type_candidates": [
            {
                "value": "候选方向",
                "reason": "必须基于确定性预分析",
                "evidence_refs": refs,
                "simulation_only": False,
            }
        ],
        "knowledge_focus_candidates": [],
        "pitfalls": [
            {
                "pitfall": "待规避事项",
                "reason": "必须基于确定性预分析",
                "evidence_refs": refs,
                "simulation_only": False,
            }
        ],
        "asset_effects": [],
        "place_effects": [],
        "library_evolution": library_items,
        "main_direction_candidates": [],
        "insufficient_reason": "仅 data_insufficient 时必填；此时所有数组必须为空",
        "summary": "不含新增数字的总结",
    }


class FakeFeedbackAgent(_IdempotentFeedbackAgent):
    """离线、确定性的反馈候选生成器。"""

    def __init__(
        self,
        response: Mapping[str, Any] | None = None,
        *,
        fail_with: FeedbackAgentError | None = None,
    ) -> None:
        super().__init__()
        self.response = dict(response) if response is not None else None
        self.fail_with = fail_with
        self.call_count = 0
        self.model_name = "fake-feedback-agent"

    def analyze(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        def factory() -> dict[str, Any]:
            if self.fail_with is not None:
                raise self.fail_with
            self.call_count += 1
            if self.response is not None:
                return copy.deepcopy(self.response)
            return self._build(input_snapshot)

        return self._cached(
            context_messages, input_snapshot, user_instruction, request_id, factory
        )

    @staticmethod
    def _build(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        analysis = _mapping(snapshot.get("deterministic_analysis"))
        quality = _text(analysis.get("overall_status"), "data_insufficient")
        metric = _mapping(snapshot.get("primary_metric"))
        output = _mapping(snapshot.get("output"))
        metric_id = metric.get("id")
        output_id = output.get("id")
        basic_refs = [_ref("metric", metric_id), _ref("output", output_id)]
        if quality != "ok":
            return {
                "data_quality": {"status": "data_insufficient"},
                "suit_type_candidates": [],
                "knowledge_focus_candidates": [],
                "pitfalls": [],
                "asset_effects": [],
                "place_effects": [],
                "library_evolution": [],
                "main_direction_candidates": [],
                "insufficient_reason": "历史同类样本不足，不能依据单条绝对播放量作方向判断。",
                "summary": "数据不足：仅保留确定性原始事实，不生成可应用候选。",
            }

        simulated = _text(metric.get("source_type")) == "simulated"
        category = _text(output.get("category"), "当前内容方向")
        assets = _eligible_rows(snapshot, "assets")
        places = _eligible_rows(snapshot, "places")
        candidate_flag = {"simulation_only": simulated}
        asset_effects = [
            {
                "asset_id": asset.get("id"),
                "effect": "effective",
                "effect_weight": 0.6,
                "reason": "确定性同类历史比较显示本次表现改善，仅提出效果候选。",
                "evidence_refs": [*basic_refs, _ref("asset", asset.get("id"))],
                **candidate_flag,
            }
            for asset in assets
        ]
        place_effects = [
            {
                "place_id": place.get("id"),
                "commercial_field": "est_benefit",
                "adjust": "hold",
                "before": place.get("est_benefit"),
                "after": None,
                "association_confidence": _text(
                    place.get("association_confidence"), "low"
                ),
                "applicable": False,
                "simulation_only": simulated,
                "reason": "地点关联可回查，但没有用户明确提供商业 after 值，故不产生可写变更。",
                "evidence_refs": [*basic_refs, _ref("place", place.get("id"))],
            }
            for place in places
        ]
        library_evolution: list[dict[str, Any]] = []
        for lib_type in LIBRARY_TYPES:
            target = next((row for row in assets if row.get("lib_type") == lib_type), None)
            if target is None:
                action = "add"
                candidate = {
                    "category": category,
                    "title": f"{category}{lib_type}反馈候选",
                    "content": "待用户确认后再创建的反馈候选，不含新商业数值。",
                }
                refs = list(basic_refs)
            else:
                action = "reinforce"
                candidate = {}
                refs = [*basic_refs, _ref("asset", target.get("id"))]
            library_evolution.append(
                {
                    "lib_type": lib_type,
                    "action": action,
                    "target_asset_id": target.get("id") if target else None,
                    "candidate": candidate,
                    "reason": "同类历史比较支持记录三库进化候选，确认前不修改资产。",
                    "evidence_refs": refs,
                    **candidate_flag,
                }
            )
        return {
            "data_quality": {"status": "ok"},
            "suit_type_candidates": [
                {
                    "value": category,
                    "reason": "基于当前类别相对历史中位数的确定性趋势提出候选。",
                    "evidence_refs": basic_refs,
                    **candidate_flag,
                }
            ],
            "knowledge_focus_candidates": [
                {
                    "value": category,
                    "reason": "将表现改善的内容类别作为待确认知识主攻方向。",
                    "evidence_refs": basic_refs,
                    **candidate_flag,
                }
            ],
            "pitfalls": [
                {
                    "pitfall": "样本窗口变化时需复核，不能把单次波动当长期结论",
                    "reason": "候选仅建立在当前冻结历史窗口上。",
                    "evidence_refs": basic_refs,
                    **candidate_flag,
                }
            ],
            "asset_effects": asset_effects,
            "place_effects": place_effects,
            "library_evolution": library_evolution,
            "main_direction_candidates": [
                {
                    "value": category,
                    "reason": "确定性分类趋势向上，作为用户待确认主攻方向。",
                    "evidence_refs": basic_refs,
                    **candidate_flag,
                }
            ],
            "summary": "已生成待确认反馈候选；分析本身未写回任何业务字段。",
        }


class DeepSeekFeedbackAgent(_IdempotentFeedbackAgent):
    """生产反馈 Agent，使用 settings 中配置的 DeepSeek 模型。"""

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
            raise FeedbackAgentError(
                "DEEPSEEK_KEY_NOT_FOUND", "DeepSeek key 文件不存在", request_id=request_id
            )
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise FeedbackAgentError(
                "DEEPSEEK_KEY_EMPTY", "DeepSeek key 文件为空", request_id=request_id
            )
        return key

    def _call(self, key: str, prompt: Mapping[str, Any], request_id: str | None) -> Any:
        try:
            response = self._post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是贵州文旅反馈候选分析助手。只解释后端确定性事实并提出待确认候选；"
                                "不得生成指标当前值、排名、收益数值或跨博主引用，不得自动应用任何变更。"
                                "只输出 JSON，顶层不得使用 candidate_analysis、result 等包装字段。"
                                "data_quality.status 只能是 ok 或 data_insufficient；字段名必须严格遵守"
                                "输入中的 output_contract，不得使用 insufficient 等近义值。"
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
            return response.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            raise FeedbackAgentError(
                "AGENT_TIMEOUT", "反馈 Agent 请求超时", retryable=True, request_id=request_id
            ) from exc
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise FeedbackAgentError(
                "AGENT_REQUEST_FAILED",
                f"反馈 Agent 请求失败：{exc.__class__.__name__}",
                retryable=True,
                request_id=request_id,
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FeedbackAgentError(
                "FEEDBACK_INVALID_JSON",
                "DeepSeek 响应缺少 choices/message/content",
                request_id=request_id,
            ) from exc

    def analyze(
        self,
        context_messages: list[dict[str, str]],
        input_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        def factory() -> dict[str, Any]:
            key = self._api_key(request_id)
            prompt = {
                "task": "feedback_candidate_analysis",
                "prompt_version": self.prompt_version,
                "context_messages": [dict(item) for item in context_messages],
                "input_snapshot": _prompt_snapshot(input_snapshot),
                "user_instruction": user_instruction,
                "output_contract": _output_contract(input_snapshot),
                "rules": [
                    "每个候选必须有 reason 和 evidence_refs，引用仅限 evidence_whitelist",
                    "输出适合类型、知识主攻方向、踩雷、资产、地点、三库进化和主方向候选",
                    "样本不足时所有判断候选留空并解释，不根据单条绝对播放量下结论",
                    "simulated 方向必须 simulation_only，地点商业 after 必须为 null 且不可应用",
                    "地点名称匹配为低置信候选，不得自动确认为关联",
                    "不生成指标值、图表值、排名或商业金额，不修改冻结快照",
                ],
            }
            content = self._call(key, prompt, request_id)
            self.call_count += 1
            try:
                payload = _parse_json(content, request_id)
                _ensure_structure(payload, request_id)
                _ensure_quality_matches(payload, input_snapshot, request_id)
                _ensure_library_targets(payload, input_snapshot, request_id)
            except FeedbackAgentError as first_error:
                if first_error.code != "FEEDBACK_INVALID_JSON":
                    raise
                repair_prompt = {
                    "task": "repair_feedback_json_once",
                    "invalid_response": str(content)[:20000],
                    "input_snapshot": _prompt_snapshot(input_snapshot),
                    "required": {
                        "list_fields": list(_LIST_FIELDS),
                        "item_fields": ["reason", "evidence_refs"],
                        "other_fields": ["data_quality.status", "summary"],
                    },
                    "output_contract": _output_contract(input_snapshot),
                    "strict_instructions": (
                        "移除所有包装层；完整复制 output_contract 的顶层字段名。"
                        "data_quality.status 必须逐字复制 output_contract 中的值。"
                        "不得省略 summary；不要解释，只返回修复后的 JSON 对象。"
                    ),
                }
                repaired = self._call(key, repair_prompt, request_id)
                self.call_count += 1
                try:
                    payload = _parse_json(repaired, request_id)
                    _ensure_structure(payload, request_id)
                    _ensure_quality_matches(payload, input_snapshot, request_id)
                    _ensure_library_targets(payload, input_snapshot, request_id)
                except FeedbackAgentError as repaired_error:
                    raise FeedbackAgentError(
                        "FEEDBACK_INVALID_JSON",
                        "反馈 JSON 在一次修复后仍不合法或字段不完整",
                        request_id=request_id,
                    ) from repaired_error
            return payload

        return self._cached(
            context_messages, input_snapshot, user_instruction, request_id, factory
        )


ProductionFeedbackAgent = DeepSeekFeedbackAgent
FeedbackAgentProtocol = FeedbackAgent

__all__ = [
    "DeepSeekFeedbackAgent",
    "FakeFeedbackAgent",
    "FeedbackAgent",
    "FeedbackAgentError",
    "FeedbackAgentProtocol",
    "LIBRARY_TYPES",
    "PROMPT_VERSION",
    "ProductionFeedbackAgent",
]

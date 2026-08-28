"""可注入的博主画像 Agent。

画像采集的状态机仍由 API 层负责。本模块只负责将用户自然语言解析成
受限的画像字段，并给出下一条针对性追问，因此不会创建或修改任何数据库
记录。生产实现调用 DeepSeek，测试和离线运行可以注入 :class:`FakeProfileAgent`。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

import httpx

from app.core.config import settings

PROFILE_FIELDS = (
    "name",
    "platform",
    "content_types",
    "style",
    "follower_band",
    "monetization_types",
    "routes",
    "viral_topic",
    "frequency",
)
REQUIRED_PROFILE_FIELDS = PROFILE_FIELDS[:6]
LIST_PROFILE_FIELDS = {"content_types", "monetization_types"}
AMBIGUOUS_ANSWERS = {"不知道", "不清楚", "随便", "都行", "不确定", "暂无", "没有想好"}
PROFILE_QUESTIONS = {
    "name": "请具体告诉我你的博主名称。",
    "platform": "请具体说明你主要在哪个平台创作，例如抖音、小红书、B站或多平台。",
    "content_types": "请具体说明你主要创作的内容方向，可以列出多个，例如贵州美食、非遗或景区。",
    "style": "请具体说明你的主要创作风格，例如口播、vlog或测评。",
    "follower_band": "请具体说明你的粉丝量级，例如1k以下、1k-1万、1万-10万或10万以上。",
    "monetization_types": "请具体说明你目前的变现方式，可以列出多个，例如商单、探店或带货。",
    "routes": "请具体说明你常跑的地区或路线；如果暂无固定路线，请明确告诉我。",
    "viral_topic": "请具体说明最近表现较好的内容主题；如果没有，请明确告诉我。",
    "frequency": "请具体说明你的更新频率，例如日更、周更或不定期。",
}


class ProfileAgentError(RuntimeError):
    """画像 Agent 的可观测、可重试错误。

    ``retryable`` 表明调用方是否可以使用相同的输入重新请求。失败结果
    不会写入幂等缓存，也不会触碰数据库。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True, request_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"{code}: {message}{suffix}")


@dataclass(frozen=True)
class ProfileExtractionResult:
    """一轮画像解析的纯数据结果。

    ``fields`` 是当前画像与本轮抽取结果的合并视图，``extracted_fields``
    仅包含本轮新识别的字段，便于 API 层只更新用户本轮明确表达的内容。
    """

    request_id: str
    fields: dict[str, Any]
    extracted_fields: dict[str, Any]
    missing_fields: tuple[str, ...]
    required_missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[str, ...]
    follow_up_question: str | None
    confidence: dict[str, float]

    @property
    def profile(self) -> dict[str, Any]:
        """兼容调用方常用的 ``profile`` 命名。"""

        return copy.deepcopy(self.fields)

    @property
    def complete(self) -> bool:
        return not self.required_missing_fields and not self.ambiguous_fields

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "fields": copy.deepcopy(self.fields),
            "extracted_fields": copy.deepcopy(self.extracted_fields),
            "missing_fields": list(self.missing_fields),
            "required_missing_fields": list(self.required_missing_fields),
            "ambiguous_fields": list(self.ambiguous_fields),
            "follow_up_question": self.follow_up_question,
            "confidence": dict(self.confidence),
            "complete": self.complete,
        }


class ProfileAgent(Protocol):
    """画像 Agent 的注入协议。

    实现必须按 ``request_id`` 对成功结果幂等；失败不应产生缓存结果。
    ``current_field`` 用于状态机知道本轮回答对应的字段，避免短回答被误
    解析成其他字段。
    """

    def extract(
        self,
        message: str,
        current_profile: Mapping[str, Any] | None = None,
        *,
        request_id: str,
        current_field: str | None = None,
        conversation: Sequence[Mapping[str, str]] | None = None,
    ) -> ProfileExtractionResult:
        ...


class _IdempotentProfileAgent:
    """为生产和 Fake Agent 提供进程内成功结果幂等。"""

    def __init__(self) -> None:
        self._result_cache: dict[str, ProfileExtractionResult] = {}

    def extract(
        self,
        message: str,
        current_profile: Mapping[str, Any] | None = None,
        *,
        request_id: str,
        current_field: str | None = None,
        conversation: Sequence[Mapping[str, str]] | None = None,
    ) -> ProfileExtractionResult:
        normalized_request_id = str(request_id).strip()
        if not normalized_request_id:
            raise ProfileAgentError(
                "PROFILE_REQUEST_ID_REQUIRED",
                "request_id 不能为空",
                retryable=False,
            )
        if normalized_request_id in self._result_cache:
            return copy.deepcopy(self._result_cache[normalized_request_id])
        result = self._extract(
            message,
            current_profile,
            request_id=normalized_request_id,
            current_field=current_field,
            conversation=conversation,
        )
        self._result_cache[normalized_request_id] = copy.deepcopy(result)
        return result

    def _extract(
        self,
        message: str,
        current_profile: Mapping[str, Any] | None,
        *,
        request_id: str,
        current_field: str | None,
        conversation: Sequence[Mapping[str, str]] | None,
    ) -> ProfileExtractionResult:
        raise NotImplementedError


def _split_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value]
    else:
        normalized = str(value).replace("，", ",").replace("、", ",").replace("；", ";")
        values = [item.strip() for item in re.split(r"[,;\n]", normalized)]
    return [item for item in values if item]


def _normalize_field(field: str, value: Any) -> Any:
    if field not in PROFILE_FIELDS:
        return None
    if value is None:
        return None
    if field in LIST_PROFILE_FIELDS:
        values = _split_values(value)
        return values or None
    if isinstance(value, (list, tuple, set)):
        value = "、".join(str(item).strip() for item in value if str(item).strip())
    normalized = str(value).strip()
    if not normalized or normalized in AMBIGUOUS_ANSWERS:
        return None
    return normalized


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() not in AMBIGUOUS_ANSWERS
    if isinstance(value, (list, tuple, set)):
        return any(_present(item) for item in value)
    return True


def _normalise_profile(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in PROFILE_FIELDS:
        if profile and field in profile:
            value = _normalize_field(field, profile[field])
            if value is not None:
                normalized[field] = value
    return normalized


def build_result(
    *,
    request_id: str,
    current_profile: Mapping[str, Any] | None,
    extracted_fields: Mapping[str, Any] | None = None,
    ambiguous_fields: Sequence[str] | None = None,
    follow_up_question: str | None = None,
    confidence: Mapping[str, Any] | None = None,
) -> ProfileExtractionResult:
    """统一清理 Agent 输出并计算缺失字段和追问。

    该函数无副作用，生产实现和 Fake 共用，确保真实模型输出也不会将
    未知字段、空字符串或不受支持的画像结构带入后续数据库事务。
    """

    current = _normalise_profile(current_profile)
    extracted: dict[str, Any] = {}
    for field, value in (extracted_fields or {}).items():
        normalized = _normalize_field(str(field), value)
        if normalized is not None:
            extracted[str(field)] = normalized
    merged = {**current, **extracted}

    ambiguous: list[str] = []
    for field in ambiguous_fields or ():
        normalized_field = str(field).strip()
        if normalized_field in PROFILE_FIELDS and normalized_field not in ambiguous:
            ambiguous.append(normalized_field)
    missing = tuple(field for field in PROFILE_FIELDS if not _present(merged.get(field)))
    required_missing = tuple(field for field in REQUIRED_PROFILE_FIELDS if field in missing)

    # 模糊字段优先追问；随后才按画像字段顺序补齐缺失信息。
    next_field = next((field for field in ambiguous if field in PROFILE_FIELDS), None)
    if next_field is None:
        next_field = next((field for field in missing), None)
    question = (
        follow_up_question.strip()
        if isinstance(follow_up_question, str) and follow_up_question.strip()
        else None
    )
    if next_field and not question:
        question = PROFILE_QUESTIONS[next_field]

    clean_confidence: dict[str, float] = {}
    for field, value in (confidence or {}).items():
        if field not in PROFILE_FIELDS:
            continue
        try:
            score = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
        clean_confidence[field] = score
    for field in extracted:
        clean_confidence.setdefault(field, 1.0)

    return ProfileExtractionResult(
        request_id=request_id,
        fields=merged,
        extracted_fields=extracted,
        missing_fields=missing,
        required_missing_fields=required_missing,
        ambiguous_fields=tuple(ambiguous),
        follow_up_question=question,
        confidence=clean_confidence,
    )


def _parse_json_content(content: Any, *, request_id: str) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ProfileAgentError(
            "PROFILE_AGENT_RESPONSE_INVALID",
            "模型响应不是 JSON 对象或 JSON 字符串",
            retryable=False,
            request_id=request_id,
        )
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 允许模型在 JSON 前后带少量解释，但只截取最外层对象。
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ProfileAgentError(
                "PROFILE_AGENT_RESPONSE_INVALID",
                "模型响应无法解析为 JSON",
                retryable=False,
                request_id=request_id,
            ) from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProfileAgentError(
                "PROFILE_AGENT_RESPONSE_INVALID",
                "模型响应 JSON 结构损坏",
                retryable=False,
                request_id=request_id,
            ) from exc
    if not isinstance(parsed, dict):
        raise ProfileAgentError(
            "PROFILE_AGENT_RESPONSE_INVALID",
            "模型响应顶层必须是 JSON 对象",
            retryable=False,
            request_id=request_id,
        )
    return parsed


def _extract_payload_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("fields", "extracted_fields", "profile", "extracted"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    # 兼容模型直接把字段放在 JSON 顶层的响应。
    return {field: payload[field] for field in PROFILE_FIELDS if field in payload}


class DeepSeekProfileAgent(_IdempotentProfileAgent):
    """生产画像 Agent，默认使用 ``deepseek-v4-flash``。"""

    def __init__(
        self,
        timeout_seconds: float = 60.0,
        *,
        post: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self._post = post or httpx.post
        self.call_count = 0

    def _extract(
        self,
        message: str,
        current_profile: Mapping[str, Any] | None,
        *,
        request_id: str,
        current_field: str | None,
        conversation: Sequence[Mapping[str, str]] | None,
    ) -> ProfileExtractionResult:
        if not message.strip():
            raise ProfileAgentError(
                "PROFILE_MESSAGE_EMPTY",
                "用户画像输入不能为空",
                retryable=False,
                request_id=request_id,
            )
        if current_field is not None and current_field not in PROFILE_FIELDS:
            raise ProfileAgentError(
                "PROFILE_FIELD_INVALID",
                f"不支持的画像字段：{current_field}",
                retryable=False,
                request_id=request_id,
            )
        try:
            key_file = settings.deepseek_key_file
            if not key_file.exists():
                raise ProfileAgentError(
                    "DEEPSEEK_KEY_NOT_FOUND",
                    "DeepSeek key 文件不存在",
                    retryable=False,
                    request_id=request_id,
                )
            api_key = key_file.read_text(encoding="utf-8").strip()
            if not api_key:
                raise ProfileAgentError(
                    "DEEPSEEK_KEY_EMPTY",
                    "DeepSeek key 文件为空",
                    retryable=False,
                    request_id=request_id,
                )
            prompt = {
                "request_id": request_id,
                "current_field": current_field,
                "current_profile": _normalise_profile(current_profile),
                "user_message": message.strip(),
                "conversation": list(conversation or ()),
                "rules": [
                    "只抽取用户明确表达的画像事实，不要猜测或补全",
                    "一次回答中可以抽取多个字段",
                    "不知道、不清楚、随便等回答应放入 ambiguous_fields，不得写入字段",
                    "fields 只允许给定画像字段；列表字段返回字符串数组",
                    "缺失或模糊时只生成一条最有针对性的追问",
                ],
                "allowed_fields": list(PROFILE_FIELDS),
                "output_schema": {
                    "fields": {field: "string|string[]|null" for field in PROFILE_FIELDS},
                    "ambiguous_fields": ["field_name"],
                    "follow_up_question": "string|null",
                    "confidence": {"field_name": "number 0..1"},
                },
            }
            self.call_count += 1
            response = self._post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是严谨的中文博主画像采集助手，只输出合法 JSON，不生成用户未明确表达的事实。",
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
            content = payload["choices"][0]["message"]["content"]
            parsed = _parse_json_content(content, request_id=request_id)
        except ProfileAgentError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise ProfileAgentError(
                "PROFILE_AGENT_REQUEST_FAILED",
                f"DeepSeek 请求失败：{exc.__class__.__name__}",
                retryable=True,
                request_id=request_id,
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProfileAgentError(
                "PROFILE_AGENT_RESPONSE_INVALID",
                "DeepSeek 响应缺少合法 choices/message/content 结构",
                retryable=False,
                request_id=request_id,
            ) from exc
        return build_result(
            request_id=request_id,
            current_profile=current_profile,
            extracted_fields=_extract_payload_fields(parsed),
            ambiguous_fields=parsed.get("ambiguous_fields", parsed.get("uncertain_fields", [])),
            follow_up_question=parsed.get("follow_up_question", parsed.get("next_question")),
            confidence=parsed.get("confidence", {}),
        )


class FakeProfileAgent(_IdempotentProfileAgent):
    """无需网络的画像 Agent，用规则模拟多字段抽取和追问。"""

    _platforms = ("抖音", "小红书", "B站", "哔哩哔哩", "视频号", "快手", "微博")
    _content_types = ("美食", "非遗", "景区", "旅行", "文旅", "民俗", "文化", "穿搭", "摄影")
    _styles = ("口播", "vlog", "Vlog", "测评", "图文", "剧情", "直播")
    _monetization = ("商单", "探店", "带货", "广告", "直播", "课程", "门票")
    _frequencies = ("日更", "周更", "月更", "不定期", "每天", "每周", "每月")

    def __init__(
        self,
        *,
        fail_with: ProfileAgentError | None = None,
        response: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.fail_with = fail_with
        self.response = dict(response) if response is not None else None
        self.call_count = 0

    def _extract(
        self,
        message: str,
        current_profile: Mapping[str, Any] | None,
        *,
        request_id: str,
        current_field: str | None,
        conversation: Sequence[Mapping[str, str]] | None,
    ) -> ProfileExtractionResult:
        del conversation
        if self.fail_with is not None:
            raise self.fail_with
        if not message.strip():
            raise ProfileAgentError(
                "PROFILE_MESSAGE_EMPTY",
                "用户画像输入不能为空",
                retryable=False,
                request_id=request_id,
            )
        self.call_count += 1
        if self.response is not None:
            payload = self.response
            return build_result(
                request_id=request_id,
                current_profile=current_profile,
                extracted_fields=_extract_payload_fields(payload),
                ambiguous_fields=payload.get("ambiguous_fields", []),
                follow_up_question=payload.get("follow_up_question"),
                confidence=payload.get("confidence", {}),
            )

        text = message.strip()
        if text in AMBIGUOUS_ANSWERS:
            return build_result(
                request_id=request_id,
                current_profile=current_profile,
                ambiguous_fields=[current_field] if current_field else [],
                follow_up_question=None,
            )
        extracted: dict[str, Any] = {}
        if current_field in PROFILE_FIELDS:
            explicit = _normalize_field(current_field, text)
            if explicit is not None:
                extracted[current_field] = explicit

        name_match = re.search(r"(?:我叫|我是|博主(?:名|名称)?(?:叫|是)?|名称(?:是|叫)?)[：:\s]*([^，,。；;\s]+)", text)
        if name_match:
            extracted["name"] = name_match.group(1).strip()
        for platform in self._platforms:
            if platform in text:
                extracted.setdefault("platform", "B站" if platform == "哔哩哔哩" else platform)
                break
        content_matches = [item for item in self._content_types if item in text]
        if content_matches:
            extracted.setdefault("content_types", content_matches)
        style_matches = [item for item in self._styles if item in text]
        if style_matches:
            style = style_matches[0].lower() if style_matches[0] in {"Vlog", "vlog"} else style_matches[0]
            extracted.setdefault("style", style)
        follower_pattern = r"(?:粉丝(?:量级|数)?|粉丝有?)[：:\s]*"
        follower_pattern += r"([\d一二三四五六七八九十百千万万以下以上至到\-—~～ ]{2,30})"
        follower_match = re.search(follower_pattern, text)
        if follower_match:
            extracted.setdefault("follower_band", follower_match.group(1).strip().replace("到", "-").replace("至", "-"))
        monetization_matches = [item for item in self._monetization if item in text]
        if monetization_matches:
            extracted.setdefault("monetization_types", monetization_matches)
        routes_match = re.search(r"(?:常跑|路线|地区)(?:是|有|包括)?[：:\s]*([^。；;，,]+)", text)
        if routes_match:
            route = routes_match.group(1).strip()
            if route not in AMBIGUOUS_ANSWERS and route not in {"无", "没有"}:
                extracted.setdefault("routes", route)
        viral_match = re.search(r"(?:爆款|爆过|表现较好(?:的内容)?)(?:是|有|为)?[：:\s]*([^。；;]+)", text)
        if viral_match:
            extracted.setdefault("viral_topic", viral_match.group(1).strip())
        frequency_matches = [item for item in self._frequencies if item in text]
        if frequency_matches:
            frequency = frequency_matches[0]
            frequency_map = {"每天": "日更", "每周": "周更", "每月": "月更"}
            extracted.setdefault("frequency", frequency_map.get(frequency, frequency))

        return build_result(
            request_id=request_id,
            current_profile=current_profile,
            extracted_fields=extracted,
            # 短回答无可识别字段时，视为当前字段模糊，给出一次定向追问。
            ambiguous_fields=[current_field] if current_field and not extracted else [],
        )


# 便于调用方按已有命名习惯注入。
ProfileAgentResult = ProfileExtractionResult
ProductionProfileAgent = DeepSeekProfileAgent

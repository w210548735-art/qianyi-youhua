"""第四阶段经营报告 Agent。

Agent 只负责解释后端已经确定的事实并提出建议，不能生成指标值、图表点、
地点排名或收益数字。所有数字均由 :mod:`report_data_service` 计算。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx

from app.core.config import settings


class ReportAgentError(RuntimeError):
    """报告 Agent 的稳定错误协议。"""

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


class ReportAgent(Protocol):
    """经营报告解释 Agent 协议。"""

    model_name: str

    def generate(
        self,
        deterministic_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]: ...


def _json_object(value: Any, *, request_id: str | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise ReportAgentError("REPORT_INVALID_JSON", "报告 Agent 返回值不是 JSON 对象", request_id=request_id)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReportAgentError("REPORT_INVALID_JSON", "报告 Agent 返回非法 JSON", request_id=request_id) from exc
    if not isinstance(parsed, dict):
        raise ReportAgentError("REPORT_INVALID_JSON", "报告 Agent 顶层必须是对象", request_id=request_id)
    return parsed


def _section_status(snapshot: Mapping[str, Any], category: str) -> str:
    facts = snapshot.get("facts")
    section = facts.get(category) if isinstance(facts, Mapping) else None
    if isinstance(section, Mapping):
        return str(section.get("status") or "data_insufficient")
    return "data_insufficient"


class FakeReportAgent:
    """完全离线且确定性的报告 Agent。"""

    model_name = "fake-report-agent"
    prompt_version = "phase4-report-v1"

    def __init__(
        self,
        *,
        response: Mapping[str, Any] | None = None,
        fail_with: ReportAgentError | None = None,
    ) -> None:
        self.response = dict(response) if response is not None else None
        self.fail_with = fail_with
        self.call_count = 0
        self._cache: dict[str, dict[str, Any]] = {}

    def generate(
        self,
        deterministic_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        del user_instruction
        if self.fail_with is not None:
            raise self.fail_with
        key = (
            request_id
            or hashlib.sha256(
                json.dumps(deterministic_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        )
        if key in self._cache:
            return copy.deepcopy(self._cache[key])
        self.call_count += 1
        if self.response is not None:
            result = copy.deepcopy(self.response)
        else:
            sections: dict[str, dict[str, Any]] = {}
            messages = {
                "money": "实际商业数据完整性决定是否能形成实际盈亏结论。",
                "traffic": "流量表现应结合历史趋势和互动质量持续观察。",
                "product": "内容产出结构应与已确认的主攻方向保持一致。",
                "supplier": "供应商判断只采用可追溯的已确认商业数据。",
            }
            for category, explanation in messages.items():
                sections[category] = {
                    "status": _section_status(deterministic_snapshot, category),
                    "explanation": explanation,
                    "evidence_refs": [f"fact:{category}"],
                }
            result = {
                "sections": sections,
                "suggestions": [
                    {
                        "action": "优先补齐缺失数据并延续已验证方向",
                        "priority": "high",
                        "reason": "经营决策必须建立在可回查的确认数据上。",
                        "evidence_refs": ["data_quality"],
                    }
                ],
                "summary": "报告解释仅基于后端确定性事实，缺失数据不按零处理。",
            }
        self._cache[key] = copy.deepcopy(result)
        return result


class DeepSeekReportAgent:
    """生产报告 Agent；非法 JSON 或缺字段时仅修复一次。"""

    prompt_version = "phase4-report-v1"

    def __init__(
        self,
        timeout_seconds: float = 90.0,
        *,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._post = post or httpx.post
        self.model_name = settings.deepseek_model
        self.call_count = 0

    def _api_key(self, request_id: str | None) -> str:
        if not settings.deepseek_key_file.exists():
            raise ReportAgentError("DEEPSEEK_KEY_NOT_FOUND", "DeepSeek key 文件不存在", request_id=request_id)
        key = settings.deepseek_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ReportAgentError("DEEPSEEK_KEY_EMPTY", "DeepSeek key 文件为空", request_id=request_id)
        return key

    def _call(self, api_key: str, payload: Mapping[str, Any], request_id: str | None) -> Any:
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
                                "你是经营报告解释助手。只能解释输入事实并提出建议；不得新增或修改任何数字、"
                                "指标、地点、排名或经营结论。只输出 JSON。"
                            ),
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            self.call_count += 1
            return response.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            raise ReportAgentError(
                "AGENT_TIMEOUT", "报告 Agent 请求超时", retryable=True, request_id=request_id
            ) from exc
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise ReportAgentError(
                "AGENT_REQUEST_FAILED",
                f"报告 Agent 请求失败：{exc.__class__.__name__}",
                retryable=True,
                request_id=request_id,
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ReportAgentError("REPORT_INVALID_JSON", "DeepSeek 响应结构不完整", request_id=request_id) from exc

    @staticmethod
    def _complete(payload: Mapping[str, Any]) -> bool:
        sections = payload.get("sections")
        if not isinstance(sections, Mapping) or any(
            not isinstance(sections.get(name), Mapping) for name in ("money", "traffic", "product", "supplier")
        ):
            return False
        return isinstance(payload.get("suggestions"), list) and isinstance(payload.get("summary"), str)

    def generate(
        self,
        deterministic_snapshot: Mapping[str, Any],
        user_instruction: str = "",
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        api_key = self._api_key(request_id)
        prompt = {
            "task": "解释经营报告并提出建议",
            "prompt_version": self.prompt_version,
            "request_id": request_id,
            "user_instruction": user_instruction,
            "deterministic_snapshot": dict(deterministic_snapshot),
            "required": {
                "sections": ["money", "traffic", "product", "supplier"],
                "suggestions": "array",
                "summary": "string",
            },
            "rules": [
                "不得输出输入快照不存在的数字、地点、指标或结论",
                "不得自行计算或修改图表数据",
                "data_insufficient 不能解释为零、赚钱或亏损",
                "estimated 不能表述为实际结果",
            ],
        }
        first = self._call(api_key, prompt, request_id)
        try:
            parsed = _json_object(first, request_id=request_id)
            if not self._complete(parsed):
                raise ReportAgentError("REPORT_INVALID_JSON", "报告结构不完整", request_id=request_id)
            return parsed
        except ReportAgentError as first_error:
            if first_error.code != "REPORT_INVALID_JSON":
                raise
        repair = self._call(
            api_key,
            {
                "task": "将上一条报告修复为字段完整的 JSON",
                "invalid_response": str(first)[:20000],
                "required": prompt["required"],
                "deterministic_snapshot": dict(deterministic_snapshot),
                "rules": prompt["rules"],
            },
            request_id,
        )
        try:
            parsed = _json_object(repair, request_id=request_id)
            if not self._complete(parsed):
                raise ReportAgentError("REPORT_INVALID_JSON", "修复后的报告结构不完整", request_id=request_id)
            return parsed
        except ReportAgentError as exc:
            raise ReportAgentError(
                "REPORT_INVALID_JSON", "报告 JSON 在一次修复后仍不合法或字段不完整", request_id=request_id
            ) from exc


ProductionReportAgent = DeepSeekReportAgent

__all__ = [
    "DeepSeekReportAgent",
    "FakeReportAgent",
    "ProductionReportAgent",
    "ReportAgent",
    "ReportAgentError",
]

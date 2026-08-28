"""为 Agent 组装受控上下文。

上下文组装是 Agent 调用前的边界层。它只读取当前任务需要的有限短期记忆，
并把长期记忆检索限制在当前博主下；业务服务不应把数据库中的全部历史直接
拼接到提示词中。
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SessionMessage, TaskCheckpoint, TaskSession

DEFAULT_SYSTEM_RULES = """你是黔衣有话的严谨内容运营助手。
只使用上下文中提供的事实；无法确认的内容必须明确标注未知，不得编造。
当前长期记忆仅供参考，不能跨博主使用。"""


class MemorySearchService(Protocol):
    """长期记忆检索服务的最小协议。

    项目中的实际检索服务可以返回 ORM 对象、字典或带同名属性的 DTO。检索
    方法支持关键字参数 ``blogger_id``、``query`` 和 ``top_k``；上下文服务
    会在调用前检查签名，以兼容简单的测试替身。
    """

    def search(self, *, blogger_id: int, query: str, top_k: int) -> Iterable[Any]: ...


@dataclass(frozen=True)
class RetrievedMemory:
    """可放入提示词的长期记忆摘要。"""

    id: int | str | None
    blogger_id: int
    memory_type: str
    title: str
    content: str
    source_type: str | None = None
    source_id: str | None = None
    confidence: float | None = None
    version: int | None = None
    similarity: float | None = None


@dataclass
class ContextAssembly:
    """一次上下文组装的结果。

    ``messages`` 是按固定顺序排列、可以直接传给 chat completion 的消息列表：

    1. 系统规则；
    2. 当前任务短期记忆（上下文、有限的最近消息、最新检查点）；
    3. 当前博主的相关 active 长期记忆；
    4. 用户本轮输入。

    其余字段用于演示页、日志和测试，不会自动追加到提示词。
    """

    messages: list[dict[str, str]]
    blogger_id: int
    task_id: str | None
    retrieved_memories: list[RetrievedMemory] = field(default_factory=list)
    memory_search_error: str | None = None
    task_missing: bool = False

    @property
    def recalled_memories(self) -> list[RetrievedMemory]:
        """兼容 ``recalled_memories`` 命名。"""

        return self.retrieved_memories

    @property
    def short_term_message(self) -> dict[str, str]:
        """返回短期记忆消息，便于演示页单独展示。"""

        return self.messages[1]

    @property
    def long_term_message(self) -> dict[str, str]:
        """返回长期记忆消息，便于演示页单独展示。"""

        return self.messages[2]

    def as_messages(self) -> list[dict[str, str]]:
        """返回副本，防止调用方意外修改已组装的结果。"""

        return [dict(message) for message in self.messages]

    def __iter__(self):
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int) -> dict[str, str]:
        return self.messages[index]


# 让调用方可以用更短的名字导入结果类型。
ContextResult = ContextAssembly


class ContextService:
    """按博主隔离、按任务裁剪上下文的服务。"""

    def __init__(
        self,
        db: Session,
        memory_search: MemorySearchService | Any | None = None,
        *,
        memory_search_service: MemorySearchService | Any | None = None,
        memory_service: MemorySearchService | Any | None = None,
        top_k: int = 5,
        recent_message_limit: int = 6,
        system_rules: str | Sequence[str] | None = None,
    ) -> None:
        injected_services = [
            service for service in (memory_search, memory_search_service, memory_service) if service is not None
        ]
        if len(injected_services) > 1:
            raise ValueError("只能注入一个长期记忆检索服务")
        self.db = db
        self.memory_search = injected_services[0] if injected_services else None
        # 暴露语义化别名，方便 API 层和演示页读取注入的检索器状态。
        self.memory_search_service = self.memory_search
        self.top_k = self._validate_limit(top_k, "top_k", allow_zero=False)
        self.recent_message_limit = self._validate_limit(recent_message_limit, "recent_message_limit", allow_zero=True)
        self.system_rules = self._normalise_system_rules(system_rules)

    def assemble_context(
        self,
        blogger_id: int,
        user_input: str,
        *,
        task_id: str | None = None,
        task_session: TaskSession | Mapping[str, Any] | None = None,
        system_rules: str | Sequence[str] | None = None,
        top_k: int | None = None,
        recent_message_limit: int | None = None,
    ) -> ContextAssembly:
        """组装一次 Agent 调用的完整上下文。

        ``task_session`` 仅用于测试或调用方已经加载任务的场景；如果同时提供
        ``task_id``，会校验二者的 ID。直接传入的任务对象同样必须属于当前
        ``blogger_id``，否则短期记忆会被安全地视为空。
        """

        effective_top_k = self.top_k if top_k is None else self._validate_limit(top_k, "top_k", allow_zero=False)
        effective_recent_limit = (
            self.recent_message_limit
            if recent_message_limit is None
            else self._validate_limit(recent_message_limit, "recent_message_limit", allow_zero=True)
        )
        task = self._resolve_task(task_id, task_session)
        task_matches_blogger = self._task_matches_blogger(task, blogger_id)
        task_missing = task_id is not None and (task is None or not task_matches_blogger)

        short_term = self._build_short_term_text(
            task if task_matches_blogger else None,
            task_id=task_id,
            recent_message_limit=effective_recent_limit,
        )
        retrieval_query = self._retrieval_query(
            task if task_matches_blogger else None,
            user_input,
        )
        memories, memory_error = self._retrieve_memories(
            blogger_id=blogger_id,
            query=retrieval_query,
            top_k=effective_top_k,
        )
        long_term = self._build_long_term_text(memories)

        rules = self.system_rules if system_rules is None else self._normalise_system_rules(system_rules)
        messages = [
            {"role": "system", "content": rules},
            {"role": "system", "content": short_term},
            {"role": "system", "content": long_term},
            {"role": "user", "content": str(user_input)},
        ]
        return ContextAssembly(
            messages=messages,
            blogger_id=blogger_id,
            task_id=task_id,
            retrieved_memories=memories,
            memory_search_error=memory_error,
            task_missing=task_missing,
        )

    # 常用别名，避免不同 Agent 服务在接入时重复包一层。
    def assemble(self, blogger_id: int, user_input: str, **kwargs: Any) -> ContextAssembly:
        return self.assemble_context(blogger_id, user_input, **kwargs)

    def build_context(self, blogger_id: int, user_input: str, **kwargs: Any) -> ContextAssembly:
        return self.assemble_context(blogger_id, user_input, **kwargs)

    def build_messages(self, blogger_id: int, user_input: str, **kwargs: Any) -> list[dict[str, str]]:
        return self.assemble_context(blogger_id, user_input, **kwargs).as_messages()

    build_agent_context = assemble_context

    def _resolve_task(
        self,
        task_id: str | None,
        task_session: TaskSession | Mapping[str, Any] | None,
    ) -> TaskSession | Mapping[str, Any] | None:
        if task_session is not None:
            supplied_id = self._value(task_session, "id")
            if task_id is not None and str(supplied_id) != str(task_id):
                return None
            return task_session
        if task_id is None:
            return None
        return self.db.get(TaskSession, task_id)

    def _task_matches_blogger(self, task: Any, blogger_id: int) -> bool:
        if task is None:
            return False
        task_blogger_id = self._value(task, "blogger_id")
        try:
            return int(task_blogger_id) == int(blogger_id)
        except (TypeError, ValueError):
            return False

    def _build_short_term_text(
        self,
        task: TaskSession | Mapping[str, Any] | None,
        *,
        task_id: str | None,
        recent_message_limit: int,
    ) -> str:
        lines = ["当前任务短期记忆："]
        if task is None:
            lines.append(f"任务：{task_id or '无'}（没有可用的任务记录）")
            lines.append("当前上下文：无")
            lines.append("最近消息：无")
            lines.append("最新检查点摘要：无")
            return "\n".join(lines)

        resolved_task_id = self._value(task, "id") or task_id or "无"
        lines.append(f"任务：{resolved_task_id}")
        lines.append(f"任务类型：{self._value(task, 'task_type') or '未提供'}")
        lines.append(f"当前上下文：{self._text(self._value(task, 'current_context')) or '无'}")

        messages = self._recent_messages(str(resolved_task_id), recent_message_limit)
        lines.append("最近消息：")
        if messages:
            for message in messages:
                role = self._text(self._value(message, "role")) or "unknown"
                content = self._text(self._value(message, "content")) or ""
                sequence = self._value(message, "sequence")
                lines.append(f"[{sequence}] {role}: {content}")
        else:
            lines.append("无")

        checkpoint = self._latest_checkpoint(str(resolved_task_id))
        lines.append("最新检查点摘要：")
        lines.append(self._checkpoint_summary(checkpoint) if checkpoint else "无")
        return "\n".join(lines)

    def _recent_messages(self, task_id: str, limit: int) -> list[SessionMessage]:
        if limit <= 0:
            return []
        rows = list(
            self.db.scalars(
                select(SessionMessage)
                .where(SessionMessage.task_id == task_id)
                .order_by(SessionMessage.sequence.desc())
                .limit(limit)
            )
        )
        rows.reverse()
        return rows

    def _latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        return self.db.scalar(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.task_id == task_id)
            .order_by(TaskCheckpoint.sequence.desc())
            .limit(1)
        )

    def _retrieval_query(self, task: Any, user_input: str) -> str:
        parts: list[str] = []
        if task is not None:
            task_title = self._text(self._value(task, "title"))
            task_context = self._text(self._value(task, "current_context"))
            if task_title:
                parts.append(f"任务：{task_title}")
            if task_context:
                parts.append(f"上下文：{task_context}")
        parts.append(f"本轮输入：{str(user_input)}")
        return "\n".join(parts)

    def _retrieve_memories(
        self,
        *,
        blogger_id: int,
        query: str,
        top_k: int,
    ) -> tuple[list[RetrievedMemory], str | None]:
        if self.memory_search is None or top_k == 0:
            return [], None
        try:
            raw_rows = self._call_memory_search(blogger_id, query, top_k)
            memories: list[RetrievedMemory] = []
            for row in raw_rows or []:
                memory = self._normalise_memory(row, blogger_id)
                if memory is not None:
                    memories.append(memory)
                if len(memories) >= top_k:
                    break
            return memories, None
        except Exception as exc:  # noqa: BLE001 - 记忆失败必须安全降级为空
            # 不把异常文本放进模型上下文；演示页和日志可以通过该字段定位问题。
            return [], f"{exc.__class__.__name__}: {str(exc)[:300]}"

    def _call_memory_search(self, blogger_id: int, query: str, top_k: int) -> Iterable[Any]:
        service = self.memory_search
        method = getattr(service, "search", None)
        if method is None:
            method = getattr(service, "search_memories", None)
        if method is None:
            method = getattr(service, "semantic_search", None)
        if method is None:
            method = getattr(service, "retrieve", None)
        if method is None and callable(service):
            method = service
        if method is None:
            raise TypeError("长期记忆检索服务必须提供 search 或 search_memories")

        # 通过签名适配三种常见测试替身：全关键字、位置参数、只接收 query。
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameters = signature.parameters
            accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            kwargs: dict[str, Any] = {}
            if accepts_kwargs or "blogger_id" in parameters:
                kwargs["blogger_id"] = blogger_id
            if accepts_kwargs or "query" in parameters:
                kwargs["query"] = query
            if accepts_kwargs or "top_k" in parameters:
                kwargs["top_k"] = top_k
            elif "limit" in parameters:
                # 项目内 MemoryService 使用 ``limit`` 命名；对外仍保持
                # ContextService 的 ``top_k`` 语义，避免悄悄使用默认数量。
                kwargs["limit"] = top_k
            if kwargs:
                # 只要服务声明了 blogger_id，就绝不使用没有隔离参数的调用方式。
                if "blogger_id" not in kwargs:
                    raise TypeError("长期记忆检索服务必须接收 blogger_id")
                return method(**kwargs)
            positional = list(parameters.values())
            if len(positional) >= 3:
                return method(blogger_id, query, top_k)
            raise TypeError("长期记忆检索服务签名缺少 blogger_id")
        return method(blogger_id=blogger_id, query=query, top_k=top_k)

    def _normalise_memory(self, row: Any, blogger_id: int) -> RetrievedMemory | None:
        row_blogger_id = self._value(row, "blogger_id")
        # 无法确认归属的结果也丢弃，避免一个不完整的 fake/适配器造成跨博主泄漏。
        try:
            if row_blogger_id is None or int(row_blogger_id) != int(blogger_id):
                return None
        except (TypeError, ValueError):
            return None
        status = self._value(row, "status")
        if status is not None and str(status).lower() != "active":
            return None
        content = self._text(self._value(row, "content"))
        title = self._text(self._value(row, "title"))
        if not content:
            return None
        confidence_value = self._value(row, "confidence")
        confidence: float | None
        try:
            confidence = None if confidence_value is None else float(confidence_value)
        except (TypeError, ValueError):
            confidence = None
        similarity_value = self._value(row, "similarity")
        try:
            similarity = None if similarity_value is None else float(similarity_value)
        except (TypeError, ValueError):
            similarity = None
        return RetrievedMemory(
            id=self._value(row, "id"),
            blogger_id=blogger_id,
            memory_type=self._text(self._value(row, "memory_type")) or "unknown",
            title=title or "未命名记忆",
            content=content,
            source_type=self._text(self._value(row, "source_type")) or None,
            source_id=self._text(self._value(row, "source_id")) or None,
            confidence=confidence,
            version=self._int_or_none(self._value(row, "version")),
            similarity=similarity,
        )

    def _build_long_term_text(self, memories: Sequence[RetrievedMemory]) -> str:
        lines = ["相关长期记忆（仅限当前博主且状态为 active）："]
        if not memories:
            lines.append("无")
            return "\n".join(lines)
        for memory in memories:
            details = [f"类型={memory.memory_type}", f"标题={memory.title}"]
            if memory.confidence is not None:
                details.append(f"置信度={memory.confidence:g}")
            if memory.similarity is not None:
                details.append(f"相关度={memory.similarity:g}")
            if memory.source_type or memory.source_id:
                details.append("来源=" + ":".join(item for item in (memory.source_type, memory.source_id) if item))
            lines.append(f"- {'；'.join(details)}\n  {memory.content}")
        return "\n".join(lines)

    @staticmethod
    def _checkpoint_summary(checkpoint: Any) -> str:
        context_snapshot = ContextService._text(ContextService._value(checkpoint, "context_snapshot"))
        if context_snapshot:
            return context_snapshot
        state_json = ContextService._text(ContextService._value(checkpoint, "state_json"))
        if not state_json:
            return "无"
        try:
            parsed = json.loads(state_json)
        except (TypeError, ValueError):
            return state_json
        if isinstance(parsed, Mapping):
            for key in ("summary", "checkpoint_summary", "context_summary", "description"):
                value = ContextService._text(parsed.get(key))
                if value:
                    return value
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _normalise_system_rules(value: str | Sequence[str] | None) -> str:
        if value is None:
            return DEFAULT_SYSTEM_RULES
        if isinstance(value, str):
            return value
        return "\n".join(str(item) for item in value)

    @staticmethod
    def _validate_limit(value: int, name: str, *, allow_zero: bool) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{name} 必须{'大于等于' if allow_zero else '大于'} 0")
        return value

    @staticmethod
    def _value(value: Any, key: str, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, Mapping):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None


__all__ = [
    "ContextAssembly",
    "ContextResult",
    "ContextService",
    "DEFAULT_SYSTEM_RULES",
    "MemorySearchService",
    "RetrievedMemory",
]

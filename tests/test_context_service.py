from __future__ import annotations

from dataclasses import dataclass

from app.models import Blogger, SessionMessage, TaskCheckpoint, TaskSession
from app.services.context_service import ContextService


@dataclass
class MemoryRow:
    id: int
    blogger_id: int
    memory_type: str
    title: str
    content: str
    status: str = "active"
    confidence: float = 0.9
    source_type: str = "user_input"
    source_id: str = "source-1"
    version: int = 1


class FakeMemorySearch:
    def __init__(self, rows: list[MemoryRow] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls: list[dict] = []

    def search(self, *, blogger_id: int, query: str, top_k: int):
        self.calls.append({"blogger_id": blogger_id, "query": query, "top_k": top_k})
        if self.error:
            raise self.error
        return self.rows


def create_blogger(db, name: str = "测试博主") -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食探店"]',
        style="口播",
        follower_band="1万-10万",
        monetization_types_json='["商单"]',
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def create_task(db, blogger_id: int, task_id: str = "task-1") -> TaskSession:
    task = TaskSession(
        id=task_id,
        blogger_id=blogger_id,
        task_type="profile",
        title="画像确认任务",
        current_context="当前正在确认博主的内容方向",
        task_dir="data/tasks/task-1",
    )
    db.add(task)
    db.flush()
    db.add_all(
        [
            SessionMessage(
                task_id=task_id,
                sequence=sequence,
                role="user" if sequence % 2 else "assistant",
                content=f"历史消息 {sequence}",
            )
            for sequence in range(1, 6)
        ]
    )
    db.add_all(
        [
            TaskCheckpoint(
                task_id=task_id,
                sequence=1,
                state_json='{"summary":"旧检查点"}',
                context_snapshot="旧检查点摘要",
            ),
            TaskCheckpoint(
                task_id=task_id,
                sequence=2,
                state_json='{"summary":"最新检查点"}',
                context_snapshot="最新检查点摘要",
            ),
        ]
    )
    db.commit()
    return task


def test_context_has_fixed_order_and_truncates_history(db):
    blogger = create_blogger(db)
    task = create_task(db, blogger.id)
    memory_search = FakeMemorySearch(
        [
            MemoryRow(1, blogger.id, "profile_fact", "内容方向", "贵州美食探店"),
            MemoryRow(2, blogger.id + 1, "profile_fact", "别的博主", "不得出现"),
            MemoryRow(3, blogger.id, "profile_fact", "未激活", "也不得出现", status="candidate"),
        ]
    )

    result = ContextService(
        db,
        memory_search=memory_search,
        top_k=5,
        recent_message_limit=2,
        system_rules="系统规则",
    ).assemble_context(blogger.id, "请继续确认", task_id=task.id)

    assert [message["role"] for message in result.messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert result.messages[0]["content"] == "系统规则"
    short_term = result.messages[1]["content"]
    assert short_term.index("当前上下文：当前正在确认博主的内容方向") < short_term.index("最近消息：")
    assert "历史消息 4" in short_term
    assert "历史消息 5" in short_term
    assert "历史消息 1" not in short_term
    assert "历史消息 2" not in short_term
    assert "历史消息 3" not in short_term
    assert "最新检查点摘要" in short_term
    assert "旧检查点摘要" not in short_term
    assert "贵州美食探店" in result.messages[2]["content"]
    assert "别的博主" not in result.messages[2]["content"]
    assert "也不得出现" not in result.messages[2]["content"]
    assert result.messages[3]["content"] == "请继续确认"
    assert [row.id for row in result.retrieved_memories] == [1]


def test_memory_search_receives_current_blogger_and_top_k(db):
    blogger = create_blogger(db)
    memory_search = FakeMemorySearch([MemoryRow(1, blogger.id, "profile_fact", "事实", "内容")])

    ContextService(db, memory_search_service=memory_search, top_k=3).assemble_context(
        blogger.id, "本轮问题", task_id=None
    )

    assert len(memory_search.calls) == 1
    assert memory_search.calls[0]["blogger_id"] == blogger.id
    assert memory_search.calls[0]["top_k"] == 3
    assert "本轮问题" in memory_search.calls[0]["query"]


def test_failed_memory_search_does_not_enter_context(db):
    blogger = create_blogger(db)
    result = ContextService(
        db,
        memory_search=FakeMemorySearch(error=RuntimeError("检索服务不可用")),
    ).assemble_context(blogger.id, "只使用可用信息")

    assert result.retrieved_memories == []
    assert result.memory_search_error is not None
    assert "检索服务不可用" not in result.messages[2]["content"]
    assert result.messages[2]["content"].endswith("无")
    assert result.messages[3]["content"] == "只使用可用信息"


def test_mismatched_task_cannot_leak_short_term_memory(db):
    owner = create_blogger(db, "任务所属博主")
    other = create_blogger(db, "当前博主")
    task = create_task(db, owner.id, task_id="private-task")
    memory_search = FakeMemorySearch()

    result = ContextService(db, memory_search=memory_search).assemble_context(
        other.id,
        "当前输入",
        task_id=task.id,
    )

    assert result.task_missing is True
    short_term = result.messages[1]["content"]
    assert "当前正在确认博主的内容方向" not in short_term
    assert "历史消息" not in short_term
    assert "旧检查点摘要" not in short_term
    assert result.messages[3]["content"] == "当前输入"
    assert memory_search.calls[0]["blogger_id"] == other.id


def test_zero_recent_message_limit_keeps_only_current_task_and_checkpoint(db):
    blogger = create_blogger(db)
    task = create_task(db, blogger.id)

    result = ContextService(db, recent_message_limit=0).assemble_context(
        blogger.id,
        "本轮输入",
        task_id=task.id,
    )

    short_term = result.messages[1]["content"]
    assert "最近消息：\n无" in short_term
    assert "当前正在确认博主的内容方向" in short_term
    assert "最新检查点摘要" in short_term

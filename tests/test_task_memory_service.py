from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.models import (
    Blogger,
    DecisionLog,
    SessionMessage,
    TaskArtifact,
    TaskSession,
)
from app.services.task_memory_service import TaskMemoryService


def create_blogger(db) -> Blogger:
    blogger = Blogger(
        name="任务记忆测试博主",
        platform="抖音",
        content_types_json=json.dumps(["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        frequency="周更",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def test_create_task_creates_database_record_and_standard_directory(db, tmp_path: Path):
    blogger = create_blogger(db)
    service = TaskMemoryService(db, tmp_path / "tasks")
    task = service.create_task(blogger.id, "profile", "画像采集")

    assert db.get(TaskSession, task.id) is not None
    task_dir = tmp_path / "tasks" / task.id
    assert task_dir.is_dir()
    assert (task_dir / "task.json").is_file()
    assert (task_dir / "messages.jsonl").is_file()
    assert (task_dir / "context.md").is_file()
    assert (task_dir / "decisions.jsonl").is_file()
    assert (task_dir / "checkpoints.json").is_file()
    assert (task_dir / "final_summary.json").is_file()
    assert (task_dir / "artifacts").is_dir()
    assert json.loads((task_dir / "task.json").read_text(encoding="utf-8"))["task_id"] == task.id


def test_messages_decisions_and_checkpoints_are_ordered_and_persisted(db, tmp_path: Path):
    blogger = create_blogger(db)
    service = TaskMemoryService(db, tmp_path / "tasks")
    task = service.create_task(blogger.id, "demo", "短期记忆演示")

    first = service.append_message(task.id, "user", "我想做酸汤鱼内容")
    second = service.append_message(task.id, "assistant", "已记录你的选题")
    decision = service.record_decision(
        task.id,
        {"topic": "酸汤鱼"},
        reason="来自当前任务输入",
        input_summary={"message_id": first.id},
    )
    checkpoint = service.create_checkpoint(
        task.id,
        {"step": "profile", "message_sequence": second.sequence},
        "已完成选题采集",
    )

    assert [
        row.sequence
        for row in db.scalars(
            select(SessionMessage).where(SessionMessage.task_id == task.id).order_by(SessionMessage.sequence)
        )
    ] == [1, 2]
    assert checkpoint.sequence == 1
    assert decision.sequence == 1
    assert decision.database_id is not None
    assert db.scalar(select(DecisionLog).where(DecisionLog.id == decision.database_id)) is not None
    task_dir = tmp_path / "tasks" / task.id
    messages = [json.loads(line) for line in (task_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    decisions = [json.loads(line) for line in (task_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    checkpoints = json.loads((task_dir / "checkpoints.json").read_text(encoding="utf-8"))
    assert [item["sequence"] for item in messages] == [1, 2]
    assert [item["sequence"] for item in decisions] == [1]
    assert [item["sequence"] for item in checkpoints] == [1]
    assert (task_dir / "context.md").read_text(encoding="utf-8") == "已完成选题采集"


def test_recover_task_uses_latest_checkpoint_after_service_restart(db, tmp_path: Path):
    blogger = create_blogger(db)
    root = tmp_path / "tasks"
    original = TaskMemoryService(db, root)
    task = original.create_task(blogger.id, "long-task", "可恢复任务")
    original.create_checkpoint(task.id, {"step": 1}, "第一步完成")
    original.create_checkpoint(task.id, {"step": 2}, "第二步完成")
    task.status = "interrupted"
    db.commit()

    restarted = TaskMemoryService(db, root)
    recovered = restarted.recover_task(task.id)

    assert recovered.status == "running"
    assert recovered.current_context == "第二步完成"
    assert json.loads(recovered.recovery_state_json) == {"step": 2}
    assert restarted.read_task_context(task.id) == "第二步完成"


def test_complete_task_writes_summary_without_promoting_long_term_memory(db, tmp_path: Path):
    blogger = create_blogger(db)
    service = TaskMemoryService(db, tmp_path / "tasks")
    task = service.create_task(blogger.id, "build", "建库任务")

    completed = service.complete_task(
        task.id,
        {"result": "演示完成"},
        memory_candidates=[
            {
                "memory_type": "user_preference",
                "title": "内容偏好",
                "content": "偏好美食探店",
                "confidence": 0.7,
            }
        ],
    )

    assert completed.status == "completed"
    summary = json.loads((tmp_path / "tasks" / task.id / "final_summary.json").read_text(encoding="utf-8"))
    assert summary["memory_candidates_auto_activated"] is False
    assert summary["memory_candidates"][0]["status"] == "candidate"
    assert summary["memory_candidates"][0]["active"] is False
    assert db.scalar(select(TaskSession).where(TaskSession.id == task.id)).status == "completed"


def test_artifact_is_written_and_recorded(db, tmp_path: Path):
    blogger = create_blogger(db)
    service = TaskMemoryService(db, tmp_path / "tasks")
    task = service.create_task(blogger.id, "artifact", "产物任务")

    artifact = service.record_artifact(task.id, "summary", "summary.json", '{"ok": true}')

    path = tmp_path / "tasks" / task.id / artifact.relative_path
    assert path.read_text(encoding="utf-8") == '{"ok": true}'
    assert db.scalar(select(TaskArtifact).where(TaskArtifact.id == artifact.id)) is not None

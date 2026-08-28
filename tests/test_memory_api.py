from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.memory_routes import (
    get_context_service,
    get_memory_service,
    get_task_memory_service,
    router,
)
from app.db.session import get_db
from app.models import Blogger, MemoryRecord, TaskSession
from app.services.context_service import ContextService
from app.services.embedding_service import FakeEmbeddingService
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService


def make_blogger(db, name: str) -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        routes="黔东南",
        viral_topic="酸汤鱼探店",
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


@pytest.fixture()
def memory_client(db, tmp_path: Path):
    """为每个 API 测试注入隔离数据库、任务目录和轻量假向量器。"""

    api = FastAPI()
    api.include_router(router)

    def override_db():
        yield db

    task_root = tmp_path / "tasks"
    embedding = FakeEmbeddingService()
    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_task_memory_service] = lambda: TaskMemoryService(db, task_root)
    api.dependency_overrides[get_memory_service] = lambda: MemoryService(db, embedding=embedding)
    # 明确覆盖上下文依赖，确保测试使用同一 FakeEmbeddingService 实例。
    api.dependency_overrides[get_context_service] = lambda: ContextService(
        db,
        memory_search=MemoryService(db, embedding=embedding),
        top_k=5,
        recent_message_limit=6,
    )
    client = TestClient(api)
    try:
        yield client, db, task_root
    finally:
        api.dependency_overrides.clear()


def test_task_api_persists_logs_and_recovers_from_checkpoint(memory_client):
    client, db, task_root = memory_client
    blogger = make_blogger(db, "任务 API 博主")

    created = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/tasks",
        json={
            "task_id": "api-task-1",
            "task_type": "demo",
            "title": "任务日志演示",
            "initial_context": "开始采集",
        },
    )
    assert created.status_code == 200
    task = created.json()
    assert task["task_id"] == "api-task-1"
    task_dir = task_root / "api-task-1"
    assert task_dir.is_dir()
    assert (task_dir / "task.json").is_file()
    assert (task_dir / "messages.jsonl").is_file()
    assert (task_dir / "context.md").is_file()
    assert (task_dir / "decisions.jsonl").is_file()
    assert (task_dir / "checkpoints.json").is_file()
    assert (task_dir / "final_summary.json").is_file()
    assert (task_dir / "artifacts").is_dir()

    first = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1/messages",
        json={"role": "user", "content": "我想做酸汤鱼内容"},
    )
    second = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1/messages",
        json={"role": "assistant", "content": "已记录选题"},
    )
    assert first.status_code == second.status_code == 200
    assert [first.json()["sequence"], second.json()["sequence"]] == [1, 2]

    decision = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1/decisions",
        json={
            "decision": {"topic": "酸汤鱼"},
            "reason": "来自当前任务输入",
            "input_summary": {"message_sequence": 1},
        },
    )
    checkpoint = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1/checkpoints",
        json={"state": {"step": 1}, "context_snapshot": "选题已确认"},
    )
    assert decision.status_code == checkpoint.status_code == 200
    assert decision.json()["sequence"] == 1
    assert checkpoint.json()["sequence"] == 1

    read = client.get(f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1")
    assert read.status_code == 200
    assert [row["sequence"] for row in read.json()["messages"]] == [1, 2]
    assert read.json()["decisions"][0]["decision"] == {"topic": "酸汤鱼"}
    assert read.json()["checkpoints"][0]["context_snapshot"] == "选题已确认"

    persisted = db.get(TaskSession, "api-task-1")
    assert persisted is not None
    persisted.status = "interrupted"
    db.commit()
    recovered = client.post(f"/api/v1/memory/bloggers/{blogger.id}/tasks/api-task-1/recover")
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "running"
    assert recovered.json()["current_context"] == "选题已确认"
    assert recovered.json()["recovery_state"] == {"step": 1}


def test_task_completion_candidates_do_not_auto_activate_and_require_confirmation(memory_client):
    client, db, _ = memory_client
    blogger = make_blogger(db, "候选 API 博主")
    task_url = f"/api/v1/memory/bloggers/{blogger.id}/tasks"
    created = client.post(
        task_url,
        json={"task_id": "candidate-task", "task_type": "demo", "title": "候选任务"},
    )
    assert created.status_code == 200

    completed = client.post(
        f"{task_url}/candidate-task/complete",
        json={
            "final_summary": {"result": "完成"},
            "memory_candidates": [
                {
                    "memory_type": "user_preference",
                    "title": "内容偏好",
                    "content": "模型从一次对话猜测偏好夜游",
                    "confidence": 0.4,
                }
            ],
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["final_summary"]["memory_candidates_auto_activated"] is False
    assert completed.json()["final_summary"]["memory_candidates"][0]["status"] == "candidate"
    assert db.query(MemoryRecord).count() == 0

    candidate = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/memories",
        json={
            "memory_type": "user_preference",
            "title": "内容偏好",
            "content": "用户明确说喜欢夜游主题",
            "source_type": "user_confirmed",
            "source_id": "answer-1",
            "status": "active",
            "user_confirmed": False,
        },
    )
    assert candidate.status_code == 200
    assert candidate.json()["status"] == "candidate"
    assert client.get(f"/api/v1/memory/bloggers/{blogger.id}/memories").json() == []
    listed = client.get(f"/api/v1/memory/bloggers/{blogger.id}/memories?status=candidate")
    assert listed.status_code == 200
    memory_id = listed.json()[0]["id"]

    without_confirmation = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/memories/{memory_id}/promote",
        json={"user_confirmed": False},
    )
    assert without_confirmation.status_code == 409
    promoted = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/memories/{memory_id}/promote",
        json={"user_confirmed": True},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "active"


def test_memory_api_isolates_list_search_and_ids_by_blogger(memory_client):
    client, db, _ = memory_client
    first = make_blogger(db, "第一位 API 博主")
    second = make_blogger(db, "第二位 API 博主")
    memory_payload = {
        "memory_type": "verified_knowledge",
        "title": "贵州美食事实",
        "content": "贵州美食包括酸汤鱼和丝娃娃",
        "source_type": "user_confirmed",
        "status": "active",
        "user_confirmed": True,
    }
    first_memory = client.post(f"/api/v1/memory/bloggers/{first.id}/memories", json=memory_payload)
    second_memory = client.post(
        f"/api/v1/memory/bloggers/{second.id}/memories",
        json={**memory_payload, "title": "第二位私有事实", "content": "第二位博主的私有内容"},
    )
    assert first_memory.status_code == second_memory.status_code == 200

    first_list = client.get(f"/api/v1/memory/bloggers/{first.id}/memories")
    second_list = client.get(f"/api/v1/memory/bloggers/{second.id}/memories")
    assert [row["blogger_id"] for row in first_list.json()] == [first.id]
    assert [row["blogger_id"] for row in second_list.json()] == [second.id]

    search = client.get(f"/api/v1/memory/bloggers/{first.id}/memories/search?q=贵州美食&limit=10")
    assert search.status_code == 200
    assert search.json()
    assert all(row["blogger_id"] == first.id for row in search.json())

    foreign_id = second_memory.json()["id"]
    foreign_get = client.post(
        f"/api/v1/memory/bloggers/{first.id}/memories/{foreign_id}/promote",
        json={"user_confirmed": True},
    )
    assert foreign_get.status_code == 404


def test_context_api_returns_fixed_order_and_current_blogger_recall(memory_client):
    client, db, _ = memory_client
    first = make_blogger(db, "上下文 API 博主")
    other = make_blogger(db, "其他 API 博主")
    task_response = client.post(
        f"/api/v1/memory/bloggers/{first.id}/tasks",
        json={
            "task_id": "context-task",
            "task_type": "profile",
            "title": "画像上下文",
            "initial_context": "当前正在确认内容方向",
        },
    )
    assert task_response.status_code == 200
    client.post(
        f"/api/v1/memory/bloggers/{first.id}/tasks/context-task/messages",
        json={"role": "user", "content": "我喜欢贵州美食"},
    )
    client.post(
        f"/api/v1/memory/bloggers/{first.id}/tasks/context-task/checkpoints",
        json={"state": {"step": "profile"}, "context_snapshot": "最新检查点"},
    )
    own = client.post(
        f"/api/v1/memory/bloggers/{first.id}/memories",
        json={
            "memory_type": "profile_fact",
            "title": "内容方向",
            "content": "贵州美食探店",
            "source_type": "user_confirmed",
            "user_confirmed": True,
            "status": "active",
        },
    )
    assert own.status_code == 200
    foreign = client.post(
        f"/api/v1/memory/bloggers/{other.id}/memories",
        json={
            "memory_type": "profile_fact",
            "title": "其他博主",
            "content": "绝对不得泄漏",
            "source_type": "user_confirmed",
            "user_confirmed": True,
            "status": "active",
        },
    )
    assert foreign.status_code == 200

    context = client.post(
        f"/api/v1/memory/bloggers/{first.id}/context",
        json={
            "task_id": "context-task",
            "user_input": "请继续确认",
            "recent_message_limit": 2,
            "top_k": 5,
        },
    )
    assert context.status_code == 200
    payload = context.json()
    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert payload["messages"][3]["content"] == "请继续确认"
    assert "当前上下文：最新检查点" in payload["messages"][1]["content"]
    assert "贵州美食探店" in payload["messages"][2]["content"]
    assert "绝对不得泄漏" not in payload["messages"][2]["content"]
    assert [row["blogger_id"] for row in payload["recalled_memories"]] == [first.id]


def test_memory_api_returns_explicit_not_found_and_conflict(memory_client):
    client, db, _ = memory_client
    blogger = make_blogger(db, "错误处理博主")
    missing_task = client.get(f"/api/v1/memory/bloggers/{blogger.id}/tasks/not-exist")
    assert missing_task.status_code == 404
    missing_blogger = client.get("/api/v1/memory/bloggers/999999/memories")
    assert missing_blogger.status_code == 404
    invalid_context = client.post(
        f"/api/v1/memory/bloggers/{blogger.id}/context",
        json={"user_input": ""},
    )
    assert invalid_context.status_code == 422

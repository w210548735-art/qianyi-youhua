from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_demo_page_health_and_memory_openapi():
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        page = client.get("/")
        openapi = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json()["embedding_model"] == "fake-embedding"
    assert page.status_code == 200
    assert "多画像选择" in page.text
    assert "手工资产新增/编辑/删除" in page.text
    assert "地点新增/编辑/删除/过滤" in page.text
    assert "决策错误查看" in page.text
    assert "短期任务记忆与恢复" in page.text
    assert "长期记忆召回与 Agent 上下文" in page.text
    for control in (
        "bloggerSelect",
        "editProfile",
        "deleteProfile",
        "assetForm",
        "assetFilter",
        "placeForm",
        "placeFilter",
        "buildLibrary",
        "loadDecisionErrors",
        "recoverTask",
        "recallMemories",
    ):
        assert f'id="{control}"' in page.text

    expected_routes = {
        ("/api/v1/profile-sessions", "post"),
        ("/api/v1/profile-sessions/{session_id}/profile", "put"),
        ("/api/v1/profile-sessions/{session_id}/confirm", "post"),
        ("/api/v1/bloggers", "get"),
        ("/api/v1/bloggers", "post"),
        ("/api/v1/bloggers/{blogger_id}", "get"),
        ("/api/v1/bloggers/{blogger_id}", "put"),
        ("/api/v1/bloggers/{blogger_id}", "delete"),
        ("/api/v1/bloggers/{blogger_id}/build-runs", "post"),
        ("/api/v1/bloggers/{blogger_id}/assets", "get"),
        ("/api/v1/bloggers/{blogger_id}/assets", "post"),
        ("/api/v1/bloggers/{blogger_id}/assets/{asset_id}", "get"),
        ("/api/v1/bloggers/{blogger_id}/assets/{asset_id}", "put"),
        ("/api/v1/bloggers/{blogger_id}/assets/{asset_id}", "delete"),
        ("/api/v1/bloggers/{blogger_id}/places", "get"),
        ("/api/v1/bloggers/{blogger_id}/places", "post"),
        ("/api/v1/bloggers/{blogger_id}/places/{place_id}", "get"),
        ("/api/v1/bloggers/{blogger_id}/places/{place_id}", "put"),
        ("/api/v1/bloggers/{blogger_id}/places/{place_id}", "delete"),
        ("/api/v1/bloggers/{blogger_id}/decisions", "get"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks", "post"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks", "get"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks/{task_id}", "get"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks/{task_id}/recover", "post"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks/{task_id}/messages", "post"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks/{task_id}/checkpoints", "post"),
        ("/api/v1/memory/bloggers/{blogger_id}/tasks/{task_id}/complete", "post"),
        ("/api/v1/memory/bloggers/{blogger_id}/memories", "get"),
        ("/api/v1/memory/bloggers/{blogger_id}/memories/search", "get"),
        ("/api/v1/memory/bloggers/{blogger_id}/context", "post"),
    }
    for path, method in expected_routes:
        assert method in openapi.json()["paths"].get(path, {})
    memory_paths = [path for path in openapi.json()["paths"] if "/memory/" in path]
    assert len(memory_paths) == 11

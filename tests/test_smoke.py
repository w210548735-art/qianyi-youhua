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
    assert "短期任务记忆与恢复" in page.text
    assert "长期记忆召回与 Agent 上下文" in page.text
    memory_paths = [path for path in openapi.json()["paths"] if "/memory/" in path]
    assert len(memory_paths) == 11

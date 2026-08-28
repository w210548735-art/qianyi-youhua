from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.core.config import settings
from app.services import deepseek_client as client_module
from app.services.deepseek_client import DeepSeekClient


class FakeResponse:
    def __init__(self, assets: list[dict]) -> None:
        self.assets = assets
        self.raise_called = False

    def raise_for_status(self) -> None:
        self.raise_called = True

    def json(self) -> dict:
        content = json.dumps({"assets": self.assets}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


def _assets() -> list[dict]:
    rows = []
    for index in range(5):
        rows.append(
            {
                "lib_type": "material",
                "category": "口播模板",
                "title": f"素材 {index}",
                "content": "只使用已核验知识组织内容",
                "tags": ["口播"],
                "reason": "适配画像",
            }
        )
    for index in range(3):
        rows.append(
            {
                "lib_type": "algorithm",
                "category": "平台策略",
                "title": f"策略 {index}",
                "content": "发布前检查规则",
                "tags": ["抖音"],
                "reason": "适配平台",
            }
        )
    return rows


def test_deepseek_client_reads_local_key_and_validates_json(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    key_file.write_text("test-secret", encoding="utf-8")
    monkeypatch.setattr(
        client_module,
        "settings",
        replace(settings, deepseek_key_file=key_file),
    )
    fake_response = FakeResponse(_assets())

    def fake_post(url, *, headers, json, timeout):
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer test-secret"
        assert json["model"] == "deepseek-v4-flash"
        assert timeout == 12.0
        return fake_response

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    result = DeepSeekClient(timeout_seconds=12.0).generate_support_assets(
        {"platform": "抖音", "style": "口播"},
        ["梵净山"],
    )

    assert fake_response.raise_called is True
    assert len(result) == 8
    assert sum(item.lib_type == "material" for item in result) == 5
    assert sum(item.lib_type == "algorithm" for item in result) == 3


def test_deepseek_key_missing_and_empty_are_explicit(tmp_path, monkeypatch):
    missing = tmp_path / "missing.txt"
    monkeypatch.setattr(
        client_module,
        "settings",
        replace(settings, deepseek_key_file=missing),
    )
    with pytest.raises(RuntimeError, match="DEEPSEEK_KEY_NOT_FOUND"):
        DeepSeekClient()._api_key()

    empty = tmp_path / "empty.txt"
    empty.write_text("  ", encoding="utf-8")
    monkeypatch.setattr(
        client_module,
        "settings",
        replace(settings, deepseek_key_file=empty),
    )
    with pytest.raises(RuntimeError, match="DEEPSEEK_KEY_EMPTY"):
        DeepSeekClient()._api_key()


def test_deepseek_rejects_invalid_asset_count(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    key_file.write_text("test-secret", encoding="utf-8")
    monkeypatch.setattr(
        client_module,
        "settings",
        replace(settings, deepseek_key_file=key_file),
    )
    monkeypatch.setattr(
        client_module.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(_assets()[:4]),
    )

    with pytest.raises(RuntimeError, match="DEEPSEEK_ASSET_COUNT_INVALID"):
        DeepSeekClient().generate_support_assets({}, [])

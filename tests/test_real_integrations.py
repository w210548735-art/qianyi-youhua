"""真实服务 smoke 测试。

这些测试默认跳过，避免离线测试意外加载模型或请求外部服务。显式开启时：

* 设置 ``RUN_REAL_INTEGRATIONS=1`` 可开启本文件中的全部真实测试；
* 设置 ``RUN_REAL_EMBEDDING=1`` 或 ``RUN_REAL_EMBEDDING_TESTS=1`` 可只开启本地向量测试；
* 设置 ``RUN_REAL_DEEPSEEK=1`` 或 ``RUN_REAL_DEEPSEEK_TESTS=1`` 可只开启 DeepSeek 测试。

真实 DeepSeek 测试只读取现有配置中的本地 key 文件，绝不输出 key 内容。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.core.config import settings
from app.services.deepseek_client import DeepSeekClient, GeneratedAsset
from app.services.embedding_service import EmbeddingService

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _real_test_enabled(*names: str) -> bool:
    """返回是否通过显式环境变量开启了真实测试。"""

    return any(os.getenv(name, "").strip().lower() in _TRUTHY for name in names)


def _require_real_test(*specific_names: str) -> None:
    """真实测试默认跳过，避免离线测试产生模型加载或网络副作用。"""

    gate_names = ("RUN_REAL_INTEGRATIONS", "RUN_REAL_INTEGRATION_TESTS", *specific_names)
    if not _real_test_enabled(*gate_names):
        pytest.skip(
            "真实集成测试默认跳过；设置 RUN_REAL_INTEGRATIONS=1 或对应的专项环境变量后再运行"
        )


def _local_key_is_available(key_file: Path) -> bool:
    """只检查 key 文件是否存在且非空，不让 key 内容进入测试输出。"""

    try:
        return key_file.is_file() and bool(key_file.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def test_real_bge_small_zh_v1_5_embedding_dimension() -> None:
    """验证本地中文模型的设备选择和 512 维向量输出。"""

    _require_real_test("RUN_REAL_EMBEDDING", "RUN_REAL_EMBEDDING_TESTS")
    torch = pytest.importorskip("torch", reason="未安装 torch，无法执行本地 embedding smoke")

    service = EmbeddingService()
    assert service.model_name == "BAAI/bge-small-zh-v1.5"
    expected_device = "cuda" if torch.cuda.is_available() else "cpu"
    assert service.device == expected_device

    try:
        results = service.encode_documents(["贵州非遗与山地美食的短视频选题"])
    except (FileNotFoundError, ImportError, OSError):
        pytest.skip("本地 BAAI/bge-small-zh-v1.5 模型或运行依赖未准备好")

    assert len(results) == 1
    vector = results[0].vector
    assert vector.shape == (512,)
    assert vector.dtype == np.float32
    assert np.isfinite(vector).all()
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-3)


def test_real_deepseek_v4_flash_returns_valid_asset_structure() -> None:
    """在 key 和网络可用时验证 DeepSeek 支持资产响应结构。"""

    _require_real_test("RUN_REAL_DEEPSEEK", "RUN_REAL_DEEPSEEK_TESTS")
    if not _local_key_is_available(settings.deepseek_key_file):
        pytest.skip("未找到非空 DeepSeek key 文件")

    assert settings.deepseek_model == "deepseek-v4-flash"
    client = DeepSeekClient(timeout_seconds=90.0)
    try:
        assets = client.generate_support_assets(
            {"platform": "抖音", "style": "口播", "content_types": ["美食", "非遗"]},
            ["梵净山", "黄果树瀑布"],
        )
    except httpx.RequestError:
        pytest.skip("DeepSeek 网络当前不可用")

    assert isinstance(assets, list)
    assert len(assets) >= 8
    assert sum(asset.lib_type == "material" for asset in assets) >= 5
    assert sum(asset.lib_type == "algorithm" for asset in assets) >= 3

    for asset in assets:
        assert isinstance(asset, GeneratedAsset)
        assert asset.lib_type in {"material", "algorithm"}
        assert isinstance(asset.category, str) and asset.category.strip()
        assert isinstance(asset.title, str) and asset.title.strip()
        assert isinstance(asset.content, str) and asset.content.strip()
        assert isinstance(asset.tags, list)
        assert all(isinstance(tag, str) and tag.strip() for tag in asset.tags)
        assert isinstance(asset.reason, str) and asset.reason.strip()

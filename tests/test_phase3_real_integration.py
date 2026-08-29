"""第三阶段真实 Agent/BGE smoke；默认离线跳过，需显式设置环境变量。"""

from __future__ import annotations

import os

import numpy as np
import pytest

from app.core.config import settings
from app.services.embedding_service import EmbeddingService
from app.services.output_agent import DeepSeekOutputAgent, OutputAgentError
from app.services.output_validation_service import OutputValidationService

pytestmark = pytest.mark.real_integration

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(*names: str) -> bool:
    return any(os.getenv(name, "").strip().lower() in _TRUTHY for name in names)


def _require_real(*names: str) -> None:
    if not _enabled("RUN_PHASE3_REAL_INTEGRATIONS", *names):
        pytest.skip("第三阶段真实集成默认跳过；需显式设置对应环境变量")


def _snapshot() -> dict:
    return {
        "blogger_id": 1,
        "profile": {"id": 1, "platform": "抖音", "style": "口播", "content_types": ["贵州美食"]},
        "sources": [{"id": 101, "title": "官方"}],
        "assets": [
            {
                "id": 11,
                "blogger_id": 1,
                "lib_type": "knowledge",
                "title": "凯里酸汤鱼",
                "content": "贵州美食可信事实",
                "credibility": 5,
                "source_document_ids": [101],
            }
        ],
        "places": [],
        "assessment": {"id": 1, "status": "succeeded"},
    }


def test_phase3_real_bge_cuda_and_dimension() -> None:
    _require_real("RUN_PHASE3_REAL_BGE")
    torch = pytest.importorskip("torch")
    service = EmbeddingService()
    assert service.model_name == "BAAI/bge-small-zh-v1.5"
    assert service.device == ("cuda" if torch.cuda.is_available() else "cpu")
    try:
        vector = service.encode_documents(["贵州文旅内容产出"])[0].vector
    except (FileNotFoundError, ImportError, OSError) as exc:
        pytest.skip(f"本地 BGE 不可用：{exc.__class__.__name__}")
    assert vector.shape == (512,)
    assert vector.dtype == np.float32
    assert np.isfinite(vector).all()


def test_phase3_real_deepseek_script_structure() -> None:
    _require_real("RUN_PHASE3_REAL_DEEPSEEK")
    if not settings.deepseek_key_file.is_file():
        pytest.skip("DeepSeek key 文件不存在")
    assert settings.deepseek_model == "deepseek-v4-flash"
    try:
        output = DeepSeekOutputAgent(timeout_seconds=90).generate_script(
            [
                {"role": "system", "content": "只使用输入快照中的事实"},
                {"role": "system", "content": "当前任务短期记忆"},
                {"role": "system", "content": "当前博主长期记忆"},
                {"role": "user", "content": "生成一条贵州美食口播脚本"},
            ],
            _snapshot(),
            "生成一条贵州美食口播脚本",
        )
    except OutputAgentError as exc:
        if exc.code in {"AGENT_TIMEOUT", "AGENT_REQUEST_FAILED", "DEEPSEEK_KEY_NOT_FOUND", "DEEPSEEK_KEY_EMPTY"}:
            pytest.skip(f"DeepSeek 网络或服务暂不可用：{exc.code}")
        raise
    normalized = OutputValidationService().validate_script(output, _snapshot())
    assert normalized["source_refs"]
    assert normalized["platform"] == "抖音"

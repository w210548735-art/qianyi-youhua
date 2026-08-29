"""第四阶段真实 BGE/DeepSeek smoke；默认离线跳过。"""

from __future__ import annotations

import os

import numpy as np
import pytest

from app.core.config import settings
from app.services.embedding_service import EmbeddingService
from app.services.feedback_agent import DeepSeekFeedbackAgent, FeedbackAgentError
from app.services.feedback_validation_service import FeedbackValidationService

pytestmark = pytest.mark.real_integration

_TRUTHY = {"1", "true", "yes", "on"}


def _require_real(name: str) -> None:
    if not (
        os.getenv("RUN_PHASE4_REAL_INTEGRATIONS", "").strip().lower() in _TRUTHY
        or os.getenv(name, "").strip().lower() in _TRUTHY
    ):
        pytest.skip("第四阶段真实集成默认跳过；需显式设置开关")


def _feedback_snapshot() -> dict:
    evidence = [
        {"evidence_type": "metric", "ref_id": 10, "claim": "主指标", "snapshot": {}},
        {"evidence_type": "output", "ref_id": 20, "claim": "当前产出", "snapshot": {}},
    ]
    return {
        "blogger_id": 1,
        "profile": {"id": 1, "platform": "抖音", "style": "口播"},
        "output": {"id": 20, "blogger_id": 1, "category": "贵州美食", "deleted_at": None},
        "primary_metric": {
            "id": 10,
            "output_id": 20,
            "source_type": "manual",
            "user_confirmed": True,
            "views": 100,
            "likes": 20,
            "comments": 5,
            "collects": 2,
            "shares": 1,
            "actual_revenue": None,
            "actual_cost": None,
        },
        "assets": [],
        "places": [],
        "output_assets": [],
        "output_places": [],
        "asset_places": [],
        "active_memories": [],
        "evidence_whitelist": evidence,
        "deterministic_analysis": {"overall_status": "ok"},
        "user_confirmed_place_updates": {},
    }


def test_phase4_real_bge_cuda_and_dimension() -> None:
    _require_real("RUN_PHASE4_REAL_BGE")
    torch = pytest.importorskip("torch")
    service = EmbeddingService()
    assert service.model_name == "BAAI/bge-small-zh-v1.5"
    assert service.device == ("cuda" if torch.cuda.is_available() else "cpu")
    vector = service.encode_documents(["贵州文旅反馈闭环与经营报告"])[0].vector
    assert vector.shape == (512,) and vector.dtype == np.float32
    assert np.isfinite(vector).all()


def test_phase4_real_deepseek_feedback_structure() -> None:
    _require_real("RUN_PHASE4_REAL_DEEPSEEK")
    if not settings.deepseek_key_file.is_file():
        pytest.skip("DeepSeek key 文件不存在")
    assert settings.deepseek_model == "deepseek-v4-flash"
    snapshot = _feedback_snapshot()
    try:
        result = DeepSeekFeedbackAgent(timeout_seconds=90).analyze(
            [
                {"role": "system", "content": "只依据当前博主冻结快照提出候选"},
                {"role": "user", "content": "分析反馈，不自动写回"},
            ],
            snapshot,
            "分析反馈，不自动写回",
        )
    except FeedbackAgentError as exc:
        if exc.code in {
            "AGENT_TIMEOUT",
            "AGENT_REQUEST_FAILED",
            "DEEPSEEK_KEY_NOT_FOUND",
            "DEEPSEEK_KEY_EMPTY",
        }:
            pytest.skip(f"DeepSeek 网络或服务暂不可用：{exc.code}")
        raise
    normalized = FeedbackValidationService().validate_and_normalize(result, snapshot)
    assert normalized["summary"]
    assert normalized["data_quality"]["status"] == "ok"

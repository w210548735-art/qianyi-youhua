"""第二阶段真实模型 smoke；默认离线跳过。"""

from __future__ import annotations

import os

import numpy as np
import pytest

from app.core.config import settings
from app.services.assessment_agent import AssessmentAgentError, DeepSeekAssessmentAgent
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.embedding_service import EmbeddingService

_TRUTHY = {"1", "true", "yes", "on"}


def enabled(*names: str) -> bool:
    return any(os.getenv(name, "").strip().lower() in _TRUTHY for name in names)


def require_real(*names: str) -> None:
    if not enabled("RUN_PHASE2_REAL_INTEGRATIONS", *names):
        pytest.skip("第二阶段真实集成默认跳过；需显式设置对应环境变量")


def real_analysis() -> dict:
    assets = [
        {
            "id": 1,
            "lib_type": "knowledge",
            "title": "凯里酸汤鱼",
            "content": "贵州地方美食可信知识",
            "category": "美食",
            "source_document_id": 10,
            "sources": [{"id": 10}],
        },
        {
            "id": 2,
            "lib_type": "material",
            "title": "酸汤鱼口播模板",
            "content": "基于可信知识组织的口播结构",
            "category": "口播模板",
        },
        {
            "id": 3,
            "lib_type": "algorithm",
            "title": "抖音结构检查",
            "content": "发布前内容结构检查规则",
            "category": "抖音策略",
        },
    ]
    return {
        "blogger_id": 1,
        "snapshot_hash": "phase2-real-smoke",
        "assets": assets,
        "sources": [{"id": 10, "title": "可信来源"}],
        "libraries": {
            "knowledge": {"count": 1, "assets": [assets[0]]},
            "material": {"count": 1, "assets": [assets[1]]},
            "algorithm": {"count": 1, "assets": [assets[2]]},
        },
        "relations": [
            {"left_asset_id": 1, "right_asset_id": 2, "similarity": 0.9},
            {"left_asset_id": 2, "right_asset_id": 3, "similarity": 0.8},
        ],
        "core_assets": assets,
        "weak_points": ["景区和非遗知识仍不足"],
        "source_coverage": 100.0,
        "profile_coverage": 60.0,
        "feature_readiness": {
            "script_generation": {"ready": True, "missing_items": []},
            "route_recommendation": {
                "ready": False,
                "missing_items": ["缺少景区可信知识资产"],
            },
        },
        "future_data": {"output": "暂无数据", "effect": "暂无数据"},
    }


def test_phase2_real_bge_cuda_and_dimension() -> None:
    require_real("RUN_PHASE2_REAL_BGE")
    torch = pytest.importorskip("torch")
    service = EmbeddingService()
    expected_device = "cuda" if torch.cuda.is_available() else "cpu"
    assert service.model_name == "BAAI/bge-small-zh-v1.5"
    assert service.device == expected_device
    try:
        vector = service.encode_documents(["贵州文旅三库体检与语义关系"])[0].vector
    except (FileNotFoundError, ImportError, OSError):
        pytest.skip("本地BGE模型或依赖尚未准备")
    assert vector.shape == (512,)
    assert vector.dtype == np.float32
    assert np.isfinite(vector).all()


def test_phase2_real_deepseek_assessment_structure() -> None:
    require_real("RUN_PHASE2_REAL_DEEPSEEK")
    if not settings.deepseek_key_file.is_file():
        pytest.skip("DeepSeek key文件不存在")
    assert settings.deepseek_model == "deepseek-v4-flash"
    agent = DeepSeekAssessmentAgent(timeout_seconds=90)
    analysis = real_analysis()
    try:
        report = agent.assess(
            [
                {"role": "system", "content": "只依据当前博主体检快照"},
                {"role": "system", "content": "当前任务短期记忆"},
                {"role": "system", "content": "当前博主active长期记忆"},
                {"role": "user", "content": "执行第二阶段知识库体检"},
            ],
            analysis,
        )
    except AssessmentAgentError as exc:
        if exc.code in {
            "AGENT_TIMEOUT",
            "AGENT_REQUEST_FAILED",
            "DEEPSEEK_KEY_NOT_FOUND",
            "DEEPSEEK_KEY_EMPTY",
        }:
            pytest.skip(f"DeepSeek真实调用当前不可用：{exc.code}")
        raise
    normalized = AssessmentValidationService().validate_and_normalize(report, analysis)
    assert len(normalized["indicators"]) >= 3
    assert normalized["evidence_refs"]
    assert 0 <= normalized["overall_score"] <= 100
    assert normalized["feature_readiness"]["route_recommendation"]["missing_items"]

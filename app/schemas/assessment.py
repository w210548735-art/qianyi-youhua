"""第二阶段知识库体检 API 请求与响应数据结构。

响应模型严格对应 ``assessment_routes`` 实际返回的 JSON 字段。数据库列名中的
``*_json`` 只属于持久化实现，不向 API 暴露，避免客户端同时处理两套协议。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AssessmentStatus = Literal["pending", "running", "succeeded", "failed"]


class AssessmentCreate(BaseModel):
    """开始体检请求。

    ``idempotency_key`` 按博主作用域保证幂等；``task_id`` 仅用于把外部任务
    记忆关联到体检，通常由编排服务自动创建。
    """

    idempotency_key: str = Field(min_length=8, max_length=100)
    task_id: str | None = Field(default=None, min_length=1, max_length=36)
    prompt_version: str = Field(default="phase2-v1", min_length=1, max_length=50)


# 兼容路由层常见的命名方式。
AssessmentCreateRequest = AssessmentCreate


class AssessmentEvidenceRef(BaseModel):
    """指标 JSON 中保存的证据引用。"""

    model_config = ConfigDict(extra="allow")

    evidence_type: str
    asset_id: int | None = None
    source_document_id: int | None = None
    from_asset_id: int | None = None
    to_asset_id: int | None = None
    claim: str | None = None


class AssessmentEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    assessment_id: int
    indicator_id: int
    evidence_type: str
    asset_id: int | None = None
    source_document_id: int | None = None
    claim: str
    created_at: datetime


class AssessmentIndicatorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    assessment_id: int
    ordinal: int
    name: str
    meaning: str
    score_logic: str
    business_meaning: str
    weight: float
    weight_reason: str
    score: float
    reason: str
    evidence: list[AssessmentEvidenceRef] = Field(default_factory=list)
    evidences: list[AssessmentEvidenceRead] = Field(default_factory=list)
    created_at: datetime


class AssessmentBase(BaseModel):
    """体检历史列表与详情共有的字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    blogger_id: int
    task_id: str | None = None
    status: AssessmentStatus
    idempotency_key: str
    snapshot_hash: str | None = None
    summary: str | None = None
    overall_score: float | None = Field(default=None, ge=0, le=100)
    decision_id: int | None = None
    prompt_version: str
    model_name: str
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class AssessmentRead(AssessmentBase):
    """体检详情响应。"""

    input_snapshot: dict[str, Any] | list[Any] | None = None
    library_analysis: dict[str, Any] | list[Any] | None = None
    feature_readiness: dict[str, Any] | list[Any] | None = None
    suggestions: list[Any] | dict[str, Any] | None = None
    indicators: list[AssessmentIndicatorRead] = Field(default_factory=list)
    evidence: list[AssessmentEvidenceRead] = Field(default_factory=list)


class AssessmentListItem(AssessmentBase):
    """历史列表响应；列表路由不展开快照、指标和证据。"""


class AssessmentCompareRequest(BaseModel):
    left_id: int = Field(gt=0)
    right_id: int = Field(gt=0)


class AssessmentHistorySummary(BaseModel):
    """比较结果中一侧体检的摘要。"""

    id: int
    status: AssessmentStatus
    created_at: str | None = None
    snapshot_hash: str | None = None
    overall_score: float | None = None
    summary: str | None = None


class AssessmentScoreComparison(BaseModel):
    left: float | None = None
    right: float | None = None
    delta: float | None = None


class AssessmentLibraryMetric(BaseModel):
    left_count: int
    right_count: int
    count_delta: int
    left_credibility: dict[str, Any] | list[Any] = Field(default_factory=dict)
    right_credibility: dict[str, Any] | list[Any] = Field(default_factory=dict)


class AssessmentLibraryCount(BaseModel):
    left: int
    right: int
    delta: int


class AssessmentCredibilityComparison(BaseModel):
    left: dict[str, Any] | list[Any] = Field(default_factory=dict)
    right: dict[str, Any] | list[Any] = Field(default_factory=dict)


class AssessmentWeakPointComparison(BaseModel):
    left: list[Any] = Field(default_factory=list)
    right: list[Any] = Field(default_factory=list)
    added: list[Any] = Field(default_factory=list)
    removed: list[Any] = Field(default_factory=list)


class AssessmentReadinessComparison(BaseModel):
    left: dict[str, Any] = Field(default_factory=dict)
    right: dict[str, Any] = Field(default_factory=dict)
    changes: list[Any] = Field(default_factory=list)


class AssessmentIndicatorChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    left_score: float | None = None
    right_score: float | None = None
    score_delta: float | None = None
    left_weight: float | None = None
    right_weight: float | None = None
    weight_delta: float | None = None


class AssessmentIndicatorChanges(BaseModel):
    matched: list[AssessmentIndicatorChange] = Field(default_factory=list)
    added: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)


class AssessmentIndicatorSystem(BaseModel):
    added: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)


class AssessmentCompareRead(BaseModel):
    """历史比较响应；两次体检的指标体系可以不同。"""

    blogger_id: int
    left_id: int
    right_id: int
    left: AssessmentHistorySummary
    right: AssessmentHistorySummary
    overall_score: AssessmentScoreComparison
    overall_score_delta: float | None = None
    library_metrics: dict[str, AssessmentLibraryMetric] = Field(default_factory=dict)
    library_scale: dict[str, AssessmentLibraryMetric] = Field(default_factory=dict)
    library_counts: dict[str, AssessmentLibraryCount] = Field(default_factory=dict)
    credibility: dict[str, AssessmentCredibilityComparison] = Field(default_factory=dict)
    weak_points: AssessmentWeakPointComparison
    feature_readiness: AssessmentReadinessComparison
    readiness: AssessmentReadinessComparison
    indicator_changes: AssessmentIndicatorChanges
    indicators: AssessmentIndicatorChanges
    indicator_system: AssessmentIndicatorSystem
    summary: str


__all__ = [
    "AssessmentCompareRead",
    "AssessmentCompareRequest",
    "AssessmentBase",
    "AssessmentCreate",
    "AssessmentCreateRequest",
    "AssessmentCredibilityComparison",
    "AssessmentEvidenceRef",
    "AssessmentEvidenceRead",
    "AssessmentHistorySummary",
    "AssessmentIndicatorChange",
    "AssessmentIndicatorChanges",
    "AssessmentIndicatorSystem",
    "AssessmentIndicatorRead",
    "AssessmentLibraryCount",
    "AssessmentLibraryMetric",
    "AssessmentListItem",
    "AssessmentReadinessComparison",
    "AssessmentRead",
    "AssessmentScoreComparison",
    "AssessmentStatus",
    "AssessmentWeakPointComparison",
]

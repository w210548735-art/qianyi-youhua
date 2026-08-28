"""第四阶段反馈闭环 API 数据契约。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FeedbackStatus = Literal["pending", "running", "analyzed", "applied", "rejected", "failed"]
CandidateStatus = Literal["pending", "applied", "rejected"]


class FeedbackRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: int = Field(gt=0)
    primary_metric_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    user_instruction: str = Field(default="", max_length=2000)


class FeedbackRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_instruction: str = Field(default="", max_length=2000)


class PlaceCommercialOverride(BaseModel):
    """用户明确确认的地点商业字段；缺失字段不会被写为零。"""

    model_config = ConfigDict(extra="forbid")

    est_cost: Decimal | None = Field(default=None, ge=0)
    est_benefit: Decimal | None = Field(default=None, ge=0)
    like_level: int | None = Field(default=None, ge=1, le=5)
    fits_koc: bool | None = None
    fits_shoot: bool | None = None


class FeedbackConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[str] | None = None
    place_overrides: dict[int, PlaceCommercialOverride] = Field(default_factory=dict)


class FeedbackRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[str] | None = None
    reason: str = Field(default="用户明确拒绝反馈候选", min_length=1, max_length=1000)


class FeedbackEvidenceRead(BaseModel):
    id: int
    feedback_run_id: int
    evidence_type: str
    ref_id: int
    claim: str
    snapshot: dict[str, Any]


class FeedbackCandidateRead(BaseModel):
    id: str
    candidate_type: Literal["profile", "asset_effect", "place_commercial", "library_evolution"]
    status: CandidateStatus
    version: int
    payload: dict[str, Any]


class FeedbackRunRead(BaseModel):
    id: int
    blogger_id: int
    output_id: int
    primary_metric_id: int
    task_id: str | None
    status: FeedbackStatus
    idempotency_key: str
    snapshot_hash: str | None
    analysis: dict[str, Any]
    summary: str | None
    prompt_version: str
    model_name: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    applied_at: datetime | None
    rejected_at: datetime | None


__all__ = [
    "CandidateStatus",
    "FeedbackCandidateRead",
    "FeedbackConfirmRequest",
    "FeedbackEvidenceRead",
    "FeedbackRejectRequest",
    "FeedbackRetryRequest",
    "FeedbackRunCreateRequest",
    "FeedbackRunRead",
    "FeedbackStatus",
    "PlaceCommercialOverride",
]

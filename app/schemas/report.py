"""第四阶段经营指标与报告 API 数据契约。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IndicatorCategory = Literal["money", "traffic", "product", "supplier"]
ObservationStatus = Literal["ok", "data_insufficient"]
ReportStatus = Literal["pending", "running", "succeeded", "failed"]


class IndicatorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: IndicatorCategory
    name: str = Field(min_length=1, max_length=200)
    meaning: str = Field(min_length=1, max_length=1000)
    formula_key: str = Field(min_length=1, max_length=100)
    source_tables: list[str] = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=50)
    direction: Literal["higher_better", "lower_better", "neutral"] = "neutral"
    target_value: Decimal | None = None


class IndicatorUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    meaning: str | None = Field(default=None, min_length=1, max_length=1000)
    target_value: Decimal | None = None


class IndicatorRecomputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)
    feedback_run_id: int | None = Field(default=None, gt=0)


class IndicatorRead(BaseModel):
    id: int
    blogger_id: int
    category: IndicatorCategory
    name: str
    meaning: str
    formula_key: str
    source_tables: list[str]
    unit: str
    direction: str
    target_value: float | None
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ObservationRead(BaseModel):
    id: int
    indicator_id: int
    feedback_run_id: int | None
    report_id: int | None
    value: float | None
    status: ObservationStatus
    trend: Literal["up", "down", "flat", "unknown"]
    evidence: dict[str, Any]
    observed_at: datetime


class ReportGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)
    user_instruction: str = Field(default="", max_length=2000)


class ReportRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_instruction: str = Field(default="", max_length=2000)


class ReportRead(BaseModel):
    id: int
    blogger_id: int
    task_id: str | None
    status: ReportStatus
    idempotency_key: str
    snapshot_hash: str | None
    conclusion: dict[str, Any]
    charts: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]
    data_quality: dict[str, Any]
    prompt_version: str
    model_name: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "IndicatorCategory",
    "IndicatorCreateRequest",
    "IndicatorRead",
    "IndicatorRecomputeRequest",
    "IndicatorUpdateRequest",
    "ObservationRead",
    "ObservationStatus",
    "ReportGenerateRequest",
    "ReportRead",
    "ReportRetryRequest",
    "ReportStatus",
]

"""第三阶段内容产出、排期、模拟发布与指标回收 Schema。

Schema 只描述 API 边界，不承担生成结果的可信度判断；资产、地点和平台状态
由对应服务在写入前再次按博主范围校验。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OutputType = Literal["script", "storyboard", "route_rec"]
OutputStatus = Literal["pending", "running", "succeeded", "failed", "draft", "deleted"]
ScheduleStatus = Literal["pending", "published", "collected", "cancelled"]
PublishStatus = Literal["pending", "published", "failed", "cancelled"]
ReminderStatus = Literal["pending", "sent", "failed", "cancelled"]
CollectionStatus = Literal["pending", "running", "succeeded", "failed"]
MetricSourceType = Literal["manual", "simulated"]


class OutputSourceRef(BaseModel):
    """输出中可回查的资产、地点或来源引用。"""

    model_config = ConfigDict(extra="forbid")

    asset_id: int | None = Field(default=None, gt=0)
    place_id: int | None = Field(default=None, gt=0)
    source_document_id: int | None = Field(default=None, gt=0)
    claim: str = Field(min_length=1)
    usage_type: str | None = Field(default=None, min_length=1, max_length=50)
    role: str | None = Field(default=None, min_length=1, max_length=50)
    sequence: int | None = Field(default=None, gt=0)


class OutputGenerateRequest(BaseModel):
    """脚本、分镜和路线推荐共用的生成请求。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)
    assessment_id: int | None = Field(default=None, gt=0)
    category: str = Field(default="", max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    topic: str | None = Field(default=None, max_length=300)
    user_instruction: str | None = Field(default=None, max_length=4000)
    platform: str | None = Field(default=None, max_length=50)
    content_type: str | None = Field(default=None, max_length=50)
    place_ids: list[int] = Field(default_factory=list, min_length=0)


class ScriptGenerateRequest(OutputGenerateRequest):
    """脚本生成请求。"""


class StoryboardGenerateRequest(OutputGenerateRequest):
    """分镜生成请求；服务层还必须接收一个有效脚本版本。"""

    script_output_id: int | None = Field(default=None, gt=0)


class RouteGenerateRequest(OutputGenerateRequest):
    """路线推荐请求。"""

    route_date: date | None = None


# 兼容路由层可能使用的命名。
OutputCreateRequest = OutputGenerateRequest
OutputGenerationRequest = OutputGenerateRequest
ScriptRequest = ScriptGenerateRequest
StoryboardRequest = StoryboardGenerateRequest
RouteRecommendationRequest = RouteGenerateRequest


class OutputAssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    output_id: int
    asset_id: int
    usage_type: str
    claim: str


class OutputPlaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    output_id: int
    place_id: int
    role: str
    sequence: int
    claim: str


class OutputBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    blogger_id: int
    task_id: str | None = None
    idempotency_key: str | None = None
    type: OutputType
    category: str
    title: str
    status: OutputStatus
    assessment_id: int | None = None
    parent_output_id: int | None = None
    version: int = Field(ge=1)
    manual_locked: bool
    decision_id: int | None = None
    prompt_version: str
    model_name: str
    error_code: str | None = None
    error_message: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OutputRead(OutputBase):
    """输出详情；``content_json`` 可由服务返回已解码对象或原始 JSON。"""

    content_json: dict[str, Any] | list[Any] | str
    assets: list[OutputAssetRead] = Field(default_factory=list)
    places: list[OutputPlaceRead] = Field(default_factory=list)


class OutputListItem(OutputBase):
    """输出历史列表项，不展开正文和引用。"""


class OutputRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    content_json: dict[str, Any] | list[Any] | None = None
    user_instruction: str | None = Field(default=None, max_length=4000)
    manual_locked: bool = True


OutputUpdateRequest = OutputRevisionRequest
OutputEditRequest = OutputRevisionRequest


class AssetPlaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_id: int
    place_id: int
    relation_type: str
    source_type: str


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_id: int = Field(gt=0)
    plan_date: date
    platform: str = Field(min_length=1, max_length=50)
    content_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)


class ScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_date: date | None = None
    platform: str | None = Field(default=None, min_length=1, max_length=50)
    content_type: str | None = Field(default=None, min_length=1, max_length=50)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: ScheduleStatus | None = None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    blogger_id: int
    output_id: int
    plan_date: date
    platform: str
    content_type: str
    title: str
    status: ScheduleStatus
    publish_time: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)


PublishCreateRequest = PublishRequest


class PublishEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    schedule_id: int
    status: PublishStatus
    idempotency_key: str
    published_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime


class ReminderEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    schedule_id: int
    reminder_date: date
    status: ReminderStatus
    dedupe_key: str
    created_at: datetime


class MetricCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: MetricSourceType = "simulated"
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    collects: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    actual_revenue: Decimal | None = Field(default=None, ge=0)
    actual_cost: Decimal | None = Field(default=None, ge=0)
    user_confirmed: bool = False
    collected_at: datetime | None = None

    @model_validator(mode="after")
    def validate_commercial_boundary(self) -> MetricCreateRequest:
        has_actual = self.actual_revenue is not None or self.actual_cost is not None
        if has_actual and (self.source_type != "manual" or not self.user_confirmed):
            raise ValueError("实际收入和成本只能由用户确认的 manual 指标录入")
        return self


MetricRequest = MetricCreateRequest


class MetricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    output_id: int
    schedule_id: int
    source_type: MetricSourceType
    views: int = Field(ge=0)
    likes: int = Field(ge=0)
    comments: int = Field(ge=0)
    collects: int = Field(ge=0)
    shares: int = Field(ge=0)
    actual_revenue: float | None = Field(default=None, ge=0)
    actual_cost: float | None = Field(default=None, ge=0)
    user_confirmed: bool
    idempotency_key: str
    collected_at: datetime
    created_at: datetime


class CollectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)
    metrics: MetricCreateRequest | None = None


class CollectionRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: MetricCreateRequest | None = None


CollectionRequest = CollectionCreateRequest


class CollectionJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    schedule_id: int
    status: CollectionStatus
    idempotency_key: str
    result_json: dict[str, Any] | list[Any] | str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class OutputDeleteRead(BaseModel):
    id: int
    blogger_id: int
    status: Literal["deleted"]
    deleted_at: datetime | None = None


__all__ = [
    "AssetPlaceRead",
    "CollectionCreateRequest",
    "CollectionJobRead",
    "CollectionRequest",
    "CollectionRetryRequest",
    "CollectionStatus",
    "MetricCreateRequest",
    "MetricRead",
    "MetricRequest",
    "MetricSourceType",
    "OutputAssetRead",
    "OutputBase",
    "OutputCreateRequest",
    "OutputDeleteRead",
    "OutputEditRequest",
    "OutputGenerateRequest",
    "OutputGenerationRequest",
    "OutputListItem",
    "OutputPlaceRead",
    "OutputRead",
    "OutputRevisionRequest",
    "OutputSourceRef",
    "OutputStatus",
    "OutputType",
    "OutputUpdateRequest",
    "PublishCreateRequest",
    "PublishEventRead",
    "PublishRequest",
    "PublishStatus",
    "ReminderEventRead",
    "ReminderStatus",
    "RouteGenerateRequest",
    "RouteRecommendationRequest",
    "ScheduleCreateRequest",
    "ScheduleRead",
    "ScheduleStatus",
    "ScheduleUpdateRequest",
    "ScriptGenerateRequest",
    "ScriptRequest",
    "StoryboardGenerateRequest",
    "StoryboardRequest",
]

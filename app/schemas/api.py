from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BloggerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str
    content_types: list[str] = Field(min_length=1)
    style: str
    follower_band: str
    monetization_types: list[str] = Field(min_length=1)
    routes: str | None = None
    viral_topic: str | None = None
    frequency: str | None = None


class ProfileBatchFormatRequest(BloggerCreate):
    """一次提交的结构化画像表单，由 Agent 统一规范格式后等待确认。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)


class ProfileBatchConfirmRequest(BloggerCreate):
    """用户核对格式化预览后一次提交的最终画像。"""

    model_config = ConfigDict(extra="forbid")


class BloggerRead(BloggerCreate):
    id: int
    profile_state: str


class BloggerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: str | None = Field(default=None, min_length=1, max_length=50)
    content_types: list[str] | None = Field(default=None, min_length=1)
    style: str | None = Field(default=None, min_length=1, max_length=50)
    follower_band: str | None = Field(default=None, min_length=1, max_length=50)
    monetization_types: list[str] | None = Field(default=None, min_length=1)
    routes: str | None = None
    viral_topic: str | None = None
    frequency: str | None = None
    suit_type: str | None = None


class BuildRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


class BuildRead(BaseModel):
    id: int
    status: str
    output_summary: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


class AssetRead(BaseModel):
    id: int
    lib_type: str
    category: str
    title: str
    content: str
    tags: list[str]
    source_type: str
    credibility: int
    similarity: float | None = None
    sources: list[dict]


class AssetCreate(BaseModel):
    lib_type: Literal["knowledge", "material", "algorithm"]
    category: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    source_type: str = Field(min_length=1, max_length=50)
    source_url: str | None = None
    source_title: str | None = None
    publisher: str | None = None
    verified_at: str | None = None
    credibility: int = Field(ge=0, le=5)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


class AssetUpdate(BaseModel):
    lib_type: Literal["knowledge", "material", "algorithm"] | None = None
    title: str | None = None
    content: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    publisher: str | None = None
    verified_at: str | None = None
    credibility: int | None = Field(default=None, ge=0, le=5)


class ConversationMessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    request_id: str | None = Field(default=None, min_length=8, max_length=100)


class ProfileCorrection(BaseModel):
    field: Literal[
        "name",
        "platform",
        "content_types",
        "style",
        "follower_band",
        "monetization_types",
        "routes",
        "viral_topic",
        "frequency",
    ]
    value: str = Field(min_length=1, max_length=2000)


class ConversationReply(BaseModel):
    session_id: int
    status: Literal["collecting", "confirming", "completed"]
    question: str | None
    collected_profile: dict

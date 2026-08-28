from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class BloggerRead(BloggerCreate):
    id: int
    profile_state: str


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


class AssetUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None
    tags: list[str] | None = None


class ConversationMessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


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

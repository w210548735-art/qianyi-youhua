from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    Asset,
    AssetEmbedding,
    Blogger,
    ConversationMessage,
    ConversationSession,
    DecisionLog,
)
from app.schemas.api import (
    AssetUpdate,
    BloggerCreate,
    BuildRequest,
    ConversationMessageCreate,
    ProfileCorrection,
)
from app.services.build_service import LibraryBuildService
from app.services.embedding_service import EmbeddingService
from app.services.memory_service import MemoryService
from app.services.search_service import AssetSearchService

router = APIRouter(prefix="/api/v1")

QUESTION_ORDER = [
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
QUESTIONS = {
    "name": "请先告诉我你的博主名称。",
    "platform": "你主要在哪个平台创作？例如抖音、小红书、B站或多平台。",
    "content_types": "你主要创作哪些内容？多个方向可用逗号分隔。",
    "style": "你的主要创作风格是什么？例如口播、vlog、测评。",
    "follower_band": "你的粉丝量级是？例如1k以下、1k-1万、1万-10万、10万以上。",
    "monetization_types": "你目前的变现方式有哪些？多个方式可用逗号分隔。",
    "routes": "你常跑哪些地区或路线？没有可回答“无”。",
    "viral_topic": "最近有没有表现较好的内容？没有可回答“无”。",
    "frequency": "你的更新频率是日更、周更还是不定期？",
}
AMBIGUOUS_ANSWERS = {"不知道", "不清楚", "随便", "都行", "不确定"}


def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()


def split_values(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def blogger_to_dict(blogger: Blogger) -> dict:
    return {
        "id": blogger.id,
        "name": blogger.name,
        "platform": blogger.platform,
        "content_types": json.loads(blogger.content_types_json),
        "style": blogger.style,
        "follower_band": blogger.follower_band,
        "monetization_types": json.loads(blogger.monetization_types_json),
        "routes": blogger.routes,
        "viral_topic": blogger.viral_topic,
        "frequency": blogger.frequency,
        "profile_state": blogger.profile_state,
    }


@router.get("/health")
def health(embedding: EmbeddingService = Depends(get_embedding_service)) -> dict:
    return {
        "status": "ok",
        "embedding_model": embedding.model_name,
        "embedding_device": embedding.device,
    }


@router.post("/profile-sessions")
def start_profile_session(db: Session = Depends(get_db)) -> dict:
    session = ConversationSession()
    db.add(session)
    db.flush()
    db.add(
        ConversationMessage(
            session_id=session.id,
            role="assistant",
            content=QUESTIONS[QUESTION_ORDER[0]],
        )
    )
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "question": QUESTIONS[QUESTION_ORDER[0]],
        "collected_profile": {},
    }


@router.post("/profile-sessions/{session_id}/messages")
def answer_profile_question(
    session_id: int,
    body: ConversationMessageCreate,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(404, "画像会话不存在")
    if session.status != "collecting":
        raise HTTPException(409, "画像会话已经完成采集")
    profile = json.loads(session.collected_profile_json)
    field = session.current_question
    clarifications = profile.setdefault("_clarifications", {})
    normalized_answer = body.message.strip()
    if normalized_answer in AMBIGUOUS_ANSWERS and clarifications.get(field, 0) == 0:
        clarifications[field] = 1
        session.collected_profile_json = json.dumps(profile, ensure_ascii=False)
        clarification = f"请尽量具体说明“{QUESTIONS[field]}”；如仍不确定，可再次回答原内容。"
        db.add(ConversationMessage(session_id=session.id, role="user", content=body.message))
        db.add(ConversationMessage(session_id=session.id, role="assistant", content=clarification))
        db.commit()
        return {
            "session_id": session.id,
            "status": session.status,
            "question": clarification,
            "collected_profile": {key: value for key, value in profile.items() if not key.startswith("_")},
        }
    profile[field] = (
        split_values(body.message) if field in {"content_types", "monetization_types"} else body.message.strip()
    )
    db.add(ConversationMessage(session_id=session.id, role="user", content=body.message))
    index = QUESTION_ORDER.index(field)
    if index + 1 < len(QUESTION_ORDER):
        next_field = QUESTION_ORDER[index + 1]
        session.current_question = next_field
        next_question = QUESTIONS[next_field]
        db.add(ConversationMessage(session_id=session.id, role="assistant", content=next_question))
    else:
        session.status = "confirming"
        next_question = None
    session.collected_profile_json = json.dumps(profile, ensure_ascii=False)
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "question": next_question,
        "collected_profile": profile,
    }


@router.put("/profile-sessions/{session_id}/profile")
def correct_profile(
    session_id: int,
    body: ProfileCorrection,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(404, "画像会话不存在")
    if session.status != "confirming":
        raise HTTPException(409, "只有待确认画像允许修正")
    profile = json.loads(session.collected_profile_json)
    profile[body.field] = (
        split_values(body.value) if body.field in {"content_types", "monetization_types"} else body.value.strip()
    )
    session.collected_profile_json = json.dumps(profile, ensure_ascii=False)
    db.add(
        ConversationMessage(
            session_id=session.id,
            role="user",
            content=f"修正画像字段 {body.field}：{body.value}",
        )
    )
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "question": None,
        "collected_profile": {key: value for key, value in profile.items() if not key.startswith("_")},
    }


@router.post("/profile-sessions/{session_id}/confirm")
def confirm_profile(
    session_id: int,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(404, "画像会话不存在")
    if session.status == "completed" and session.blogger_id:
        completed_blogger = db.get(Blogger, session.blogger_id)
        if completed_blogger is None:
            raise HTTPException(409, "画像已确认但博主记录不存在")
        return blogger_to_dict(completed_blogger)
    if session.status != "confirming":
        raise HTTPException(409, "画像必填信息尚未采集完成")
    profile = {
        key: value for key, value in json.loads(session.collected_profile_json).items() if not key.startswith("_")
    }
    required = QUESTION_ORDER[:6]
    if any(not profile.get(field) for field in required):
        raise HTTPException(422, "画像必填信息不完整")
    blogger = Blogger(
        name=profile["name"],
        platform=profile["platform"],
        content_types_json=json.dumps(profile["content_types"], ensure_ascii=False),
        style=profile["style"],
        follower_band=profile["follower_band"],
        monetization_types_json=json.dumps(profile["monetization_types"], ensure_ascii=False),
        routes=None if profile.get("routes") in {None, "无"} else profile.get("routes"),
        viral_topic=None if profile.get("viral_topic") in {None, "无"} else profile.get("viral_topic"),
        frequency=profile.get("frequency"),
    )
    db.add(blogger)
    db.flush()
    decision = DecisionLog(
        blogger_id=blogger.id,
        decision_type="profile",
        prompt_version="phase1-state-machine-v1",
        input_summary=session.collected_profile_json,
        decision=json.dumps({"blogger_id": blogger.id}, ensure_ascii=False),
        reason="用户逐项回答并确认结构化画像",
    )
    db.add(decision)
    session.status = "completed"
    session.blogger_id = blogger.id
    session.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(blogger)
    result = blogger_to_dict(blogger)
    try:
        memory = MemoryService(db, embedding=embedding)
        profile_memory = memory.sync_profile(blogger.id, user_confirmed=True)
        decision_memories = memory.sync_decisions(
            blogger.id,
            decisions=[decision.id],
            user_confirmed=True,
        )
        result["memory_sync"] = {
            "status": "succeeded",
            "profile_memory_id": profile_memory.id,
            "decision_memory_ids": [item.id for item in decision_memories],
        }
    except Exception as exc:
        result["memory_sync"] = {
            "status": "failed",
            "error_code": exc.__class__.__name__,
        }
    return result


@router.post("/bloggers")
def create_blogger(
    body: BloggerCreate,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    blogger = Blogger(
        name=body.name,
        platform=body.platform,
        content_types_json=json.dumps(body.content_types, ensure_ascii=False),
        style=body.style,
        follower_band=body.follower_band,
        monetization_types_json=json.dumps(body.monetization_types, ensure_ascii=False),
        routes=body.routes,
        viral_topic=body.viral_topic,
        frequency=body.frequency,
    )
    db.add(blogger)
    db.flush()
    decision = DecisionLog(
        blogger_id=blogger.id,
        decision_type="profile",
        prompt_version="phase1-direct-v1",
        input_summary=json.dumps(body.model_dump(), ensure_ascii=False),
        decision=json.dumps({"blogger_id": blogger.id}, ensure_ascii=False),
        reason="用户通过结构化表单明确提交并确认画像",
    )
    db.add(decision)
    db.commit()
    db.refresh(blogger)
    result = blogger_to_dict(blogger)
    try:
        memory = MemoryService(db, embedding=embedding)
        profile_memory = memory.sync_profile(blogger.id, user_confirmed=True)
        decision_memories = memory.sync_decisions(
            blogger.id,
            decisions=[decision.id],
            user_confirmed=True,
        )
        result["memory_sync"] = {
            "status": "succeeded",
            "profile_memory_id": profile_memory.id,
            "decision_memory_ids": [item.id for item in decision_memories],
        }
    except Exception as exc:
        result["memory_sync"] = {
            "status": "failed",
            "error_code": exc.__class__.__name__,
        }
    return result


@router.get("/bloggers")
def list_bloggers(db: Session = Depends(get_db)) -> list[dict]:
    return [blogger_to_dict(row) for row in db.scalars(select(Blogger).order_by(Blogger.id.desc()))]


@router.get("/bloggers/{blogger_id}")
def get_blogger(blogger_id: int, db: Session = Depends(get_db)) -> dict:
    blogger = db.get(Blogger, blogger_id)
    if blogger is None:
        raise HTTPException(404, "博主不存在")
    return blogger_to_dict(blogger)


@router.post("/bloggers/{blogger_id}/build-runs")
def build_library(blogger_id: int, body: BuildRequest, db: Session = Depends(get_db)) -> dict:
    service = LibraryBuildService(db)
    try:
        run = service.start_build(blogger_id, body.idempotency_key)
        run = service.execute_build(run.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    response = {
        "id": run.id,
        "status": run.status,
        "output_summary": json.loads(run.output_summary) if run.output_summary else None,
        "error_code": run.error_code,
        "error_message": run.error_message,
    }
    if run.status == "succeeded":
        try:
            memory = MemoryService(db, embedding=service.embedding)
            verified = memory.sync_verified_assets(blogger_id)
            decisions = memory.sync_decisions(blogger_id, user_confirmed=False)
            response["memory_sync"] = {
                "status": "succeeded",
                "verified_count": len(verified),
                "decision_candidate_count": len(decisions),
            }
        except Exception as exc:
            response["memory_sync"] = {
                "status": "failed",
                "error_code": exc.__class__.__name__,
            }
    return response


@router.get("/bloggers/{blogger_id}/assets")
def search_assets(
    blogger_id: int,
    q: str | None = Query(default=None, max_length=200),
    lib_type: str | None = None,
    category: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> list[dict]:
    return AssetSearchService(db, embedding).search(
        blogger_id,
        q,
        lib_type,
        category,
        page_size,
        (page - 1) * page_size,
    )


@router.put("/assets/{asset_id}")
def update_asset(
    asset_id: int,
    body: AssetUpdate,
    db: Session = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(404, "资产不存在")
    for field in ("title", "content", "category"):
        value = getattr(body, field)
        if value is not None:
            setattr(asset, field, value)
    if body.tags is not None:
        asset.tags_json = json.dumps(body.tags, ensure_ascii=False)
    asset.manual_locked = True
    tags_text = " ".join(json.loads(asset.tags_json))
    text = f"标题：{asset.title}\n分类：{asset.category}\n内容：{asset.content}\n标签：{tags_text}"
    result = embedding_service.encode_documents([text])[0]
    embedding = db.get(AssetEmbedding, asset.id)
    if embedding is None:
        embedding = AssetEmbedding(asset_id=asset.id)
        db.add(embedding)
    embedding.model_name = embedding_service.model_name
    embedding.model_version = "v1.5"
    embedding.dimension = len(result.vector)
    embedding.vector = embedding_service.to_bytes(result.vector)
    embedding.vector_norm = 1.0
    embedding.content_hash = result.content_hash
    db.commit()
    updated = AssetSearchService(db).get(asset.blogger_id, asset.id)
    if updated is None:
        raise HTTPException(500, "资产更新后读取失败")
    return updated


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(404, "资产不存在")
    asset.deleted_at = datetime.utcnow()
    asset.manual_locked = True
    db.commit()
    return {"deleted": True, "asset_id": asset_id}


@router.get("/bloggers/{blogger_id}/decisions")
def list_decisions(blogger_id: int, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(DecisionLog).where(DecisionLog.blogger_id == blogger_id).order_by(DecisionLog.id.desc()))
    return [
        {
            "id": row.id,
            "decision_type": row.decision_type,
            "decision": json.loads(row.decision),
            "reason": row.reason,
            "prompt_version": row.prompt_version,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]

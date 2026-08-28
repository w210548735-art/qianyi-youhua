from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    Asset,
    Blogger,
    ConversationMessage,
    ConversationSession,
    DecisionLog,
)
from app.schemas.api import (
    AssetCreate,
    AssetUpdate,
    BloggerCreate,
    BloggerUpdate,
    BuildRequest,
    ConversationMessageCreate,
    ProfileCorrection,
)
from app.services.asset_service import (
    AssetConflictError,
    AssetNotFoundError,
    AssetService,
    AssetValidationError,
)
from app.services.blogger_service import (
    BloggerNotFoundError,
    BloggerService,
    BloggerValidationError,
)
from app.services.build_service import LibraryBuildService
from app.services.embedding_service import EmbeddingService
from app.services.memory_service import MemoryService
from app.services.profile_agent import (
    DeepSeekProfileAgent,
    ProfileAgent,
    ProfileAgentError,
)
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


def get_profile_agent() -> ProfileAgent:
    return DeepSeekProfileAgent()


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
        "deleted_at": blogger.deleted_at.isoformat() if blogger.deleted_at else None,
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
    profile_agent: ProfileAgent = Depends(get_profile_agent),
) -> dict:
    session = db.get(ConversationSession, session_id)
    if session is None:
        raise HTTPException(404, "画像会话不存在")
    profile = json.loads(session.collected_profile_json)
    field = session.current_question
    clarifications = profile.setdefault("_clarifications", {})
    processed_requests = profile.setdefault("_processed_requests", {})
    request_id = body.request_id or hashlib.sha256(
        f"{session.id}|{field}|{session.collected_profile_json}|{body.message}".encode("utf-8")
    ).hexdigest()
    cached = processed_requests.get(request_id)
    if isinstance(cached, dict):
        return cached
    if session.status != "collecting":
        raise HTTPException(409, "画像会话已经完成采集")

    visible_profile = {key: value for key, value in profile.items() if not key.startswith("_")}
    conversation = [
        {"role": row.role, "content": row.content}
        for row in db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session.id)
            .order_by(ConversationMessage.id)
        )
    ]
    try:
        agent_result = profile_agent.extract(
            body.message,
            visible_profile,
            request_id=request_id,
            current_field=field,
            conversation=conversation,
        )
    except ProfileAgentError as exc:
        db.add(ConversationMessage(session_id=session.id, role="user", content=body.message))
        db.add(
            ConversationMessage(
                session_id=session.id,
                role="assistant",
                content="画像 Agent 暂时不可用，已保留当前进度，请使用相同 request_id 重试。",
            )
        )
        db.add(
            DecisionLog(
                blogger_id=None,
                decision_type="profile_agent_failure",
                prompt_version="deepseek-v4-flash",
                input_summary=json.dumps(
                    {"session_id": session.id, "request_id": request_id, "profile": visible_profile},
                    ensure_ascii=False,
                ),
                decision=json.dumps(
                    {"error_code": exc.code, "retryable": exc.retryable},
                    ensure_ascii=False,
                ),
                reason="画像 Agent 调用失败，保留原会话和已采集字段并允许重试",
            )
        )
        db.commit()
        raise HTTPException(
            503,
            {
                "error_code": exc.code,
                "retryable": exc.retryable,
                "request_id": request_id,
            },
        ) from exc

    profile.update(agent_result.extracted_fields)
    ambiguous_current = field in agent_result.ambiguous_fields or body.message.strip() in AMBIGUOUS_ANSWERS
    if ambiguous_current and clarifications.get(field, 0) == 0:
        clarifications[field] = 1
        clarification = agent_result.follow_up_question or (
            f"请尽量具体说明“{QUESTIONS[field]}”；如仍不确定，可再次回答原内容。"
        )
        db.add(ConversationMessage(session_id=session.id, role="user", content=body.message))
        db.add(ConversationMessage(session_id=session.id, role="assistant", content=clarification))
        response = {
            "session_id": session.id,
            "status": session.status,
            "question": clarification,
            "request_id": request_id,
            "retryable": True,
            "collected_profile": {
                key: value for key, value in profile.items() if not key.startswith("_")
            },
        }
        processed_requests[request_id] = response
        session.collected_profile_json = json.dumps(profile, ensure_ascii=False)
        db.add(
            DecisionLog(
                blogger_id=None,
                decision_type="profile_agent_turn",
                prompt_version="deepseek-v4-flash",
                input_summary=json.dumps(
                    {"session_id": session.id, "request_id": request_id, "message": body.message},
                    ensure_ascii=False,
                ),
                decision=json.dumps(agent_result.to_dict(), ensure_ascii=False),
                reason="画像 Agent 判定当前字段模糊并执行唯一一次定向追问",
            )
        )
        db.commit()
        return response

    if field not in profile:
        profile[field] = (
            split_values(body.message)
            if field in {"content_types", "monetization_types"}
            else body.message.strip()
        )
    db.add(ConversationMessage(session_id=session.id, role="user", content=body.message))
    remaining = [item for item in QUESTION_ORDER if not profile.get(item)]
    if remaining:
        next_field = remaining[0]
        session.current_question = next_field
        next_question = agent_result.follow_up_question or QUESTIONS[next_field]
        db.add(ConversationMessage(session_id=session.id, role="assistant", content=next_question))
    else:
        session.status = "confirming"
        next_question = None
    db.add(
        DecisionLog(
            blogger_id=None,
            decision_type="profile_agent_turn",
            prompt_version="deepseek-v4-flash",
            input_summary=json.dumps(
                {"session_id": session.id, "request_id": request_id, "message": body.message},
                ensure_ascii=False,
            ),
            decision=json.dumps(
                {
                    **agent_result.to_dict(),
                    "next_field": session.current_question if session.status == "collecting" else None,
                },
                ensure_ascii=False,
            ),
            reason="画像 Agent 抽取本轮明确字段，状态机继续控制必填项和确认边界",
        )
    )
    response = {
        "session_id": session.id,
        "status": session.status,
        "question": next_question,
        "request_id": request_id,
        "retryable": False,
        "collected_profile": {
            key: value for key, value in profile.items() if not key.startswith("_")
        },
    }
    processed_requests[request_id] = response
    session.collected_profile_json = json.dumps(profile, ensure_ascii=False)
    db.commit()
    return response


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
    return [blogger_to_dict(row) for row in BloggerService(db).list_active()]


@router.get("/bloggers/{blogger_id}")
def get_blogger(blogger_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        blogger = BloggerService(db).get_active(blogger_id)
    except BloggerNotFoundError:
        raise HTTPException(404, "博主不存在") from None
    return blogger_to_dict(blogger)


@router.put("/bloggers/{blogger_id}")
def update_blogger(
    blogger_id: int,
    body: BloggerUpdate,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    try:
        blogger = BloggerService(
            db,
            memory_service=MemoryService(db, embedding=embedding),
        ).update_confirmed_profile(blogger_id, body.model_dump(exclude_unset=True))
    except BloggerNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except BloggerValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return blogger_to_dict(blogger)


@router.delete("/bloggers/{blogger_id}")
def delete_blogger(blogger_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        blogger = BloggerService(db).soft_delete(blogger_id)
    except BloggerNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "deleted": True,
        "blogger_id": blogger.id,
        "deleted_at": blogger.deleted_at.isoformat() if blogger.deleted_at else None,
    }


@router.post("/bloggers/{blogger_id}/build-runs")
def build_library(blogger_id: int, body: BuildRequest, db: Session = Depends(get_db)) -> dict:
    try:
        BloggerService(db).get_active(blogger_id)
    except BloggerNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
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
    tags: list[str] | None = Query(default=None),
    source_type: str | None = None,
    source: str | None = Query(default=None, max_length=200),
    min_credibility: int | None = Query(default=None, ge=0, le=5),
    max_credibility: int | None = Query(default=None, ge=0, le=5),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> list[dict]:
    if (
        min_credibility is not None
        and max_credibility is not None
        and min_credibility > max_credibility
    ):
        raise HTTPException(422, "CREDIBILITY_RANGE_INVALID")
    try:
        BloggerService(db).get_active(blogger_id)
    except BloggerNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return AssetSearchService(db, embedding).search(
        blogger_id=blogger_id,
        query=q,
        lib_type=lib_type,
        category=category,
        limit=page_size,
        offset=(page - 1) * page_size,
        tags=tags,
        source_type=source_type,
        source=source,
        min_credibility=min_credibility,
        max_credibility=max_credibility,
    )


def _asset_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AssetNotFoundError, BloggerNotFoundError)):
        return HTTPException(404, str(exc))
    if isinstance(exc, AssetConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, AssetValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(422, "ASSET_OPERATION_FAILED")


@router.post("/bloggers/{blogger_id}/assets")
def create_asset(
    blogger_id: int,
    body: AssetCreate,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    try:
        asset = AssetService(
            db,
            embedding=embedding,
            memory_service=MemoryService(db, embedding=embedding),
        ).create_manual(blogger_id, body.model_dump())
    except (AssetNotFoundError, AssetConflictError, AssetValidationError, BloggerNotFoundError) as exc:
        raise _asset_error(exc) from exc
    result = AssetSearchService(db, embedding).get(blogger_id, asset.id)
    if result is None:
        raise HTTPException(500, "ASSET_READ_AFTER_CREATE_FAILED")
    return result


@router.get("/bloggers/{blogger_id}/assets/{asset_id}")
def get_asset(
    blogger_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    try:
        AssetService(db, embedding=embedding).get(blogger_id, asset_id)
    except (AssetNotFoundError, BloggerNotFoundError) as exc:
        raise _asset_error(exc) from exc
    result = AssetSearchService(db, embedding).get(blogger_id, asset_id)
    if result is None:
        raise HTTPException(404, "ASSET_NOT_FOUND")
    return result


@router.put("/bloggers/{blogger_id}/assets/{asset_id}")
def update_scoped_asset(
    blogger_id: int,
    asset_id: int,
    body: AssetUpdate,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    try:
        asset = AssetService(
            db,
            embedding=embedding,
            memory_service=MemoryService(db, embedding=embedding),
        ).update_manual(blogger_id, asset_id, body.model_dump(exclude_unset=True))
    except (AssetNotFoundError, AssetConflictError, AssetValidationError, BloggerNotFoundError) as exc:
        raise _asset_error(exc) from exc
    result = AssetSearchService(db, embedding).get(blogger_id, asset.id)
    if result is None:
        raise HTTPException(500, "ASSET_READ_AFTER_UPDATE_FAILED")
    return result


@router.delete("/bloggers/{blogger_id}/assets/{asset_id}")
def delete_scoped_asset(
    blogger_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    embedding: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    try:
        asset = AssetService(db, embedding=embedding).soft_delete(blogger_id, asset_id)
    except (AssetNotFoundError, BloggerNotFoundError) as exc:
        raise _asset_error(exc) from exc
    return {
        "deleted": True,
        "asset_id": asset.id,
        "deleted_at": asset.deleted_at.isoformat() if asset.deleted_at else None,
    }


@router.put("/assets/{asset_id}")
def update_asset(
    asset_id: int,
    body: AssetUpdate,
    db: Session = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> dict:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    return update_scoped_asset(
        asset.blogger_id,
        asset_id,
        body,
        db,
        embedding_service,
    )


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    try:
        deleted = AssetService(db).soft_delete(asset.blogger_id, asset_id)
    except (AssetNotFoundError, BloggerNotFoundError) as exc:
        raise _asset_error(exc) from exc
    return {
        "deleted": True,
        "asset_id": deleted.id,
        "deleted_at": deleted.deleted_at.isoformat() if deleted.deleted_at else None,
    }


@router.get("/bloggers/{blogger_id}/decisions")
def list_decisions(blogger_id: int, db: Session = Depends(get_db)) -> list[dict]:
    try:
        BloggerService(db).get_active(blogger_id)
    except BloggerNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
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

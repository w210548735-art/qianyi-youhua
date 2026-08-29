from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService, ChatServiceError

router = APIRouter(prefix="/api/v1", tags=["chat"])


def get_chat_service(db: Session = Depends(get_db)) -> ChatService:
    """生产运行始终构造使用真实 DeepSeek 配置的 ChatService。"""

    return ChatService(db)


@router.post("/bloggers/{blogger_id}/chat", response_model=ChatResponse)
def chat_with_blog_assistant(
    blogger_id: int,
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    try:
        result = service.chat(
            blogger_id,
            body.message,
            [item.model_dump() for item in body.conversation],
            request_id=body.request_id,
        )
        return ChatResponse(**result)
    except ChatServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error_code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "request_id": exc.request_id,
            },
        ) from exc

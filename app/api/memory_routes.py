"""第一阶段记忆系统 REST API。

本路由把任务短期记忆、长期记忆和 Agent 上下文组装暴露给演示页和后续
客户端。所有任务和长期记忆的读写都带有 ``blogger_id``，避免只凭资源 ID
访问到其他博主的数据。
"""

# FastAPI 的依赖注入需要在函数签名中调用 Depends。
# ruff: noqa: B008

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models import (
    Blogger,
    DecisionLog,
    MemoryRecord,
    SessionMessage,
    TaskArtifact,
    TaskCheckpoint,
    TaskSession,
)
from app.services.context_service import ContextService
from app.services.memory_service import (
    MemoryConfirmationRequiredError,
    MemoryEmbeddingError,
    MemoryNotFoundError,
    MemoryService,
    MemoryServiceError,
    MemoryValidationError,
)
from app.services.task_memory_service import (
    InvalidTaskStateError,
    TaskMemoryError,
    TaskMemoryService,
    TaskNotFoundError,
)

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class TaskCreateRequest(BaseModel):
    """创建短期任务的请求。博主 ID 来自 URL，避免正文和路径不一致。"""

    task_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    initial_context: str = Field(default="", max_length=50000)
    metadata: dict[str, Any] | None = None


class TaskMessageRequest(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=50000)


class TaskDecisionRequest(BaseModel):
    decision: Any
    reason: str = Field(default="", max_length=10000)
    input_summary: Any = ""
    decision_type: str = Field(default="task", min_length=1, max_length=50)
    prompt_version: str = Field(default="phase1-v1", min_length=1, max_length=100)


class TaskCheckpointRequest(BaseModel):
    state: dict[str, Any] | list[Any] | str
    context_snapshot: str | None = Field(default=None, max_length=50000)


class TaskCompleteRequest(BaseModel):
    final_summary: dict[str, Any] = Field(default_factory=dict)
    memory_candidates: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["completed", "succeeded", "failed", "cancelled"] = "completed"


class MemoryCreateRequest(BaseModel):
    memory_type: Literal[
        "profile_fact",
        "user_preference",
        "verified_knowledge",
        "decision_summary",
    ]
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=100000)
    source_type: str = Field(min_length=1, max_length=50)
    source_id: str | int | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: Literal["candidate", "active"] = "candidate"
    user_confirmed: bool = False


class MemoryPromoteRequest(BaseModel):
    user_confirmed: bool = False


class ContextRequest(BaseModel):
    user_input: str = Field(min_length=1, max_length=50000)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    top_k: int = Field(default=5, ge=1, le=50)
    recent_message_limit: int = Field(default=6, ge=0, le=50)


def get_task_memory_service(db: Session = Depends(get_db)) -> TaskMemoryService:
    """提供任务服务；测试可以覆盖此依赖以注入临时目录。"""

    return TaskMemoryService(db, settings.tasks_root)


def get_memory_service(db: Session = Depends(get_db)) -> MemoryService:
    """提供长期记忆服务；测试可以覆盖此依赖以注入 FakeEmbeddingService。"""

    return MemoryService(db)


def get_context_service(
    db: Session = Depends(get_db),
    memory_service: MemoryService = Depends(get_memory_service),
) -> ContextService:
    """构造受控上下文组装器，不把全部历史数据塞入提示词。"""

    return ContextService(db, memory_search=memory_service)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_value(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _blogger_or_404(db: Session, blogger_id: int) -> Blogger:
    blogger = db.scalar(
        select(Blogger).where(
            Blogger.id == blogger_id,
            Blogger.deleted_at.is_(None),
        )
    )
    if blogger is None:
        raise HTTPException(status_code=404, detail="BLOGGER_NOT_FOUND")
    return blogger


def _task_or_404(
    db: Session,
    task_service: TaskMemoryService,
    blogger_id: int,
    task_id: str,
) -> TaskSession:
    _blogger_or_404(db, blogger_id)
    try:
        task = task_service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc
    # 对不存在的归属统一返回 404，避免任务 ID 探测泄漏其他博主数据。
    if task.blogger_id != blogger_id:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    return task


def _task_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TaskNotFoundError):
        return HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    if isinstance(exc, InvalidTaskStateError):
        return HTTPException(status_code=409, detail=str(exc) or "TASK_STATE_CONFLICT")
    if isinstance(exc, (ValueError, TaskMemoryError)):
        return HTTPException(status_code=422, detail=str(exc) or "TASK_REQUEST_INVALID")
    return HTTPException(status_code=422, detail="TASK_OPERATION_FAILED")


def _memory_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc) or "MEMORY_NOT_FOUND")
    if isinstance(exc, MemoryConfirmationRequiredError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (MemoryEmbeddingError, MemoryValidationError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc) or "MEMORY_REQUEST_INVALID")
    if isinstance(exc, MemoryServiceError):
        return HTTPException(status_code=422, detail=str(exc) or "MEMORY_OPERATION_FAILED")
    return HTTPException(status_code=422, detail="MEMORY_OPERATION_FAILED")


def _task_payload(db: Session, task: TaskSession) -> dict[str, Any]:
    """将任务及其日志序列化为演示页可直接显示的结构。"""

    messages = db.scalars(
        select(SessionMessage)
        .where(SessionMessage.task_id == task.id)
        .order_by(SessionMessage.sequence, SessionMessage.id)
    )
    checkpoints = db.scalars(
        select(TaskCheckpoint)
        .where(TaskCheckpoint.task_id == task.id)
        .order_by(TaskCheckpoint.sequence, TaskCheckpoint.id)
    )
    artifacts = db.scalars(select(TaskArtifact).where(TaskArtifact.task_id == task.id).order_by(TaskArtifact.id))
    decisions: list[dict[str, Any]] = []
    decision_rows = db.scalars(
        select(DecisionLog)
        .where(
            DecisionLog.blogger_id == task.blogger_id,
            DecisionLog.decision_type.like("task:%"),
        )
        .order_by(DecisionLog.id)
    )
    for row in decision_rows:
        envelope = _json_value(row.input_summary, {})
        if not isinstance(envelope, dict) or envelope.get("task_id") != task.id:
            continue
        decisions.append(
            {
                "id": row.id,
                "sequence": envelope.get("sequence"),
                "decision_type": row.decision_type.removeprefix("task:"),
                "prompt_version": row.prompt_version,
                "input_summary": envelope.get("input"),
                "decision": _json_value(row.decision),
                "reason": row.reason,
                "created_at": _iso(row.created_at),
            }
        )
    decisions.sort(key=lambda item: (item["sequence"] or 0, item["id"]))
    final_summary: dict[str, Any] = {}
    summary_path = Path(task.task_dir) / "final_summary.json"
    if summary_path.exists():
        loaded_summary = _json_value(summary_path.read_text(encoding="utf-8"), {})
        if isinstance(loaded_summary, dict):
            final_summary = loaded_summary
    return {
        "task_id": task.id,
        "blogger_id": task.blogger_id,
        "task_type": task.task_type,
        "title": task.title,
        "status": task.status,
        "current_context": task.current_context,
        "recovery_state": _json_value(task.recovery_state_json, {}),
        "task_dir": task.task_dir,
        "started_at": _iso(task.started_at),
        "updated_at": _iso(task.updated_at),
        "completed_at": _iso(task.completed_at),
        "final_summary": final_summary,
        "messages": [
            {
                "id": row.id,
                "sequence": row.sequence,
                "role": row.role,
                "content": row.content,
                "created_at": _iso(row.created_at),
            }
            for row in messages
        ],
        "decisions": decisions,
        "checkpoints": [
            {
                "id": row.id,
                "sequence": row.sequence,
                "state": _json_value(row.state_json),
                "context_snapshot": row.context_snapshot,
                "created_at": _iso(row.created_at),
            }
            for row in checkpoints
        ],
        "artifacts": [
            {
                "id": row.id,
                "artifact_type": row.artifact_type,
                "relative_path": row.relative_path,
                "content_hash": row.content_hash,
                "created_at": _iso(row.created_at),
            }
            for row in artifacts
        ],
    }


def _memory_payload(record: MemoryRecord, *, similarity: float | None = None) -> dict[str, Any]:
    return {
        "id": record.id,
        "blogger_id": record.blogger_id,
        "memory_type": record.memory_type,
        "title": record.title,
        "content": record.content,
        "source_type": record.source_type,
        "source_id": record.source_id,
        "confidence": record.confidence,
        "status": record.status,
        "version": record.version,
        "parent_memory_id": record.parent_memory_id,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "has_embedding": record.embedding is not None,
        **({"similarity": similarity} if similarity is not None else {}),
    }


@router.post("/bloggers/{blogger_id}/tasks")
def create_task(
    blogger_id: int,
    body: TaskCreateRequest,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _blogger_or_404(db, blogger_id)
    try:
        task = task_service.create_task(
            blogger_id,
            body.task_type,
            body.title,
            task_id=body.task_id,
            initial_context=body.initial_context,
            metadata=body.metadata,
        )
        return _task_payload(db, task)
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.get("/bloggers/{blogger_id}/tasks")
def list_tasks(
    blogger_id: int,
    include_completed: bool = False,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> list[dict[str, Any]]:
    _blogger_or_404(db, blogger_id)
    tasks = (
        task_service.list_unfinished_tasks(blogger_id)
        if not include_completed
        else list(
            db.scalars(
                select(TaskSession)
                .where(TaskSession.blogger_id == blogger_id)
                .order_by(TaskSession.updated_at.desc(), TaskSession.id)
            )
        )
    )
    return [_task_payload(db, task) for task in tasks]


@router.get("/bloggers/{blogger_id}/tasks/{task_id}")
def get_task(
    blogger_id: int,
    task_id: str,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    task = _task_or_404(db, task_service, blogger_id, task_id)
    return _task_payload(db, task)


@router.post("/bloggers/{blogger_id}/tasks/{task_id}/recover")
def recover_task(
    blogger_id: int,
    task_id: str,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _task_or_404(db, task_service, blogger_id, task_id)
    try:
        task = task_service.recover_task(task_id)
        return _task_payload(db, task)
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.post("/bloggers/{blogger_id}/tasks/{task_id}/messages")
def append_task_message(
    blogger_id: int,
    task_id: str,
    body: TaskMessageRequest,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _task_or_404(db, task_service, blogger_id, task_id)
    try:
        message = task_service.append_message(task_id, body.role, body.content)
        return {
            "id": message.id,
            "task_id": message.task_id,
            "sequence": message.sequence,
            "role": message.role,
            "content": message.content,
            "created_at": _iso(message.created_at),
        }
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.post("/bloggers/{blogger_id}/tasks/{task_id}/decisions")
def record_task_decision(
    blogger_id: int,
    task_id: str,
    body: TaskDecisionRequest,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _task_or_404(db, task_service, blogger_id, task_id)
    try:
        row = task_service.record_decision(
            task_id,
            body.decision,
            reason=body.reason,
            input_summary=body.input_summary,
            decision_type=body.decision_type,
            prompt_version=body.prompt_version,
        )
        return {
            "task_id": row.task_id,
            "sequence": row.sequence,
            "decision_type": row.decision_type,
            "prompt_version": row.prompt_version,
            "input_summary": _json_value(row.input_summary),
            "decision": _json_value(row.decision),
            "reason": row.reason,
            "database_id": row.database_id,
            "created_at": _iso(row.created_at),
        }
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.post("/bloggers/{blogger_id}/tasks/{task_id}/checkpoints")
def create_task_checkpoint(
    blogger_id: int,
    task_id: str,
    body: TaskCheckpointRequest,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _task_or_404(db, task_service, blogger_id, task_id)
    try:
        checkpoint = task_service.create_checkpoint(
            task_id,
            body.state,
            body.context_snapshot,
        )
        return {
            "id": checkpoint.id,
            "task_id": checkpoint.task_id,
            "sequence": checkpoint.sequence,
            "state": _json_value(checkpoint.state_json),
            "context_snapshot": checkpoint.context_snapshot,
            "created_at": _iso(checkpoint.created_at),
        }
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.post("/bloggers/{blogger_id}/tasks/{task_id}/complete")
def complete_task(
    blogger_id: int,
    task_id: str,
    body: TaskCompleteRequest,
    db: Session = Depends(get_db),
    task_service: TaskMemoryService = Depends(get_task_memory_service),
) -> dict[str, Any]:
    _task_or_404(db, task_service, blogger_id, task_id)
    try:
        task = task_service.complete_task(
            task_id,
            body.final_summary,
            memory_candidates=body.memory_candidates,
            status=body.status,
        )
        return _task_payload(db, task)
    except (TaskMemoryError, ValueError) as exc:
        raise _task_error(exc) from exc


@router.get("/bloggers/{blogger_id}/memories")
def list_memories(
    blogger_id: int,
    status: Literal["active", "candidate", "superseded", "all"] = Query(default="active"),
    memory_type: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
    memory_service: MemoryService = Depends(get_memory_service),
) -> list[dict[str, Any]]:
    _blogger_or_404(db, blogger_id)
    try:
        rows = memory_service.list_memories(
            blogger_id,
            status=None if status == "all" else status,
            memory_type=memory_type,
        )
        return [_memory_payload(row) for row in rows]
    except MemoryServiceError as exc:
        raise _memory_error(exc) from exc


@router.get("/bloggers/{blogger_id}/memories/search")
def search_memories(
    blogger_id: int,
    q: str = Query(min_length=1, max_length=50000),
    limit: int = Query(default=10, ge=1, le=50),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    memory_service: MemoryService = Depends(get_memory_service),
) -> list[dict[str, Any]]:
    _blogger_or_404(db, blogger_id)
    try:
        hits = memory_service.semantic_search(
            blogger_id,
            q,
            limit=limit,
            min_confidence=min_confidence,
        )
        result: list[dict[str, Any]] = []
        for hit in hits:
            record = memory_service.get_memory(int(hit["id"]))
            # 再次检查归属；服务本身已隔离，路由边界不依赖单一实现。
            if record.blogger_id != blogger_id:
                continue
            result.append(_memory_payload(record, similarity=float(hit["similarity"])))
        return result
    except (MemoryServiceError, ValueError) as exc:
        raise _memory_error(exc) from exc


@router.post("/bloggers/{blogger_id}/memories")
def create_memory(
    blogger_id: int,
    body: MemoryCreateRequest,
    db: Session = Depends(get_db),
    memory_service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    _blogger_or_404(db, blogger_id)
    try:
        record = memory_service.create_memory(
            blogger_id,
            body.memory_type,
            body.title,
            body.content,
            body.source_type,
            body.source_id,
            confidence=body.confidence,
            status=body.status,
            user_confirmed=body.user_confirmed,
        )
        return _memory_payload(record)
    except (MemoryServiceError, ValueError) as exc:
        raise _memory_error(exc) from exc


@router.post("/bloggers/{blogger_id}/memories/{memory_id}/promote")
def promote_memory(
    blogger_id: int,
    memory_id: int,
    body: MemoryPromoteRequest,
    db: Session = Depends(get_db),
    memory_service: MemoryService = Depends(get_memory_service),
) -> dict[str, Any]:
    _blogger_or_404(db, blogger_id)
    try:
        current = memory_service.get_memory(memory_id)
    except MemoryServiceError as exc:
        raise _memory_error(exc) from exc
    if current.blogger_id != blogger_id:
        raise HTTPException(status_code=404, detail="MEMORY_NOT_FOUND")
    try:
        promoted = memory_service.promote_memory(memory_id, body.user_confirmed)
        return _memory_payload(promoted)
    except (MemoryServiceError, ValueError) as exc:
        raise _memory_error(exc) from exc


@router.post("/bloggers/{blogger_id}/context")
def assemble_agent_context(
    blogger_id: int,
    body: ContextRequest,
    db: Session = Depends(get_db),
    context_service: ContextService = Depends(get_context_service),
) -> dict[str, Any]:
    _blogger_or_404(db, blogger_id)
    if body.task_id is not None:
        task = db.get(TaskSession, body.task_id)
        if task is None or task.blogger_id != blogger_id:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    result = context_service.assemble_context(
        blogger_id,
        body.user_input,
        task_id=body.task_id,
        top_k=body.top_k,
        recent_message_limit=body.recent_message_limit,
    )
    return {
        "blogger_id": result.blogger_id,
        "task_id": result.task_id,
        "messages": result.messages,
        "recalled_memories": [
            {
                "id": memory.id,
                "blogger_id": memory.blogger_id,
                "memory_type": memory.memory_type,
                "title": memory.title,
                "content": memory.content,
                "source_type": memory.source_type,
                "source_id": memory.source_id,
                "confidence": memory.confidence,
                "version": memory.version,
                "similarity": memory.similarity,
            }
            for memory in result.retrieved_memories
        ],
        "memory_search_error": result.memory_search_error,
        "task_missing": result.task_missing,
    }

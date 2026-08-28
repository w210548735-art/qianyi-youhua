"""短期任务记忆服务。

该服务负责把一个任务的数据库状态和本地任务目录保持在可恢复的状态。
任务目录是调试、演示和进程重启恢复的文件副本；数据库仍然是任务状态的
权威来源。跨数据库和文件系统无法做到单一事务，因此所有文件操作都设计
为幂等，并提供 ``sync_task_files`` 用数据库记录重建文件副本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Blogger, DecisionLog, SessionMessage, TaskArtifact, TaskCheckpoint, TaskSession


class TaskMemoryError(RuntimeError):
    """任务记忆服务的基础异常。"""


class TaskNotFoundError(TaskMemoryError):
    """任务不存在。"""


class InvalidTaskStateError(TaskMemoryError):
    """任务当前状态不允许执行请求的操作。"""


class TaskMemoryPersistenceError(TaskMemoryError):
    """数据库记录存在，但任务文件副本同步失败。"""


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ACTIVE_TASK_STATUSES = {"pending", "running", "paused", "interrupted", "recovering"}
_TERMINAL_TASK_STATUSES = {"completed", "succeeded", "failed", "cancelled"}
_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"不能序列化类型: {type(value).__name__}")


def _json_text(value: Any) -> str:
    """把 JSON 值规范化为可读且稳定的字符串。"""

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


class TaskMemoryService:
    """任务短期记忆的数据库与文件持久化服务。

    ``tasks_root`` 可注入到临时目录，生产环境默认使用 ``settings.tasks_root``。
    所有任务目录名称都由受限的任务 ID 组成，避免路径穿越。
    """

    def __init__(self, db: Session, tasks_root: str | Path | None = None) -> None:
        self.db = db
        self.tasks_root = Path(tasks_root or settings.tasks_root).resolve()

    # ------------------------------------------------------------------
    # 任务生命周期
    # ------------------------------------------------------------------
    def create_task(
        self,
        blogger_id: int,
        task_type: str,
        title: str,
        *,
        task_id: str | None = None,
        initial_context: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskSession:
        """创建任务、数据库记录和标准任务目录。

        显式传入已存在的 ``task_id`` 时返回现有任务，方便重试请求幂等。
        """

        if not task_type.strip() or not title.strip():
            raise ValueError("task_type 和 title 不能为空")
        resolved_id = task_id or str(uuid.uuid4())
        self._validate_task_id(resolved_id)
        self._ensure_active_blogger(blogger_id)

        existing = self.db.get(TaskSession, resolved_id)
        if existing is not None:
            if existing.blogger_id != blogger_id:
                raise TaskNotFoundError(f"任务不存在: {resolved_id}")
            self._ensure_task_directory(existing)
            self.sync_task_files(existing.id)
            return existing

        task_dir = self._task_dir(resolved_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_standard_files(task_dir)

        session = TaskSession(
            id=resolved_id,
            blogger_id=blogger_id,
            task_type=task_type.strip(),
            title=title.strip(),
            status="running",
            current_context=initial_context,
            recovery_state_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            task_dir=str(task_dir),
        )
        self.db.add(session)
        try:
            self.db.commit()
            self.db.refresh(session)
            self._write_task_json(session)
            self._write_context(session, initial_context)
        except Exception:
            self.db.rollback()
            # 只清理此次服务创建的空目录；已有目录的文件不做破坏性操作。
            if task_dir.exists() and not any(task_dir.iterdir()):
                task_dir.rmdir()
            raise
        return session

    # 下面的别名用于 API 层以不同命名调用同一个生命周期操作。
    create_task_session = create_task
    start_task = create_task

    def get_task(self, task_id: str) -> TaskSession:
        self._validate_task_id(task_id)
        task = self.db.get(TaskSession, task_id)
        if task is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        self._ensure_active_blogger(task.blogger_id)
        return task

    def list_unfinished_tasks(self, blogger_id: int | None = None) -> list[TaskSession]:
        statement = (
            select(TaskSession)
            .join(Blogger, Blogger.id == TaskSession.blogger_id)
            .where(
                TaskSession.status.not_in(_TERMINAL_TASK_STATUSES),
                Blogger.deleted_at.is_(None),
            )
        )
        if blogger_id is not None:
            self._ensure_active_blogger(blogger_id)
            statement = statement.where(TaskSession.blogger_id == blogger_id)
        return list(self.db.scalars(statement.order_by(TaskSession.updated_at, TaskSession.id)))

    def _ensure_active_blogger(self, blogger_id: int) -> Blogger:
        blogger = self.db.scalar(
            select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None))
        )
        if blogger is None:
            raise TaskNotFoundError(f"博主不存在: {blogger_id}")
        return blogger

    def recover_task(self, task_id: str) -> TaskSession:
        """从数据库最新检查点恢复任务上下文和文件副本。"""

        task = self.get_task(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            self.sync_task_files(task.id)
            return task

        latest = self.db.scalar(
            select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id).order_by(TaskCheckpoint.sequence.desc())
        )
        if latest is not None:
            task.current_context = latest.context_snapshot
            task.recovery_state_json = latest.state_json
        if task.status in {"interrupted", "recovering", "paused", "pending"}:
            task.status = "running"
        task.updated_at = _utcnow()
        self.db.commit()
        self.sync_task_files(task.id)
        self.db.refresh(task)
        return task

    def recover_unfinished_tasks(self, blogger_id: int | None = None) -> list[TaskSession]:
        recovered = []
        for task in self.list_unfinished_tasks(blogger_id):
            recovered.append(self.recover_task(task.id))
        return recovered

    # 兼容“服务启动时恢复”的调用命名。
    recover_pending_tasks = recover_unfinished_tasks

    # ------------------------------------------------------------------
    # 消息、决策、检查点
    # ------------------------------------------------------------------
    def append_message(
        self,
        task_id: str,
        role: str,
        content: str,
        *,
        created_at: datetime | None = None,
    ) -> SessionMessage:
        task = self.get_task(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise InvalidTaskStateError("已结束任务不能追加消息")
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"不支持的消息角色: {role}")
        if not content.strip():
            raise ValueError("消息内容不能为空")

        sequence = self._next_sequence(SessionMessage, SessionMessage.task_id, task.id)
        message = SessionMessage(
            task_id=task.id,
            sequence=sequence,
            role=role,
            content=content,
            created_at=created_at or _utcnow(),
        )
        self.db.add(message)
        task.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(message)
        self._append_jsonl_if_missing(
            self._task_dir(task.id) / "messages.jsonl",
            {
                "id": message.id,
                "sequence": message.sequence,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
            },
            identity_key="id",
        )
        self._write_task_json(task)
        return message

    add_message = append_message

    def record_decision(
        self,
        task_id: str,
        decision: Any,
        *,
        reason: str = "",
        input_summary: Any = "",
        decision_type: str = "task",
        prompt_version: str = "phase1-v1",
        created_at: datetime | None = None,
    ) -> Any:
        """持久化任务决策，并写入 decisions.jsonl。

        任务决策同时写入 ``DecisionLog``（通过 task_id/sequence 信封关联）和
        JSONL 文件；它不会因此自动晋升长期记忆。
        """

        task = self.get_task(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise InvalidTaskStateError("已结束任务不能追加决策")
        # 先从数据库重建文件副本，避免上次提交后进程中断导致序号回退。
        self._rewrite_decisions(task.id)
        row = _TaskDecisionRow(
            task_id=task.id,
            sequence=self._next_file_sequence(self._task_dir(task.id) / "decisions.jsonl"),
            decision_type=decision_type,
            prompt_version=prompt_version,
            input_summary=_json_text(input_summary),
            decision=_json_text(decision),
            reason=reason,
            created_at=created_at or _utcnow(),
        )
        database_decision = DecisionLog(
            blogger_id=task.blogger_id,
            decision_type=f"task:{row.decision_type}",
            prompt_version=row.prompt_version,
            input_summary=json.dumps(
                {
                    "task_id": task.id,
                    "sequence": row.sequence,
                    "input": _json_value(row.input_summary),
                },
                ensure_ascii=False,
            ),
            decision=row.decision,
            reason=row.reason,
            created_at=row.created_at,
        )
        self.db.add(database_decision)
        task.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(database_decision)
        row.database_id = database_decision.id
        self._append_jsonl_if_missing(
            self._task_dir(task.id) / "decisions.jsonl",
            {
                "sequence": row.sequence,
                "decision_type": row.decision_type,
                "prompt_version": row.prompt_version,
                "input_summary": _json_value(row.input_summary),
                "decision": _json_value(row.decision),
                "reason": row.reason,
                "created_at": row.created_at,
            },
            identity_key="sequence",
        )
        self._write_task_json(task)
        return row

    append_decision = record_decision

    def create_checkpoint(
        self,
        task_id: str,
        state: dict[str, Any] | list[Any] | str,
        context_snapshot: str | None = None,
        *,
        created_at: datetime | None = None,
    ) -> TaskCheckpoint:
        task = self.get_task(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise InvalidTaskStateError("已结束任务不能创建检查点")
        state_json = _json_text(state)
        context = context_snapshot if context_snapshot is not None else task.current_context
        sequence = self._next_sequence(TaskCheckpoint, TaskCheckpoint.task_id, task.id)
        checkpoint = TaskCheckpoint(
            task_id=task.id,
            sequence=sequence,
            state_json=state_json,
            context_snapshot=context,
            created_at=created_at or _utcnow(),
        )
        task.current_context = context
        task.recovery_state_json = state_json
        task.updated_at = _utcnow()
        self.db.add(checkpoint)
        self.db.commit()
        self.db.refresh(checkpoint)
        self._rewrite_checkpoints(task.id)
        self._write_context(task, context)
        self._write_task_json(task)
        return checkpoint

    add_checkpoint = create_checkpoint
    save_checkpoint = create_checkpoint

    def update_context(self, task_id: str, context: str) -> TaskSession:
        task = self.get_task(task_id)
        if task.status in _TERMINAL_TASK_STATUSES:
            raise InvalidTaskStateError("已结束任务不能更新上下文")
        task.current_context = context
        task.updated_at = _utcnow()
        self.db.commit()
        self._write_context(task, context)
        self._write_task_json(task)
        return task

    # ------------------------------------------------------------------
    # 任务产物和结束摘要
    # ------------------------------------------------------------------
    def record_artifact(
        self,
        task_id: str,
        artifact_type: str,
        filename: str,
        content: str | bytes | Path,
        *,
        encoding: str = "utf-8",
    ) -> TaskArtifact:
        task = self.get_task(task_id)
        if not artifact_type.strip():
            raise ValueError("artifact_type 不能为空")
        safe_name = self._safe_file_name(filename)
        if isinstance(content, Path):
            payload = content.read_bytes()
        elif isinstance(content, bytes):
            payload = content
        else:
            payload = content.encode(encoding)
        digest = hashlib.sha256(payload).hexdigest()

        artifacts_dir = self._task_dir(task.id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        relative = Path("artifacts") / safe_name
        target = self._safe_child(self._task_dir(task.id), relative)
        existing = self.db.scalar(
            select(TaskArtifact).where(
                TaskArtifact.task_id == task.id,
                TaskArtifact.relative_path == relative.as_posix(),
            )
        )
        if existing is not None and existing.content_hash == digest:
            if not target.exists():
                self._atomic_write_bytes(target, payload)
            return existing
        if target.exists() and (existing is None or existing.content_hash != digest):
            stem = target.stem
            suffix = target.suffix
            target = target.with_name(f"{stem}-{digest[:12]}{suffix}")
            relative = Path("artifacts") / target.name

        self._atomic_write_bytes(target, payload)
        artifact = TaskArtifact(
            task_id=task.id,
            artifact_type=artifact_type.strip(),
            relative_path=relative.as_posix(),
            content_hash=digest,
            created_at=_utcnow(),
        )
        self.db.add(artifact)
        task.updated_at = _utcnow()
        self.db.commit()
        self.db.refresh(artifact)
        self._write_task_json(task)
        return artifact

    add_artifact = record_artifact

    def complete_task(
        self,
        task_id: str,
        final_summary: dict[str, Any] | None = None,
        *,
        memory_candidates: Iterable[dict[str, Any]] | None = None,
        status: str = "completed",
    ) -> TaskSession:
        """结束任务并生成摘要；记忆候选永远以 candidate 状态输出。

        此方法不会创建或更新 ``MemoryRecord``，避免未经用户确认的内容进入
        active 长期记忆。调用方后续应将 ``final_summary.json`` 中的候选交给
        明确的审核/晋升服务。
        """

        task = self.get_task(task_id)
        if status not in _TERMINAL_TASK_STATUSES:
            raise ValueError(f"不支持的结束状态: {status}")
        now = _utcnow()
        candidates = [self._candidate_copy(item) for item in (memory_candidates or [])]
        summary: dict[str, Any] = dict(final_summary or {})
        summary.update(
            {
                "task_id": task.id,
                "blogger_id": task.blogger_id,
                "task_type": task.task_type,
                "title": task.title,
                "status": status,
                "completed_at": now.isoformat(),
                "memory_candidates": candidates,
                "memory_candidates_auto_activated": False,
            }
        )
        task.status = status
        task.completed_at = now
        task.updated_at = now
        self.db.commit()
        self._atomic_write_json(self._task_dir(task.id) / "final_summary.json", summary)
        self._write_task_json(task)
        return task

    finish_task = complete_task
    end_task = complete_task

    def fail_task(self, task_id: str, error: str, *, error_code: str = "TASK_FAILED") -> TaskSession:
        task = self.get_task(task_id)
        summary = {"error_code": error_code, "error": error}
        return self.complete_task(task.id, summary, status="failed")

    # ------------------------------------------------------------------
    # 文件同步和读取
    # ------------------------------------------------------------------
    def sync_task_files(self, task_id: str) -> None:
        """用数据库记录重建任务文件，供重启恢复和文件损坏修复使用。"""

        task = self.get_task(task_id)
        task_dir = self._task_dir(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_standard_files(task_dir)
        self._write_task_json(task)
        self._write_context(task, task.current_context)

        messages = self.db.scalars(
            select(SessionMessage)
            .where(SessionMessage.task_id == task.id)
            .order_by(SessionMessage.sequence, SessionMessage.id)
        )
        self._atomic_write_jsonl(
            task_dir / "messages.jsonl",
            (
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "role": row.role,
                    "content": row.content,
                    "created_at": row.created_at,
                }
                for row in messages
            ),
        )

        checkpoints = self.db.scalars(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.task_id == task.id)
            .order_by(TaskCheckpoint.sequence, TaskCheckpoint.id)
        )
        self._atomic_write_json(
            task_dir / "checkpoints.json",
            [
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "state": _json_value(row.state_json),
                    "state_json": row.state_json,
                    "context_snapshot": row.context_snapshot,
                    "created_at": row.created_at,
                }
                for row in checkpoints
            ],
        )
        self._rewrite_decisions(task.id)

    def read_task_context(self, task_id: str) -> str:
        task = self.get_task(task_id)
        path = self._task_dir(task.id) / "context.md"
        if not path.exists():
            self._write_context(task, task.current_context)
        return path.read_text(encoding="utf-8")

    def task_directory(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self._task_dir(task_id)

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------
    def _next_sequence(self, model: Any, field: Any, task_id: str) -> int:
        maximum = self.db.scalar(select(func.max(model.sequence)).where(field == task_id))
        return int(maximum or 0) + 1

    @staticmethod
    def _next_file_sequence(path: Path) -> int:
        if not path.exists():
            return 1
        sequence = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    sequence = max(sequence, int(json.loads(line).get("sequence", 0)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        return sequence + 1

    def _task_dir(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        task_dir = (self.tasks_root / task_id).resolve()
        try:
            task_dir.relative_to(self.tasks_root)
        except ValueError as exc:
            raise TaskMemoryError("任务目录越界") from exc
        return task_dir

    def _ensure_task_directory(self, task: TaskSession) -> None:
        expected = self._task_dir(task.id)
        if Path(task.task_dir).resolve() != expected:
            task.task_dir = str(expected)
            self.db.commit()
        expected.mkdir(parents=True, exist_ok=True)
        self._ensure_standard_files(expected)

    @staticmethod
    def _ensure_standard_files(task_dir: Path) -> None:
        (task_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        for filename, default in (
            ("messages.jsonl", ""),
            ("decisions.jsonl", ""),
            ("context.md", ""),
            ("checkpoints.json", "[]\n"),
            ("final_summary.json", "{}\n"),
        ):
            path = task_dir / filename
            if not path.exists():
                path.write_text(default, encoding="utf-8")

    def _write_task_json(self, task: TaskSession) -> None:
        payload = {
            "task_id": task.id,
            "blogger_id": task.blogger_id,
            "task_type": task.task_type,
            "title": task.title,
            "status": task.status,
            "current_context": task.current_context,
            "recovery_state": _json_value(task.recovery_state_json),
            "started_at": task.started_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
        }
        self._atomic_write_json(self._task_dir(task.id) / "task.json", payload)

    def _write_context(self, task: TaskSession, context: str) -> None:
        self._atomic_write_text(self._task_dir(task.id) / "context.md", context)

    def _rewrite_checkpoints(self, task_id: str) -> None:
        task = self.get_task(task_id)
        rows = self.db.scalars(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.task_id == task.id)
            .order_by(TaskCheckpoint.sequence, TaskCheckpoint.id)
        )
        self._atomic_write_json(
            self._task_dir(task.id) / "checkpoints.json",
            [
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "state": _json_value(row.state_json),
                    "state_json": row.state_json,
                    "context_snapshot": row.context_snapshot,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        )

    def _rewrite_decisions(self, task_id: str) -> None:
        """从 DecisionLog 中同步当前任务的决策 JSONL 副本。"""

        task = self.get_task(task_id)
        rows = self.db.scalars(
            select(DecisionLog)
            .where(
                DecisionLog.blogger_id == task.blogger_id,
                DecisionLog.decision_type.like("task:%"),
            )
            .order_by(DecisionLog.id)
        )
        values: list[dict[str, Any]] = []
        for row in rows:
            try:
                envelope = json.loads(row.input_summary)
            except (TypeError, json.JSONDecodeError):
                continue
            if envelope.get("task_id") != task.id:
                continue
            decision_type = row.decision_type.removeprefix("task:")
            values.append(
                {
                    "id": row.id,
                    "sequence": int(envelope.get("sequence", len(values) + 1)),
                    "decision_type": decision_type,
                    "prompt_version": row.prompt_version,
                    "input_summary": envelope.get("input", ""),
                    "decision": _json_value(row.decision),
                    "reason": row.reason,
                    "created_at": row.created_at,
                }
            )
        values.sort(key=lambda value: (value["sequence"], value["id"]))
        self._atomic_write_jsonl(self._task_dir(task.id) / "decisions.jsonl", values)

    def _append_jsonl_if_missing(self, path: Path, payload: dict[str, Any], *, identity_key: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = payload.get(identity_key)
        existing_values: set[Any] = set()
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        existing_values.add(json.loads(line).get(identity_key))
                    except json.JSONDecodeError:
                        # 损坏行不会阻止后续记录追加；sync_task_files 可完整重建。
                        continue
        if identity in existing_values:
            return
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        TaskMemoryService._atomic_write_bytes(path, text.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _atomic_write_json(path: Path, value: Any) -> None:
        TaskMemoryService._atomic_write_text(
            path, json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n"
        )

    @staticmethod
    def _atomic_write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
        text = "".join(json.dumps(value, ensure_ascii=False, default=_json_default) + "\n" for value in values)
        TaskMemoryService._atomic_write_text(path, text)

    @staticmethod
    def _safe_file_name(filename: str) -> str:
        name = Path(filename).name.strip()
        name = _SAFE_FILE_NAME.sub("-", name)
        if not name or name in {".", ".."}:
            name = f"artifact-{uuid.uuid4().hex}.bin"
        return name[:180]

    @staticmethod
    def _safe_child(root: Path, relative: Path) -> Path:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise TaskMemoryError("产物路径越界") from exc
        return candidate

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("非法任务 ID")

    @staticmethod
    def _candidate_copy(candidate: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ValueError("长期记忆候选必须是对象")
        copied = dict(candidate)
        copied["status"] = "candidate"
        copied["active"] = False
        copied["auto_activated"] = False
        return copied


class _TaskDecisionRow:
    """文件级任务决策记录，不写入长期 ``DecisionLog``。"""

    def __init__(
        self,
        *,
        task_id: str,
        sequence: int,
        decision_type: str,
        prompt_version: str,
        input_summary: str,
        decision: str,
        reason: str,
        created_at: datetime,
    ) -> None:
        self.task_id = task_id
        self.sequence = sequence
        self.decision_type = decision_type
        self.prompt_version = prompt_version
        self.input_summary = input_summary
        self.decision = decision
        self.reason = reason
        self.created_at = created_at
        self.database_id: int | None = None

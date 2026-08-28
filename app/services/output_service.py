"""第三阶段脚本与分镜输出编排服务。"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Assessment,
    Blogger,
    DecisionLog,
    MemoryRecord,
    Output,
    OutputAsset,
    OutputPlace,
    Place,
    TaskSession,
)
from app.services.context_service import ContextService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.output_agent import DeepSeekOutputAgent, OutputAgent, OutputAgentError
from app.services.output_validation_service import OutputValidationError, OutputValidationService
from app.services.task_memory_service import TaskMemoryService


class OutputServiceError(RuntimeError):
    """携带稳定错误码的内容输出异常。"""

    def __init__(self, code: str, *, status_code: int = 422, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail or code


class OutputService:
    """编排冻结快照、Agent、证据、任务记忆和版本化输出。"""

    prompt_version = "phase3-output-v1"

    def __init__(
        self,
        db: Session,
        *,
        agent: OutputAgent | None = None,
        validation_service: OutputValidationService | None = None,
        task_service: TaskMemoryService | None = None,
        context_service: ContextService | None = None,
        memory_service: MemoryService | None = None,
        analysis_service: LibraryAnalysisService | None = None,
    ) -> None:
        self.db = db
        self.agent = agent or DeepSeekOutputAgent()
        self.validation_service = validation_service or OutputValidationService()
        self.task_service = task_service or TaskMemoryService(db)
        self.memory_service = memory_service or MemoryService(db)
        self.analysis_service = analysis_service or LibraryAnalysisService(db)
        self.context_service = context_service or ContextService(
            db,
            memory_service=self.memory_service,
            system_rules=(
                "你是黔衣有话内容产出Agent。只能依据当前博主的冻结快照、短期任务记忆和"
                "active长期记忆；不得编造地点、商业数据、平台数据或引用快照外资产。"
            ),
        )

    def start_generation(
        self,
        blogger_id: int,
        output_type: str,
        assessment_id: int,
        idempotency_key: str,
        *,
        user_instruction: str = "",
        parent_output_id: int | None = None,
        category: str | None = None,
    ) -> Output:
        """幂等创建一个待执行脚本或分镜任务。"""

        self._active_blogger(blogger_id)
        kind = str(output_type).strip()
        if kind not in {"script", "storyboard"}:
            raise OutputServiceError("OUTPUT_INVALID_JSON")
        assessment = self._ready_assessment(blogger_id, assessment_id, kind)
        key = str(idempotency_key).strip()
        if not key:
            raise OutputServiceError("OUTPUT_INVALID_JSON")
        existing = self.db.scalar(
            select(Output).where(Output.blogger_id == blogger_id, Output.idempotency_key == key)
        )
        if existing is not None:
            return existing

        parent: Output | None = None
        if kind == "storyboard":
            if parent_output_id is None:
                raise OutputServiceError("STORYBOARD_SCRIPT_REQUIRED")
            parent = self._output_or_error(blogger_id, parent_output_id)
            if parent.type != "script" or parent.status != "succeeded":
                raise OutputServiceError("STORYBOARD_SCRIPT_REQUIRED")

        task_id = f"output-{blogger_id}-{sha256(key.encode('utf-8')).hexdigest()[:16]}"
        task = self.task_service.create_task(
            blogger_id,
            f"output_{kind}",
            "生成脚本" if kind == "script" else "生成分镜",
            task_id=task_id,
            initial_context=user_instruction.strip() or "按当前体检与可信资产生成内容",
            metadata={
                "phase": "pending",
                "assessment_id": assessment.id,
                "output_type": kind,
                "parent_output_id": parent_output_id,
                "idempotency_key": key,
            },
        )
        output = Output(
            blogger_id=blogger_id,
            task_id=task.id,
            idempotency_key=key,
            type=kind,
            category=(category or "待生成").strip() or "待生成",
            title="待生成脚本" if kind == "script" else "待生成分镜",
            content_json=self._json({"user_instruction": user_instruction.strip()}),
            status="pending",
            assessment_id=assessment.id,
            parent_output_id=parent.id if parent is not None else None,
            version=1,
            manual_locked=False,
            prompt_version=self.prompt_version,
            model_name=getattr(self.agent, "model_name", "unknown"),
        )
        self.db.add(output)
        try:
            self.db.commit()
            self.db.refresh(output)
            return output
        except SQLAlchemyError as exc:
            self.db.rollback()
            same = self.db.scalar(
                select(Output).where(Output.blogger_id == blogger_id, Output.idempotency_key == key)
            )
            if same is not None:
                return same
            raise OutputServiceError("OUTPUT_PERSIST_FAILED", status_code=500) from exc

    def execute_generation(self, output_id: int, blogger_id: int | None = None) -> Output:
        """执行待生成输出；任何失败都不会留下引用或成功决策半成品。"""

        output = self._output_or_error(blogger_id, output_id, allow_deleted=False)
        if output.status == "succeeded":
            return output
        if output.status == "running":
            raise OutputServiceError("OUTPUT_ALREADY_RUNNING", status_code=409)
        if output.status == "failed":
            raise OutputServiceError("OUTPUT_NOT_FOUND", status_code=409, detail="请使用retry接口")
        if output.task_id is None:
            raise OutputServiceError("OUTPUT_PERSIST_FAILED", status_code=500)

        task_id = output.task_id
        try:
            output.status = "running"
            output.error_code = None
            output.error_message = None
            self.db.commit()
            self.task_service.append_message(task_id, "assistant", f"开始生成{output.type}")
            snapshot = self.build_snapshot(output.blogger_id, output.assessment_id)
            snapshot_hash = self.calculate_snapshot_hash(snapshot)
            self.task_service.create_checkpoint(
                task_id,
                {"phase": "snapshot", "snapshot_hash": snapshot_hash, "output_id": output.id},
                context_snapshot="内容输入快照已冻结，等待 Agent 生成",
            )
            initial = self._decode(output.content_json)
            instruction = str(initial.get("user_instruction") or "")
            context = self.context_service.assemble_context(
                output.blogger_id,
                self._json(
                    {
                        "用户指令": instruction,
                        "输出类型": output.type,
                        "体检": snapshot["assessment"],
                        "可信资产数量": len(snapshot["assets"]),
                        "地点数量": len(snapshot["places"]),
                    }
                ),
                task_id=task_id,
            )
            script: dict[str, Any] | None = None
            if output.type == "script":
                raw = self.agent.generate_script(context.as_messages(), snapshot, instruction)
            else:
                parent = self._output_or_error(output.blogger_id, output.parent_output_id or 0)
                script = {**self._decode(parent.content_json), "id": parent.id, "version": parent.version}
                raw = self.agent.generate_storyboard(context.as_messages(), snapshot, script)
            normalized = self.validation_service.validate_and_normalize(
                raw,
                output.type,
                snapshot,
                script=script,
            )
            self._validate_place_refs(normalized, snapshot)
            current_hash = self.calculate_snapshot_hash(
                self.build_snapshot(output.blogger_id, output.assessment_id)
            )
            if current_hash != snapshot_hash:
                raise OutputServiceError("OUTPUT_SNAPSHOT_CHANGED", status_code=409)

            self.task_service.create_checkpoint(
                task_id,
                {"phase": "validated", "snapshot_hash": snapshot_hash, "output_id": output.id},
                context_snapshot="Agent 输出已通过结构、事实、归属和快照校验",
            )
            self.task_service.complete_task(
                task_id,
                {
                    "output_id": output.id,
                    "output_type": output.type,
                    "snapshot_hash": snapshot_hash,
                    "title": normalized.get("title") or output.title,
                },
                memory_candidates=[
                    {
                        "memory_type": "decision_summary",
                        "title": f"内容产出摘要：{normalized.get('title') or output.title}",
                        "content": f"已生成{output.type}，输出ID为{output.id}，仅作为待确认候选。",
                        "source_type": "output",
                        "source_id": str(output.id),
                        "confidence": 0.7,
                    }
                ],
            )
            self._persist_success(output, normalized, snapshot_hash)
            return self.get_output(output.blogger_id, output.id)
        except Exception as exc:
            code = self._error_code(exc)
            self._persist_failure(output.id, code, str(exc))
            if isinstance(exc, OutputServiceError):
                raise
            raise OutputServiceError(code, status_code=500 if code == "OUTPUT_PERSIST_FAILED" else 422) from exc

    def retry_generation(self, blogger_id: int, output_id: int) -> Output:
        output = self._output_or_error(blogger_id, output_id)
        if output.status == "running":
            raise OutputServiceError("OUTPUT_ALREADY_RUNNING", status_code=409)
        if output.status == "succeeded":
            return output
        if output.task_id is None:
            raise OutputServiceError("OUTPUT_PERSIST_FAILED", status_code=500)
        task = self.db.get(TaskSession, output.task_id)
        if task is None:
            raise OutputServiceError("OUTPUT_PERSIST_FAILED", status_code=500)
        task.status = "running"
        task.completed_at = None
        task.current_context = "输出生成失败后重试，保留消息和检查点"
        output.status = "pending"
        output.error_code = None
        output.error_message = None
        self.db.commit()
        self.task_service.sync_task_files(task.id)
        return self.execute_generation(output.id, blogger_id)

    def get_output(self, blogger_id: int, output_id: int) -> Output:
        self._active_blogger(blogger_id)
        return self._output_or_error(blogger_id, output_id, with_children=True)

    def list_outputs(
        self,
        blogger_id: int,
        *,
        output_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Output]:
        self._active_blogger(blogger_id)
        if limit < 1 or limit > 1000 or offset < 0:
            raise OutputServiceError("OUTPUT_INVALID_JSON")
        statement = select(Output).where(
            Output.blogger_id == blogger_id,
            Output.deleted_at.is_(None),
        )
        if output_type:
            statement = statement.where(Output.type == output_type)
        if status:
            statement = statement.where(Output.status == status)
        return list(
            self.db.scalars(
                statement.order_by(Output.created_at.desc(), Output.id.desc()).offset(offset).limit(limit)
            )
        )

    def revise_output(
        self,
        blogger_id: int,
        output_id: int,
        content: Mapping[str, Any],
        *,
        title: str | None = None,
        category: str | None = None,
    ) -> Output:
        """人工编辑生成新版本；旧版本及其排期引用保持不变。"""

        source = self.get_output(blogger_id, output_id)
        if source.status != "succeeded":
            raise OutputServiceError("OUTPUT_NOT_FOUND")
        snapshot = self.build_snapshot(blogger_id, source.assessment_id)
        script: dict[str, Any] | None = None
        if source.type == "storyboard":
            script_id = self._decode(source.content_json).get("script_id")
            parent = self._output_or_error(blogger_id, int(script_id or source.parent_output_id or 0))
            script = {**self._decode(parent.content_json), "id": parent.id, "version": parent.version}
        normalized = self.validation_service.validate_and_normalize(
            dict(content), source.type, snapshot, script=script
        )
        self._validate_place_refs(normalized, snapshot)
        root_id = source.parent_output_id if source.parent_output_id and source.type == "script" else source.id
        latest = self.db.scalar(
            select(func.max(Output.version)).where(
                Output.blogger_id == blogger_id,
                (Output.id == root_id) | (Output.parent_output_id == root_id),
            )
        )
        version = int(latest or source.version) + 1
        decision = DecisionLog(
            blogger_id=blogger_id,
            decision_type="output_revision",
            prompt_version=self.prompt_version,
            input_summary=self._json({"source_output_id": source.id, "version": source.version}),
            decision=self._json({"new_version": version, "title": title or normalized.get("title")}),
            reason="用户手工编辑输出，创建不可变新版本并保留旧版引用",
        )
        self.db.add(decision)
        self.db.flush()
        revised = Output(
            blogger_id=blogger_id,
            task_id=None,
            idempotency_key=f"revision-{root_id}-{version}",
            type=source.type,
            category=(category or normalized.get("category") or source.category).strip(),
            title=(title or normalized.get("title") or source.title).strip(),
            content_json=self._json(normalized),
            status="succeeded",
            assessment_id=source.assessment_id,
            parent_output_id=root_id,
            version=version,
            manual_locked=True,
            decision_id=decision.id,
            prompt_version=self.prompt_version,
            model_name="manual",
        )
        self.db.add(revised)
        try:
            self.db.flush()
            self._persist_references(revised, normalized)
            self.db.commit()
            return self.get_output(blogger_id, revised.id)
        except Exception:
            self.db.rollback()
            raise

    def soft_delete_output(self, blogger_id: int, output_id: int) -> Output:
        output = self.get_output(blogger_id, output_id)
        if output.deleted_at is not None:
            return output
        output.deleted_at = datetime.utcnow()
        output.status = "deleted"
        self.db.add(
            DecisionLog(
                blogger_id=blogger_id,
                decision_type="output_soft_delete",
                prompt_version=self.prompt_version,
                input_summary=self._json({"output_id": output.id}),
                decision=self._json({"deleted": True}),
                reason="用户软删除输出；历史引用与数据库记录继续保留",
            )
        )
        self.db.commit()
        return output

    def recover_unfinished_outputs(self, blogger_id: int | None = None) -> list[Output]:
        statement = select(Output).where(Output.status.in_(("pending", "running")))
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(Output.blogger_id == blogger_id)
        rows = list(self.db.scalars(statement.order_by(Output.id)))
        for row in rows:
            row.status = "failed"
            row.error_code = "OUTPUT_PERSIST_FAILED"
            row.error_message = "服务重启导致输出任务中断，可安全重试"
            self.db.commit()
            if row.task_id:
                try:
                    self.task_service.recover_task(row.task_id)
                    self.task_service.fail_task(row.task_id, row.error_message, error_code=row.error_code)
                except Exception:
                    self.db.rollback()
        return rows

    def build_snapshot(self, blogger_id: int, assessment_id: int | None) -> dict[str, Any]:
        assessment = self._ready_assessment(blogger_id, assessment_id, "script")
        snapshot = self.analysis_service.build_snapshot(blogger_id)
        places = list(
            self.db.scalars(
                select(Place)
                .where(Place.blogger_id == blogger_id, Place.deleted_at.is_(None))
                .order_by(Place.id)
            )
        )
        active_memories = list(
            self.db.scalars(
                select(MemoryRecord)
                .where(MemoryRecord.blogger_id == blogger_id, MemoryRecord.status == "active")
                .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id.desc())
                .limit(20)
            )
        )
        snapshot.update(
            {
                "assessment": {
                    "id": assessment.id,
                    "snapshot_hash": assessment.snapshot_hash,
                    "overall_score": assessment.overall_score,
                    "summary": assessment.summary,
                    "feature_readiness": self._decode(assessment.feature_readiness_json),
                    "library_analysis": self._decode(assessment.library_analysis_json),
                },
                "places": [self._place_payload(row) for row in places],
                "active_memories": [
                    {
                        "id": row.id,
                        "memory_type": row.memory_type,
                        "title": row.title,
                        "content": row.content,
                        "source_type": row.source_type,
                        "source_id": row.source_id,
                        "confidence": row.confidence,
                        "version": row.version,
                    }
                    for row in active_memories
                ],
            }
        )
        snapshot["output_snapshot_hash"] = self.calculate_snapshot_hash(snapshot)
        return snapshot

    @classmethod
    def calculate_snapshot_hash(cls, snapshot: Mapping[str, Any]) -> str:
        payload = dict(snapshot)
        payload.pop("output_snapshot_hash", None)
        payload.pop("snapshot_hash", None)
        return sha256(cls._json(payload).encode("utf-8")).hexdigest()

    def _persist_success(self, output: Output, normalized: dict[str, Any], snapshot_hash: str) -> None:
        try:
            output.content_json = self._json({**normalized, "snapshot_hash": snapshot_hash})
            output.category = str(normalized.get("category") or output.category)
            output.title = str(normalized.get("title") or output.title)
            output.status = "succeeded"
            output.error_code = None
            output.error_message = None
            self._persist_references(output, normalized)
            decision = DecisionLog(
                blogger_id=output.blogger_id,
                decision_type="output_generation",
                prompt_version=self.prompt_version,
                input_summary=self._json(
                    {"output_id": output.id, "assessment_id": output.assessment_id, "snapshot_hash": snapshot_hash}
                ),
                decision=self._json(
                    {
                        "output_type": output.type,
                        "title": output.title,
                        "asset_refs": [row.asset_id for row in output.assets],
                        "place_refs": [row.place_id for row in output.places],
                    }
                ),
                reason="基于体检、可信资产、地点和受控记忆生成，并经后端验证",
            )
            self.db.add(decision)
            self.db.flush()
            output.decision_id = decision.id
            self.db.commit()
            self.memory_service.create_memory(
                output.blogger_id,
                "decision_summary",
                f"内容产出摘要：{output.title}",
                f"输出ID：{output.id}；类型：{output.type}；标题：{output.title}",
                "decision_log",
                decision.id,
                confidence=0.7,
                status="candidate",
                user_confirmed=False,
            )
        except Exception:
            self.db.rollback()
            raise

    def _persist_references(self, output: Output, normalized: Mapping[str, Any]) -> None:
        seen_assets: set[tuple[int, str]] = set()
        for ref in normalized.get("source_refs", []):
            if not isinstance(ref, Mapping) or ref.get("asset_id") is None:
                continue
            asset_id = int(ref["asset_id"])
            usage = str(ref.get("usage_type") or ref.get("evidence_type") or "fact")[:50]
            asset_key = (asset_id, usage)
            if asset_key in seen_assets:
                continue
            seen_assets.add(asset_key)
            self.db.add(
                OutputAsset(
                    output_id=output.id,
                    asset_id=asset_id,
                    usage_type=usage,
                    claim=str(ref.get("claim") or "输出事实引用"),
                )
            )
        place_refs: list[Mapping[str, Any]] = []
        for section_name in ("place_refs", "stops"):
            values = normalized.get(section_name, [])
            if isinstance(values, list):
                place_refs.extend(item for item in values if isinstance(item, Mapping))
        seen_places: set[tuple[int, str, int]] = set()
        for index, ref in enumerate(place_refs, start=1):
            if ref.get("place_id") is None:
                continue
            place_id = int(ref["place_id"])
            role = str(ref.get("role") or "featured")[:50]
            sequence = int(ref.get("sequence") or index)
            place_key = (place_id, role, sequence)
            if place_key in seen_places:
                continue
            seen_places.add(place_key)
            self.db.add(
                OutputPlace(
                    output_id=output.id,
                    place_id=place_id,
                    role=role,
                    sequence=sequence,
                    claim=str(ref.get("claim") or ref.get("reason") or "输出地点引用"),
                )
            )
        self.db.flush()

    def _validate_place_refs(self, normalized: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        allowed = {int(item["id"]) for item in snapshot.get("places", []) if isinstance(item, Mapping)}
        for key in ("place_refs", "stops"):
            values = normalized.get(key, [])
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, Mapping) or item.get("place_id") is None:
                    raise OutputServiceError("OUTPUT_EVIDENCE_INVALID")
                if int(item["place_id"]) not in allowed:
                    raise OutputServiceError("OUTPUT_EVIDENCE_INVALID")

    def _persist_failure(self, output_id: int, code: str, message: str) -> None:
        self.db.rollback()
        output = self.db.get(Output, output_id)
        if output is None:
            return
        self.db.query(OutputAsset).filter(OutputAsset.output_id == output.id).delete()
        self.db.query(OutputPlace).filter(OutputPlace.output_id == output.id).delete()
        output.status = "failed"
        output.error_code = code
        output.error_message = self._safe_error(message)
        output.decision_id = None
        self.db.commit()
        if output.task_id:
            try:
                self.task_service.append_message(output.task_id, "assistant", f"输出生成失败：{code}")
                self.task_service.create_checkpoint(
                    output.task_id,
                    {"phase": "failed", "error_code": code, "output_id": output.id},
                    context_snapshot=f"输出失败，可从错误码 {code} 重试",
                )
                self.task_service.fail_task(output.task_id, code, error_code=code)
            except Exception:
                self.db.rollback()

    def _ready_assessment(self, blogger_id: int, assessment_id: int | None, kind: str) -> Assessment:
        self._active_blogger(blogger_id)
        if assessment_id is None:
            raise OutputServiceError("ASSESSMENT_NOT_READY")
        row = self.db.scalar(
            select(Assessment).where(
                Assessment.id == assessment_id,
                Assessment.blogger_id == blogger_id,
                Assessment.status == "succeeded",
            )
        )
        if row is None:
            raise OutputServiceError("ASSESSMENT_NOT_READY")
        readiness = self._decode(row.feature_readiness_json)
        readiness_key = "route_recommendation" if kind == "route_rec" else "script_generation"
        state = readiness.get(readiness_key)
        if isinstance(state, Mapping) and state.get("ready") is False:
            raise OutputServiceError("ASSESSMENT_NOT_READY", detail=self._json(state.get("missing_items", [])))
        return row

    def _output_or_error(
        self,
        blogger_id: int | None,
        output_id: int,
        *,
        allow_deleted: bool = False,
        with_children: bool = False,
    ) -> Output:
        statement = select(Output).where(Output.id == output_id)
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(Output.blogger_id == blogger_id)
        if not allow_deleted:
            statement = statement.where(Output.deleted_at.is_(None))
        if with_children:
            statement = statement.options(selectinload(Output.assets), selectinload(Output.places))
        row = self.db.scalar(statement)
        if row is None:
            raise OutputServiceError("OUTPUT_NOT_FOUND", status_code=404)
        return row

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(
            select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None))
        )
        if row is None:
            raise OutputServiceError("BLOGGER_NOT_FOUND", status_code=404)
        return row

    @staticmethod
    def _place_payload(row: Place) -> dict[str, Any]:
        return {
            "id": row.id,
            "blogger_id": row.blogger_id,
            "name": row.name,
            "category": row.category,
            "location": row.location,
            "specialty": row.specialty,
            "tags": OutputService._decode(row.tags_json).get("items", [])
            if isinstance(OutputService._decode(row.tags_json), dict)
            else json.loads(row.tags_json or "[]"),
            "source_type": row.source_type,
            "source_url": row.source_url,
            "credibility": row.credibility,
            "origin": row.origin,
            "like_level": row.like_level,
            "est_cost": row.est_cost,
            "est_benefit": row.est_benefit,
            "fits_koc": row.fits_koc,
            "fits_shoot": row.fits_shoot,
        }

    @staticmethod
    def _decode(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, OutputServiceError):
            return exc.code
        if isinstance(exc, (OutputAgentError, OutputValidationError)):
            return str(getattr(exc, "code", "OUTPUT_INVALID_JSON"))
        value = str(exc)
        for code in (
            "OUTPUT_INVALID_JSON",
            "OUTPUT_EVIDENCE_INVALID",
            "OUTPUT_SNAPSHOT_CHANGED",
            "STORYBOARD_SCRIPT_REQUIRED",
            "ASSESSMENT_NOT_READY",
        ):
            if code in value:
                return code
        return "OUTPUT_PERSIST_FAILED"

    @staticmethod
    def _safe_error(message: str) -> str:
        if not message:
            return "输出执行失败"
        upper = message.upper()
        if "KEY" in upper or "AUTHORIZATION" in upper or "TRACEBACK" in upper:
            return "输出依赖调用失败"
        return message[:500]

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = ["OutputService", "OutputServiceError"]

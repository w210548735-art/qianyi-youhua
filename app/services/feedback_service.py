"""第四阶段反馈分析、候选确认/拒绝与闭环写回编排。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Asset,
    AssetEffectRevision,
    AssetEmbedding,
    Blogger,
    DecisionLog,
    FeedbackEvidence,
    FeedbackRun,
    LibraryEvolutionRevision,
    MemoryRecord,
    Place,
    PlaceCommercialRevision,
    ProfileFeedbackRevision,
    TaskSession,
)
from app.services.context_service import ContextService
from app.services.embedding_service import EmbeddingService
from app.services.feedback_agent import DeepSeekFeedbackAgent, FeedbackAgent, FeedbackAgentError
from app.services.feedback_analysis_service import FeedbackAnalysisError, FeedbackAnalysisService
from app.services.feedback_validation_service import (
    FeedbackValidationError,
    FeedbackValidationService,
)
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService


class FeedbackServiceError(RuntimeError):
    """反馈服务稳定错误。"""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 422,
        detail: Any | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail if detail is not None else code
        super().__init__(code)


class FeedbackService:
    """保证“分析只产候选、明确确认才写回”的反馈闭环。"""

    prompt_version = "phase4-feedback-v1"
    COMMERCIAL_FIELDS = {"est_cost", "est_benefit", "like_level", "fits_koc", "fits_shoot"}

    def __init__(
        self,
        db: Session,
        *,
        analysis_service: FeedbackAnalysisService | None = None,
        agent: FeedbackAgent | None = None,
        validation_service: FeedbackValidationService | None = None,
        task_service: TaskMemoryService | None = None,
        memory_service: MemoryService | None = None,
        context_service: ContextService | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.db = db
        self.analysis_service = analysis_service or FeedbackAnalysisService(db)
        self.agent = agent or DeepSeekFeedbackAgent()
        self.validation_service = validation_service or FeedbackValidationService()
        self.task_service = task_service or TaskMemoryService(db)
        self.embedding_service = embedding_service or EmbeddingService()
        self.memory_service = memory_service or MemoryService(db, embedding=self.embedding_service)
        self.context_service = context_service or ContextService(
            db,
            memory_service=self.memory_service,
            system_rules=(
                "你是黔衣有话反馈分析Agent。只能基于当前博主冻结快照提出候选；"
                "禁止自动写回、编造数字、跨博主引用或把模拟反馈当真实经营结论。"
            ),
        )

    def start(
        self,
        blogger_id: int,
        output_id: int,
        primary_metric_id: int,
        idempotency_key: str,
        *,
        user_instruction: str = "",
    ) -> FeedbackRun:
        self._active_blogger(blogger_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise FeedbackServiceError("FEEDBACK_IDEMPOTENCY_REQUIRED")
        existing = self.db.scalar(
            select(FeedbackRun).where(
                FeedbackRun.blogger_id == blogger_id,
                FeedbackRun.idempotency_key == key,
            )
        )
        if existing is not None:
            return existing
        # build_snapshot 是创建运行前的完整归属和链路校验，不会调用 Agent。
        snapshot = self.analysis_service.build_snapshot(
            blogger_id,
            output_id,
            primary_metric_id,
            user_instruction=user_instruction,
        )
        task_id = f"feedback-{blogger_id}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        task = self.task_service.create_task(
            blogger_id,
            "feedback_analysis",
            "分析内容反馈",
            task_id=task_id,
            initial_context=user_instruction.strip() or "基于已录入指标分析反馈候选",
            metadata={
                "phase": "pending",
                "output_id": output_id,
                "primary_metric_id": primary_metric_id,
                "idempotency_key": key,
            },
        )
        run = FeedbackRun(
            blogger_id=blogger_id,
            output_id=output_id,
            primary_metric_id=primary_metric_id,
            task_id=task.id,
            status="pending",
            idempotency_key=key,
            snapshot_json=self._json(snapshot),
            snapshot_hash=str(snapshot["snapshot_hash"]),
            prompt_version=getattr(self.agent, "prompt_version", self.prompt_version),
            model_name=getattr(self.agent, "model_name", "unknown"),
        )
        self.db.add(run)
        try:
            self.db.commit()
            self.db.refresh(run)
            return run
        except SQLAlchemyError as exc:
            self.db.rollback()
            duplicate = self.db.scalar(
                select(FeedbackRun).where(
                    FeedbackRun.blogger_id == blogger_id,
                    FeedbackRun.idempotency_key == key,
                )
            )
            if duplicate is not None:
                return duplicate
            raise FeedbackServiceError("FEEDBACK_PERSIST_FAILED", status_code=500) from exc

    start_analysis = start

    def analyze(self, blogger_id: int, run_id: int) -> FeedbackRun:
        run = self._run_or_error(blogger_id, run_id)
        if run.status in {"analyzed", "applied", "rejected"}:
            return run
        if run.status == "running":
            raise FeedbackServiceError("FEEDBACK_ALREADY_RUNNING", status_code=409)
        if run.status == "failed":
            raise FeedbackServiceError("FEEDBACK_RETRY_REQUIRED", status_code=409)
        stable_run_id = run.id
        run.status = "running"
        run.error_code = None
        run.error_message = None
        self.db.commit()
        snapshot = self._decode(run.snapshot_json, {})
        instruction = str(snapshot.get("user_instruction") or "")
        try:
            if run.task_id:
                self.task_service.append_message(run.task_id, "assistant", "开始反馈确定性预分析")
            current = self.analysis_service.build_snapshot(
                run.blogger_id,
                run.output_id,
                run.primary_metric_id,
                task_id=run.task_id,
                user_instruction=instruction,
            )
            if str(current["snapshot_hash"]) != run.snapshot_hash:
                raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
            if run.task_id:
                self.task_service.create_checkpoint(
                    run.task_id,
                    {"phase": "snapshot", "snapshot_hash": run.snapshot_hash, "run_id": run.id},
                    context_snapshot="反馈输入快照已冻结",
                )
            context = self.context_service.assemble_context(
                run.blogger_id,
                self._json(
                    {
                        "output_id": run.output_id,
                        "metric_id": run.primary_metric_id,
                        "deterministic_analysis": current.get("deterministic_analysis"),
                        "用户指令": instruction,
                    }
                ),
                task_id=run.task_id,
            )
            raw = self.agent.analyze(
                context.as_messages(),
                current,
                instruction,
                request_id=f"feedback-{run.id}",
            )
            normalized = self.validation_service.validate_and_normalize(raw, current)
            fresh = self.analysis_service.build_snapshot(
                run.blogger_id,
                run.output_id,
                run.primary_metric_id,
                task_id=run.task_id,
                user_instruction=instruction,
            )
            if str(fresh["snapshot_hash"]) != run.snapshot_hash:
                raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
            self._persist_analysis(run, current, normalized)
            if run.task_id:
                self.task_service.create_checkpoint(
                    run.task_id,
                    {"phase": "analyzed", "snapshot_hash": run.snapshot_hash, "run_id": run.id},
                    context_snapshot="反馈候选已校验并持久化，尚未应用",
                )
                self.task_service.complete_task(
                    run.task_id,
                    {
                        "feedback_run_id": run.id,
                        "status": "analyzed",
                        "snapshot_hash": run.snapshot_hash,
                        "candidate_count": len(self.get_candidates(run.blogger_id, run.id)),
                    },
                    memory_candidates=[
                        {
                            "memory_type": "decision_summary",
                            "title": f"反馈候选摘要：运行{run.id}",
                            "content": normalized.get("summary", "反馈候选待用户确认"),
                            "source_type": "feedback_run",
                            "source_id": str(run.id),
                            "confidence": 0.7,
                        }
                    ],
                )
            return self.get(blogger_id, stable_run_id)
        except Exception as exc:
            code = self._error_code(exc)
            self._persist_failure(stable_run_id, code, str(exc))
            if isinstance(exc, FeedbackServiceError):
                raise
            status = 500 if code == "FEEDBACK_PERSIST_FAILED" else 422
            raise FeedbackServiceError(code, status_code=status) from exc

    execute = analyze

    def retry(
        self,
        blogger_id: int,
        run_id: int,
        *,
        user_instruction: str | None = None,
    ) -> FeedbackRun:
        run = self._run_or_error(blogger_id, run_id)
        if run.status == "running":
            raise FeedbackServiceError("FEEDBACK_ALREADY_RUNNING", status_code=409)
        if run.status in {"analyzed", "applied", "rejected"}:
            return run
        run.status = "pending"
        run.error_code = None
        run.error_message = None
        if user_instruction is not None:
            snapshot = self._decode(run.snapshot_json, {})
            snapshot["user_instruction"] = user_instruction.strip()
            run.snapshot_json = self._json(snapshot)
        if run.task_id:
            task = self.db.get(TaskSession, run.task_id)
            if task is not None:
                task.status = "running"
                task.completed_at = None
                task.current_context = "反馈分析失败后重试，保留原消息和检查点"
        self.db.commit()
        if run.task_id:
            self.task_service.sync_task_files(run.task_id)
        return self.analyze(blogger_id, run.id)

    def recover_unfinished(self, blogger_id: int | None = None) -> list[FeedbackRun]:
        """服务启动时把中断中的运行转为可安全重试的失败态。"""

        statement = select(FeedbackRun).where(FeedbackRun.status == "running")
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(FeedbackRun.blogger_id == blogger_id)
        rows = list(self.db.scalars(statement.order_by(FeedbackRun.id)))
        for row in rows:
            row.status = "failed"
            row.error_code = "FEEDBACK_INTERRUPTED"
            row.error_message = "服务中断，可安全重试"
            row.updated_at = datetime.utcnow()
            task = self.db.get(TaskSession, row.task_id) if row.task_id else None
            if task is not None and task.status == "running":
                task.status = "failed"
                task.current_context = "FEEDBACK_INTERRUPTED：服务中断，可安全重试"
                task.recovery_state_json = self._json(
                    {"phase": "interrupted", "feedback_run_id": row.id}
                )
                task.updated_at = datetime.utcnow()
        self.db.commit()
        return rows

    def get(self, blogger_id: int, run_id: int) -> FeedbackRun:
        return self._run_or_error(blogger_id, run_id, with_children=True)

    def list_runs(
        self, blogger_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[FeedbackRun]:
        self._active_blogger(blogger_id)
        if limit < 1 or limit > 1000 or offset < 0:
            raise FeedbackServiceError("FEEDBACK_QUERY_INVALID")
        return list(
            self.db.scalars(
                select(FeedbackRun)
                .where(FeedbackRun.blogger_id == blogger_id)
                .order_by(FeedbackRun.created_at.desc(), FeedbackRun.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )

    def get_evidence(self, blogger_id: int, run_id: int) -> list[FeedbackEvidence]:
        return list(self.get(blogger_id, run_id).evidences)

    def get_candidates(self, blogger_id: int, run_id: int) -> list[dict[str, Any]]:
        run = self.get(blogger_id, run_id)
        rows: list[dict[str, Any]] = []
        for profile_revision in run.profile_revisions:
            rows.append(
                {
                    "id": f"profile:{profile_revision.id}",
                    "candidate_type": "profile",
                    "status": profile_revision.status,
                    "version": profile_revision.version,
                    "payload": {
                        "field_name": profile_revision.field_name,
                        "before": profile_revision.before,
                        "after": profile_revision.after,
                        "reason": profile_revision.reason,
                    },
                }
            )
        for asset_revision in run.asset_revisions:
            rows.append(
                {
                    "id": f"asset_effect:{asset_revision.id}",
                    "candidate_type": "asset_effect",
                    "status": asset_revision.status,
                    "version": asset_revision.version,
                    "payload": {
                        "asset_id": asset_revision.asset_id,
                        "before_effect": asset_revision.before_effect,
                        "after_effect": asset_revision.after_effect,
                        "before_weight": asset_revision.before_weight,
                        "after_weight": asset_revision.after_weight,
                        "reason": asset_revision.reason,
                    },
                }
            )
        for place_revision in run.place_revisions:
            rows.append(
                {
                    "id": f"place_commercial:{place_revision.id}",
                    "candidate_type": "place_commercial",
                    "status": place_revision.status,
                    "version": place_revision.version,
                    "payload": {
                        "place_id": place_revision.place_id,
                        "before": self._decode(place_revision.before_json, {}),
                        "after": self._decode(place_revision.after_json, {}),
                        "reason": place_revision.reason,
                    },
                }
            )
        for library_revision in run.library_revisions:
            rows.append(
                {
                    "id": f"library_evolution:{library_revision.id}",
                    "candidate_type": "library_evolution",
                    "status": library_revision.status,
                    "version": library_revision.version,
                    "payload": {
                        "lib_type": library_revision.lib_type,
                        "action": library_revision.action,
                        "target_asset_id": library_revision.target_asset_id,
                        "candidate": self._decode(library_revision.candidate_json, {}),
                        "reason": library_revision.reason,
                    },
                }
            )
        return rows

    def confirm(
        self,
        blogger_id: int,
        run_id: int,
        *,
        candidate_ids: Sequence[str] | None = None,
        place_overrides: Mapping[int | str, Mapping[str, Any]] | None = None,
    ) -> FeedbackRun:
        run = self.get(blogger_id, run_id)
        if run.status == "applied":
            return run
        if run.status != "analyzed":
            raise FeedbackServiceError("FEEDBACK_CONFIRM_INVALID_STATE", status_code=409)
        self._assert_snapshot_current(run)
        selected = set(candidate_ids or [row["id"] for row in self.get_candidates(blogger_id, run.id)])
        known = {row["id"] for row in self.get_candidates(blogger_id, run.id)}
        if not selected or not selected <= known:
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_NOT_FOUND", status_code=404)
        overrides = {int(key): dict(value) for key, value in (place_overrides or {}).items()}
        now = datetime.utcnow()
        try:
            for profile_revision in run.profile_revisions:
                self._apply_or_reject_profile(profile_revision, selected, now)
            for asset_revision in run.asset_revisions:
                self._apply_or_reject_asset(asset_revision, selected, now)
            for place_revision in run.place_revisions:
                self._apply_or_reject_place(run, place_revision, selected, overrides, now)
            for library_revision in run.library_revisions:
                self._apply_or_reject_library(run, library_revision, selected, now)
            decision = DecisionLog(
                blogger_id=blogger_id,
                decision_type="feedback_confirm",
                prompt_version=self.prompt_version,
                input_summary=self._json(
                    {"feedback_run_id": run.id, "snapshot_hash": run.snapshot_hash}
                ),
                decision=self._json({"selected_candidate_ids": sorted(selected)}),
                reason="用户明确确认反馈候选，按冻结快照原子应用",
            )
            self.db.add(decision)
            self.db.flush()
            self.memory_service.sync_profile(blogger_id, user_confirmed=True, commit=False)
            analysis = self._decode(run.analysis_json, {})
            memory_id = analysis.get("memory_candidate_id")
            promoted = False
            if memory_id is not None:
                memory = self.db.get(MemoryRecord, int(memory_id))
                if memory is not None and memory.blogger_id == blogger_id and memory.status == "candidate":
                    self.memory_service.promote_memory(memory.id, user_confirmed=True, commit=False)
                    promoted = True
            if not promoted:
                self.memory_service.create_memory(
                    blogger_id,
                    "decision_summary",
                    f"已确认反馈：运行{run.id}",
                    f"反馈运行{run.id}：{run.summary or '用户已确认反馈候选'}",
                    "decision_log",
                    decision.id,
                    confidence=1.0,
                    status="active",
                    user_confirmed=True,
                    commit=False,
                )
            run.status = "applied"
            run.applied_at = now
            run.updated_at = now
            self.db.commit()
            return self.get(blogger_id, run.id)
        except FeedbackServiceError:
            self.db.rollback()
            raise
        except Exception as exc:
            self.db.rollback()
            raise FeedbackServiceError("FEEDBACK_APPLY_FAILED", status_code=500) from exc

    apply = confirm

    def reject(
        self,
        blogger_id: int,
        run_id: int,
        *,
        candidate_ids: Sequence[str] | None = None,
        reason: str = "用户明确拒绝反馈候选",
    ) -> FeedbackRun:
        run = self.get(blogger_id, run_id)
        if run.status == "rejected":
            return run
        if run.status == "applied":
            raise FeedbackServiceError("FEEDBACK_REJECT_INVALID_STATE", status_code=409)
        if run.status != "analyzed":
            raise FeedbackServiceError("FEEDBACK_REJECT_INVALID_STATE", status_code=409)
        selected = set(candidate_ids or [row["id"] for row in self.get_candidates(blogger_id, run.id)])
        known = {row["id"] for row in self.get_candidates(blogger_id, run.id)}
        if not selected or not selected <= known:
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_NOT_FOUND", status_code=404)
        now = datetime.utcnow()
        changed = 0
        for prefix, revisions in self._revision_groups(run):
            for revision in revisions:
                if f"{prefix}:{revision.id}" not in selected:
                    continue
                if revision.status == "applied":
                    raise FeedbackServiceError("FEEDBACK_CANDIDATE_ALREADY_APPLIED", status_code=409)
                if revision.status == "pending":
                    revision.status = "rejected"
                    revision.rejected_at = now
                    revision.updated_at = now
                    changed += 1
        self.db.add(
            DecisionLog(
                blogger_id=blogger_id,
                decision_type="feedback_reject",
                prompt_version=self.prompt_version,
                input_summary=self._json({"feedback_run_id": run.id}),
                decision=self._json({"rejected_candidate_ids": sorted(selected)}),
                reason=reason,
            )
        )
        all_terminal = all(
            revision.status != "pending"
            for _, revisions in self._revision_groups(run)
            for revision in revisions
        )
        if all_terminal:
            run.status = "rejected"
            run.rejected_at = now
        run.updated_at = now
        try:
            self.db.commit()
            return self.get(blogger_id, run.id)
        except SQLAlchemyError as exc:
            self.db.rollback()
            raise FeedbackServiceError("FEEDBACK_PERSIST_FAILED", status_code=500) from exc

    # ------------------------------------------------------------------
    # 分析落库
    # ------------------------------------------------------------------
    def _persist_analysis(
        self,
        run: FeedbackRun,
        snapshot: Mapping[str, Any],
        normalized: dict[str, Any],
    ) -> None:
        try:
            for evidence in snapshot.get("evidence_whitelist", []):
                if not isinstance(evidence, Mapping):
                    continue
                self.db.add(
                    FeedbackEvidence(
                        feedback_run_id=run.id,
                        evidence_type=str(evidence["evidence_type"]),
                        ref_id=int(evidence["ref_id"]),
                        claim=str(evidence.get("claim") or "冻结反馈证据"),
                        snapshot_json=self._json(evidence.get("snapshot") or {}),
                    )
                )
            for candidate in normalized.get("suit_type_candidates", []):
                self._add_profile_revision(run, "suit_type", candidate)
            for candidate in normalized.get("knowledge_focus_candidates", []):
                self._add_profile_revision(run, "knowledge_focus", candidate)
            asset_map = {int(item["id"]): item for item in snapshot.get("assets", [])}
            for candidate in normalized.get("asset_effects", []):
                asset_id = int(candidate["asset_id"])
                before = asset_map[asset_id]
                self.db.add(
                    AssetEffectRevision(
                        run_id=run.id,
                        asset_id=asset_id,
                        before_effect=before.get("effect"),
                        after_effect=candidate.get("effect"),
                        before_weight=before.get("effect_weight"),
                        after_weight=candidate.get("effect_weight"),
                        reason=str(candidate["reason"]),
                        status="pending",
                        version=1,
                    )
                )
            place_map = {int(item["id"]): item for item in snapshot.get("places", [])}
            for candidate in normalized.get("place_effects", []):
                place_id = int(candidate["place_id"])
                before = {
                    field: place_map[place_id].get(field) for field in sorted(self.COMMERCIAL_FIELDS)
                }
                after: dict[str, Any] = {}
                field = str(candidate.get("commercial_field") or "")
                if field in self.COMMERCIAL_FIELDS and candidate.get("after") is not None:
                    after[field] = candidate["after"]
                after["simulation_only"] = bool(candidate.get("simulation_only"))
                after["applicable"] = bool(candidate.get("applicable"))
                self.db.add(
                    PlaceCommercialRevision(
                        run_id=run.id,
                        place_id=place_id,
                        before_json=self._json(before),
                        after_json=self._json(after),
                        reason=str(candidate["reason"]),
                        status="pending",
                        version=1,
                    )
                )
            for candidate in normalized.get("library_evolution", []):
                self.db.add(
                    LibraryEvolutionRevision(
                        run_id=run.id,
                        lib_type=str(candidate["lib_type"]),
                        action=str(candidate["action"]),
                        target_asset_id=candidate.get("target_asset_id"),
                        candidate_json=self._json(
                            {
                                **dict(candidate.get("candidate") or {}),
                                "simulation_only": bool(candidate.get("simulation_only")),
                            }
                        ),
                        reason=str(candidate["reason"]),
                        status="pending",
                        version=1,
                    )
                )
            decision = DecisionLog(
                blogger_id=run.blogger_id,
                decision_type="feedback_analysis",
                prompt_version=self.prompt_version,
                input_summary=self._json(
                    {"feedback_run_id": run.id, "snapshot_hash": run.snapshot_hash}
                ),
                decision=self._json(normalized),
                reason="反馈分析只生成待确认候选，不自动修改业务数据",
            )
            self.db.add(decision)
            self.db.flush()
            candidate_memory = self.memory_service.create_memory(
                run.blogger_id,
                "decision_summary",
                f"反馈候选摘要：运行{run.id}",
                f"反馈运行{run.id}：{normalized.get('summary') or '反馈候选待确认'}",
                "feedback_run",
                run.id,
                confidence=0.7,
                status="candidate",
                user_confirmed=False,
                commit=False,
            )
            normalized = {**normalized, "memory_candidate_id": candidate_memory.id}
            run.snapshot_json = self._json(snapshot)
            run.analysis_json = self._json(normalized)
            run.summary = str(normalized.get("summary") or "")
            run.status = "analyzed"
            run.error_code = None
            run.error_message = None
            run.updated_at = datetime.utcnow()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _add_profile_revision(
        self,
        run: FeedbackRun,
        field_name: str,
        candidate: Mapping[str, Any],
    ) -> None:
        blogger = self._active_blogger(run.blogger_id)
        self.db.add(
            ProfileFeedbackRevision(
                run_id=run.id,
                blogger_id=run.blogger_id,
                field_name=field_name,
                before=getattr(blogger, field_name),
                after=str(candidate["value"]),
                reason=str(candidate["reason"]),
                status="pending",
                version=1,
            )
        )

    # ------------------------------------------------------------------
    # 确认应用
    # ------------------------------------------------------------------
    def _apply_or_reject_profile(
        self,
        revision: ProfileFeedbackRevision,
        selected: set[str],
        now: datetime,
    ) -> None:
        token = f"profile:{revision.id}"
        if token not in selected:
            self._mark_rejected(revision, now)
            return
        if revision.status == "rejected":
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_REJECTED", status_code=409)
        if revision.status == "applied":
            return
        blogger = self._active_blogger(revision.blogger_id)
        if getattr(blogger, revision.field_name) != revision.before:
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
        setattr(blogger, revision.field_name, revision.after)
        self._mark_applied(revision, now)

    def _apply_or_reject_asset(
        self,
        revision: AssetEffectRevision,
        selected: set[str],
        now: datetime,
    ) -> None:
        token = f"asset_effect:{revision.id}"
        if token not in selected:
            self._mark_rejected(revision, now)
            return
        if revision.status == "rejected":
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_REJECTED", status_code=409)
        if revision.status == "applied":
            return
        asset = self.db.scalar(
            select(Asset).where(
                Asset.id == revision.asset_id,
                Asset.deleted_at.is_(None),
            )
        )
        if asset is None or asset.blogger_id != revision.feedback_run.blogger_id:
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
        if asset.effect != revision.before_effect or asset.effect_weight != revision.before_weight:
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
        asset.effect = revision.after_effect
        asset.effect_weight = revision.after_weight
        self._mark_applied(revision, now)

    def _apply_or_reject_place(
        self,
        run: FeedbackRun,
        revision: PlaceCommercialRevision,
        selected: set[str],
        overrides: Mapping[int, Mapping[str, Any]],
        now: datetime,
    ) -> None:
        token = f"place_commercial:{revision.id}"
        if token not in selected:
            self._mark_rejected(revision, now)
            return
        if revision.status == "rejected":
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_REJECTED", status_code=409)
        if revision.status == "applied":
            return
        place = self.db.scalar(
            select(Place).where(
                Place.id == revision.place_id,
                Place.blogger_id == run.blogger_id,
                Place.deleted_at.is_(None),
            )
        )
        if place is None:
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
        before = self._decode(revision.before_json, {})
        if any(getattr(place, field) != before.get(field) for field in self.COMMERCIAL_FIELDS):
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
        after = self._decode(revision.after_json, {})
        if after.get("simulation_only"):
            if overrides.get(place.id):
                raise FeedbackServiceError("FEEDBACK_SIMULATION_BOUNDARY")
            self._mark_rejected(revision, now)
            return
        values = {
            field: value
            for field, value in {**after, **dict(overrides.get(place.id, {}))}.items()
            if field in self.COMMERCIAL_FIELDS and value is not None
        }
        # Agent 建议不能把未知商业值补成数字；NULL 只接受确认请求中的显式覆盖。
        for field, value in values.items():
            if before.get(field) is None and field not in overrides.get(place.id, {}):
                raise FeedbackServiceError("FEEDBACK_COMMERCIAL_CONFIRMATION_REQUIRED")
            setattr(place, field, value)
        revision.after_json = self._json(values)
        self._mark_applied(revision, now)

    def _apply_or_reject_library(
        self,
        run: FeedbackRun,
        revision: LibraryEvolutionRevision,
        selected: set[str],
        now: datetime,
    ) -> None:
        token = f"library_evolution:{revision.id}"
        if token not in selected:
            self._mark_rejected(revision, now)
            return
        if revision.status == "rejected":
            raise FeedbackServiceError("FEEDBACK_CANDIDATE_REJECTED", status_code=409)
        if revision.status == "applied":
            return
        candidate = self._decode(revision.candidate_json, {})
        if revision.action == "add":
            title = str(candidate.get("title") or "").strip()
            content = str(candidate.get("content") or "").strip()
            category = str(candidate.get("category") or "反馈进化").strip()
            if not title or not content:
                raise FeedbackServiceError("FEEDBACK_LIBRARY_CANDIDATE_INVALID")
            dedupe = hashlib.sha256(
                f"feedback|{run.blogger_id}|{revision.lib_type}|{title}|{content}".encode()
            ).hexdigest()
            asset = self.db.scalar(
                select(Asset).where(
                    Asset.blogger_id == run.blogger_id,
                    Asset.dedupe_key == dedupe,
                )
            )
            if asset is None:
                asset = Asset(
                    blogger_id=run.blogger_id,
                    lib_type=revision.lib_type,
                    category=category,
                    title=title,
                    content=content,
                    tags_json=self._json([category, "反馈确认"]),
                    source_type="user_confirmed",
                    credibility=4,
                    origin="feedback",
                    dedupe_key=dedupe,
                    manual_locked=False,
                    effect="effective",
                    effect_weight=0.6,
                )
                self.db.add(asset)
                self.db.flush()
                encoded = self.embedding_service.encode_documents([f"{title}\n{content}"])[0]
                vector = np.asarray(encoded.vector, dtype=np.float32)
                self.db.add(
                    AssetEmbedding(
                        asset_id=asset.id,
                        model_name=self.embedding_service.model_name,
                        model_version="phase4-feedback-v1",
                        dimension=int(vector.size),
                        vector=self.embedding_service.to_bytes(vector),
                        vector_norm=float(np.linalg.norm(vector)),
                        content_hash=encoded.content_hash,
                    )
                )
            revision.target_asset_id = asset.id
        elif revision.target_asset_id is not None:
            asset = self.db.scalar(
                select(Asset).where(
                    Asset.id == revision.target_asset_id,
                    Asset.blogger_id == run.blogger_id,
                    Asset.deleted_at.is_(None),
                )
            )
            if asset is None:
                raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)
            if not asset.manual_locked:
                if revision.action == "reinforce":
                    asset.effect = "effective"
                    asset.effect_weight = min(1.0, float(asset.effect_weight or 0.5) + 0.1)
                else:
                    asset.effect = "review"
        self._mark_applied(revision, now)

    def _assert_snapshot_current(self, run: FeedbackRun) -> None:
        snapshot = self._decode(run.snapshot_json, {})
        current = self.analysis_service.build_snapshot(
            run.blogger_id,
            run.output_id,
            run.primary_metric_id,
            task_id=run.task_id,
            user_instruction=str(snapshot.get("user_instruction") or ""),
        )
        if str(current["snapshot_hash"]) != run.snapshot_hash:
            raise FeedbackServiceError("FEEDBACK_SNAPSHOT_CHANGED", status_code=409)

    # ------------------------------------------------------------------
    # 查询与错误恢复
    # ------------------------------------------------------------------
    def _run_or_error(
        self,
        blogger_id: int,
        run_id: int,
        *,
        with_children: bool = False,
    ) -> FeedbackRun:
        self._active_blogger(blogger_id)
        statement = select(FeedbackRun).where(
            FeedbackRun.id == run_id,
            FeedbackRun.blogger_id == blogger_id,
        )
        if with_children:
            statement = statement.options(
                selectinload(FeedbackRun.evidences),
                selectinload(FeedbackRun.profile_revisions),
                selectinload(FeedbackRun.asset_revisions),
                selectinload(FeedbackRun.place_revisions),
                selectinload(FeedbackRun.library_revisions),
            )
        run = self.db.scalar(statement)
        if run is None:
            raise FeedbackServiceError("FEEDBACK_NOT_FOUND", status_code=404)
        return run

    def _active_blogger(self, blogger_id: int) -> Blogger:
        blogger = self.db.scalar(
            select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None))
        )
        if blogger is None:
            raise FeedbackServiceError("BLOGGER_NOT_FOUND", status_code=404)
        return blogger

    def _persist_failure(self, run_id: int, code: str, message: str) -> None:
        self.db.rollback()
        run = self.db.get(FeedbackRun, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error_code = code
        run.error_message = str(message)[:1000]
        run.updated_at = datetime.utcnow()
        self.db.commit()
        if run.task_id:
            try:
                self.task_service.fail_task(run.task_id, str(message), error_code=code)
            except Exception:
                self.db.rollback()

    @staticmethod
    def _mark_applied(revision: Any, now: datetime) -> None:
        revision.status = "applied"
        revision.confirmed_at = now
        revision.applied_at = now
        revision.updated_at = now

    @staticmethod
    def _mark_rejected(revision: Any, now: datetime) -> None:
        if revision.status == "pending":
            revision.status = "rejected"
            revision.rejected_at = now
            revision.updated_at = now

    @staticmethod
    def _revision_groups(run: FeedbackRun) -> list[tuple[str, Sequence[Any]]]:
        return [
            ("profile", run.profile_revisions),
            ("asset_effect", run.asset_revisions),
            ("place_commercial", run.place_revisions),
            ("library_evolution", run.library_revisions),
        ]

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, FeedbackServiceError):
            return exc.code
        if isinstance(exc, (FeedbackAgentError, FeedbackValidationError, FeedbackAnalysisError)):
            return exc.code
        if isinstance(exc, SQLAlchemyError):
            return "FEEDBACK_PERSIST_FAILED"
        return "FEEDBACK_FAILED"

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _decode(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default


__all__ = ["FeedbackService", "FeedbackServiceError"]

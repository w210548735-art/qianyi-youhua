"""第二阶段知识库体检编排服务。"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Assessment,
    AssessmentEvidence,
    AssessmentIndicator,
    Asset,
    Blogger,
    DecisionLog,
    TaskSession,
)
from app.services.assessment_agent import AssessmentAgent, DeepSeekAssessmentAgent
from app.services.assessment_validation_service import (
    AssessmentValidationError,
    AssessmentValidationService,
)
from app.services.context_service import ContextService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService


class AssessmentServiceError(RuntimeError):
    """携带稳定错误码的体检异常。"""

    def __init__(self, code: str, *, status_code: int = 422, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail or code


class AssessmentService:
    """编排快照、分析、Agent、校验、证据、任务记忆和长期记忆。"""

    prompt_version = "phase2-assessment-v1"

    def __init__(
        self,
        db: Session,
        *,
        agent: AssessmentAgent | None = None,
        analysis_service: LibraryAnalysisService | None = None,
        validation_service: AssessmentValidationService | None = None,
        task_service: TaskMemoryService | None = None,
        context_service: ContextService | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.db = db
        self.agent = agent or DeepSeekAssessmentAgent()
        self.analysis_service = analysis_service or LibraryAnalysisService(db)
        self.validation_service = validation_service or AssessmentValidationService()
        self.task_service = task_service or TaskMemoryService(db)
        self.memory_service = memory_service or MemoryService(db)
        self.context_service = context_service or ContextService(
            db,
            memory_service=self.memory_service,
            system_rules=(
                "你是黔衣有话知识库体检Agent。只能依据当前快照、当前任务记忆和当前博主的长期记忆；"
                "不得引用其他博主或快照外资产，不得把后续功能描述成已经实现。"
            ),
        )

    def start_assessment(self, blogger_id: int, idempotency_key: str) -> Assessment:
        """校验博主和三库后创建幂等的待执行体检与任务。"""

        self._active_blogger(blogger_id)
        key = idempotency_key.strip()
        if not key:
            raise AssessmentServiceError("INDICATOR_RULE_VIOLATION")
        existing = self.db.scalar(
            select(Assessment).where(
                Assessment.blogger_id == blogger_id,
                Assessment.idempotency_key == key,
            )
        )
        if existing is not None:
            return existing
        self._validate_libraries(blogger_id)
        task_id = f"assessment-{blogger_id}-{sha256(key.encode('utf-8')).hexdigest()[:16]}"
        task = self.task_service.create_task(
            blogger_id,
            "assessment",
            "知识库体检与指标评估",
            task_id=task_id,
            initial_context="等待生成知识库快照",
            metadata={"idempotency_key": key, "phase": "pending"},
        )
        assessment = Assessment(
            blogger_id=blogger_id,
            task_id=task.id,
            status="pending",
            idempotency_key=key,
            input_snapshot_json="{}",
            prompt_version=self.prompt_version,
            model_name=getattr(self.agent, "model_name", "unknown"),
        )
        self.db.add(assessment)
        try:
            self.db.commit()
            self.db.refresh(assessment)
            return assessment
        except SQLAlchemyError as exc:
            self.db.rollback()
            same = self.db.scalar(
                select(Assessment).where(
                    Assessment.blogger_id == blogger_id,
                    Assessment.idempotency_key == key,
                )
            )
            if same is not None:
                return same
            raise AssessmentServiceError("ASSESSMENT_PERSIST_FAILED", status_code=500) from exc

    def execute_assessment(self, assessment_id: int, blogger_id: int | None = None) -> Assessment:
        """执行一次体检；失败时只保留安全错误与任务恢复信息。"""

        assessment = self._assessment_or_error(assessment_id, blogger_id)
        if assessment.status == "succeeded":
            return assessment
        if assessment.status == "running":
            raise AssessmentServiceError("ASSESSMENT_ALREADY_RUNNING", status_code=409)
        if assessment.status == "failed":
            raise AssessmentServiceError("ASSESSMENT_NOT_FOUND", status_code=409, detail="请使用retry接口")
        task_id = assessment.task_id
        if task_id is None:
            raise AssessmentServiceError("ASSESSMENT_PERSIST_FAILED", status_code=500)

        try:
            assessment.status = "running"
            assessment.started_at = datetime.utcnow()
            assessment.error_code = None
            assessment.error_message = None
            self.db.commit()
            self.task_service.append_message(task_id, "assistant", "开始生成知识库资产快照")
            snapshot = self.analysis_service.build_snapshot(assessment.blogger_id)
            snapshot_hash = self.analysis_service.calculate_snapshot_hash(snapshot)
            assessment.snapshot_hash = snapshot_hash
            assessment.input_snapshot_json = self._json(snapshot)
            self.db.commit()
            self.task_service.create_checkpoint(
                task_id,
                {"phase": "snapshot", "snapshot_hash": snapshot_hash},
                context_snapshot="知识库快照已冻结，准备执行确定性分析",
            )

            analysis = self.analysis_service.analyze(snapshot)
            self.task_service.create_checkpoint(
                task_id,
                {"phase": "analysis", "snapshot_hash": snapshot_hash},
                context_snapshot=self._json(analysis),
            )
            context_input = {
                "snapshot_hash": snapshot_hash,
                "counts": analysis.get("counts", {}),
                "source_coverage": analysis.get("source_coverage", {}),
                "core_assets": analysis.get("core_assets", []),
                "weak_categories": analysis.get("weak_categories", []),
                "feature_readiness": analysis.get("feature_readiness", {}),
                "missing_items": analysis.get("missing_items", []),
            }
            context = self.context_service.assemble_context(
                assessment.blogger_id,
                self._json(context_input),
                task_id=task_id,
            )
            raw_result = self._call_agent(context.as_messages(), analysis)
            normalized = self.validation_service.validate_and_normalize(
                raw_result,
                snapshot=snapshot,
            )
            current_snapshot = self.analysis_service.build_snapshot(assessment.blogger_id)
            if self.analysis_service.calculate_snapshot_hash(current_snapshot) != snapshot_hash:
                raise AssessmentServiceError("LIBRARY_SNAPSHOT_CHANGED", status_code=409)

            overall_score = self.validation_service.calculate_overall_score(normalized["indicators"])
            # final_summary 及任务状态先完成；若文件或任务提交失败，不会产生
            # Assessment/Decision/Memory 的成功半成品。后续落库失败时 fail_task
            # 会把摘要安全改写为失败状态。
            self.task_service.complete_task(
                task_id,
                {
                    "assessment_id": assessment.id,
                    "snapshot_hash": snapshot_hash,
                    "overall_score": overall_score,
                    "summary": normalized["summary"],
                },
                memory_candidates=[
                    {
                        "memory_type": "decision_summary",
                        "title": "知识库体检摘要",
                        "content": normalized["summary"],
                        "source_type": "assessment",
                        "source_id": str(assessment.id),
                        "confidence": 0.7,
                    }
                ],
            )
            self._persist_success(assessment, analysis, normalized, overall_score)
            return assessment
        except Exception as exc:
            code = self._error_code(exc)
            self._persist_failure(assessment.id, code, str(exc))
            if isinstance(exc, AssessmentServiceError):
                raise
            if code in {
                "AGENT_TIMEOUT",
                "AGENT_INVALID_JSON",
                "INDICATOR_RULE_VIOLATION",
                "EVIDENCE_REFERENCE_INVALID",
            }:
                raise AssessmentServiceError(code) from exc
            raise AssessmentServiceError("ASSESSMENT_PERSIST_FAILED", status_code=500) from exc

    def retry_assessment(self, blogger_id: int, assessment_id: int) -> Assessment:
        assessment = self._assessment_or_error(assessment_id, blogger_id)
        if assessment.status == "running":
            raise AssessmentServiceError("ASSESSMENT_ALREADY_RUNNING", status_code=409)
        if assessment.status == "succeeded":
            return assessment
        task = self.db.get(TaskSession, assessment.task_id)
        if task is None:
            raise AssessmentServiceError("ASSESSMENT_PERSIST_FAILED", status_code=500)
        task.status = "running"
        task.completed_at = None
        task.current_context = "体检失败后重试，保留已有消息与检查点"
        assessment.status = "pending"
        assessment.error_code = None
        assessment.error_message = None
        self.db.commit()
        self.task_service.sync_task_files(task.id)
        return self.execute_assessment(assessment.id, blogger_id)

    def get_assessment(self, blogger_id: int, assessment_id: int) -> Assessment:
        self._active_blogger(blogger_id)
        return self._assessment_or_error(assessment_id, blogger_id, with_children=True)

    def list_assessments(self, blogger_id: int, *, limit: int = 50, offset: int = 0) -> list[Assessment]:
        self._active_blogger(blogger_id)
        if limit < 1 or limit > 100 or offset < 0:
            raise AssessmentServiceError("INDICATOR_RULE_VIOLATION")
        statement = (
            select(Assessment)
            .where(Assessment.blogger_id == blogger_id)
            .order_by(Assessment.created_at.desc(), Assessment.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.scalars(statement))

    def list_evidence(self, blogger_id: int, assessment_id: int) -> list[AssessmentEvidence]:
        self.get_assessment(blogger_id, assessment_id)
        return list(
            self.db.scalars(
                select(AssessmentEvidence)
                .where(AssessmentEvidence.assessment_id == assessment_id)
                .order_by(AssessmentEvidence.indicator_id, AssessmentEvidence.id)
            )
        )

    def recover_unfinished_assessments(self, blogger_id: int | None = None) -> list[Assessment]:
        """服务重启后把中断中的体检置为可重试失败态并恢复任务文件。"""

        statement = select(Assessment).where(Assessment.status.in_(("pending", "running")))
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(Assessment.blogger_id == blogger_id)
        rows = list(self.db.scalars(statement.order_by(Assessment.id)))
        for assessment in rows:
            assessment.status = "failed"
            assessment.error_code = "ASSESSMENT_PERSIST_FAILED"
            assessment.error_message = "服务重启导致体检中断，可安全重试"
            assessment.finished_at = datetime.utcnow()
            self.db.commit()
            if assessment.task_id is not None:
                try:
                    self.task_service.recover_task(assessment.task_id)
                    self.task_service.fail_task(
                        assessment.task_id,
                        assessment.error_message,
                        error_code=assessment.error_code,
                    )
                except Exception:
                    self.db.rollback()
        return rows

    def _persist_success(
        self,
        assessment: Assessment,
        analysis: dict[str, Any],
        normalized: dict[str, Any],
        overall_score: float,
    ) -> None:
        try:
            self.db.execute(delete(AssessmentEvidence).where(AssessmentEvidence.assessment_id == assessment.id))
            self.db.execute(delete(AssessmentIndicator).where(AssessmentIndicator.assessment_id == assessment.id))
            indicator_rows: list[tuple[AssessmentIndicator, list[dict[str, Any]]]] = []
            for ordinal, item in enumerate(normalized["indicators"], start=1):
                evidence = list(item.get("evidence", []))
                indicator = AssessmentIndicator(
                    assessment_id=assessment.id,
                    ordinal=ordinal,
                    name=item["name"],
                    meaning=item["meaning"],
                    score_logic=item["score_logic"],
                    business_meaning=item["business_meaning"],
                    weight=float(item["weight"]),
                    weight_reason=item["weight_reason"],
                    score=float(item["score"]),
                    reason=item["reason"],
                    evidence_json=self._json(evidence),
                )
                self.db.add(indicator)
                self.db.flush()
                indicator_rows.append((indicator, evidence))
            for indicator, evidence_items in indicator_rows:
                for evidence_row in evidence_items:
                    if evidence_row["evidence_type"] == "relation":
                        for endpoint, field in (
                            ("relation_from", "from_asset_id"),
                            ("relation_to", "to_asset_id"),
                        ):
                            self.db.add(
                                AssessmentEvidence(
                                    assessment_id=assessment.id,
                                    indicator_id=indicator.id,
                                    evidence_type=endpoint,
                                    asset_id=evidence_row.get(field),
                                    claim=evidence_row["claim"],
                                )
                            )
                    else:
                        self.db.add(
                            AssessmentEvidence(
                                assessment_id=assessment.id,
                                indicator_id=indicator.id,
                                evidence_type=evidence_row["evidence_type"],
                                asset_id=evidence_row.get("asset_id"),
                                source_document_id=evidence_row.get("source_document_id"),
                                claim=evidence_row["claim"],
                            )
                        )
            decision = DecisionLog(
                blogger_id=assessment.blogger_id,
                decision_type="assessment",
                prompt_version=self.prompt_version,
                input_summary=self._json(
                    {"assessment_id": assessment.id, "snapshot_hash": assessment.snapshot_hash}
                ),
                decision=self._json(
                    {"overall_score": overall_score, "summary": normalized["summary"]}
                ),
                reason="确定性库分析、受控Agent指标与后端证据校验共同生成",
            )
            self.db.add(decision)
            self.db.flush()
            assessment.library_analysis_json = self._json(analysis)
            assessment.feature_readiness_json = self._json(normalized["feature_readiness"])
            assessment.suggestions_json = self._json(normalized["suggestions"])
            assessment.summary = normalized["summary"]
            assessment.overall_score = overall_score
            assessment.decision_id = decision.id
            assessment.status = "succeeded"
            assessment.finished_at = datetime.utcnow()
            assessment.error_code = None
            assessment.error_message = None
            self.memory_service.create_memory(
                assessment.blogger_id,
                "decision_summary",
                "知识库体检摘要",
                (
                    f"体检ID：{assessment.id}\n快照：{assessment.snapshot_hash}\n"
                    f"{normalized['summary']}"
                ),
                "decision_log",
                decision.id,
                confidence=0.7,
                status="candidate",
                user_confirmed=False,
            )
        except Exception:
            self.db.rollback()
            raise

    def _persist_failure(self, assessment_id: int, code: str, message: str) -> None:
        self.db.rollback()
        assessment = self.db.get(Assessment, assessment_id)
        if assessment is None:
            return
        self.db.execute(delete(AssessmentEvidence).where(AssessmentEvidence.assessment_id == assessment.id))
        self.db.execute(delete(AssessmentIndicator).where(AssessmentIndicator.assessment_id == assessment.id))
        assessment.status = "failed"
        assessment.error_code = code
        assessment.error_message = self._safe_error(message)
        assessment.library_analysis_json = None
        assessment.feature_readiness_json = None
        assessment.suggestions_json = None
        assessment.summary = None
        assessment.overall_score = None
        assessment.decision_id = None
        assessment.finished_at = datetime.utcnow()
        self.db.commit()
        task_id = assessment.task_id
        if task_id is None:
            return
        try:
            self.task_service.append_message(task_id, "assistant", f"体检失败：{code}")
            self.task_service.create_checkpoint(
                task_id,
                {"phase": "failed", "error_code": code},
                context_snapshot=f"体检失败，可从错误码 {code} 重试",
            )
            self.task_service.fail_task(task_id, code, error_code=code)
        except Exception:
            self.db.rollback()

    def _assessment_or_error(
        self,
        assessment_id: int,
        blogger_id: int | None,
        *,
        with_children: bool = False,
    ) -> Assessment:
        statement = select(Assessment).where(Assessment.id == assessment_id)
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(Assessment.blogger_id == blogger_id)
        if with_children:
            statement = statement.options(
                selectinload(Assessment.indicators),
                selectinload(Assessment.evidences),
            )
        assessment = self.db.scalar(statement)
        if assessment is None:
            raise AssessmentServiceError("ASSESSMENT_NOT_FOUND", status_code=404)
        return assessment

    def _active_blogger(self, blogger_id: int) -> Blogger:
        blogger = self.db.get(Blogger, blogger_id)
        if blogger is None:
            raise AssessmentServiceError("BLOGGER_NOT_FOUND", status_code=404)
        if blogger.deleted_at is not None:
            raise AssessmentServiceError("BLOGGER_DELETED", status_code=404)
        return blogger

    def _validate_libraries(self, blogger_id: int) -> None:
        rows = self.db.execute(
            select(Asset.lib_type, func.count(Asset.id))
            .where(Asset.blogger_id == blogger_id, Asset.deleted_at.is_(None))
            .group_by(Asset.lib_type)
        )
        counts = {str(lib_type): int(count) for lib_type, count in rows}
        if sum(counts.values()) == 0:
            raise AssessmentServiceError("LIBRARY_EMPTY")
        if any(counts.get(lib_type, 0) == 0 for lib_type in ("knowledge", "material", "algorithm")):
            raise AssessmentServiceError("THREE_LIBRARIES_INCOMPLETE")

    def _call_agent(self, context_messages: list[dict[str, str]], analysis: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.agent.assess(context_messages=context_messages, analysis=analysis)
        except TimeoutError as exc:
            raise AssessmentServiceError("AGENT_TIMEOUT") from exc

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, AssessmentServiceError):
            return exc.code
        if isinstance(exc, AssessmentValidationError):
            code = getattr(exc, "code", str(exc))
            return str(code) if str(code) else "INDICATOR_RULE_VIOLATION"
        value = str(exc)
        for code in (
            "AGENT_TIMEOUT",
            "AGENT_INVALID_JSON",
            "INDICATOR_RULE_VIOLATION",
            "EVIDENCE_REFERENCE_INVALID",
            "LIBRARY_SNAPSHOT_CHANGED",
        ):
            if code in value:
                return code
        return "ASSESSMENT_PERSIST_FAILED"

    @staticmethod
    def _safe_error(message: str) -> str:
        if not message:
            return "体检执行失败"
        upper = message.upper()
        if "KEY" in upper or "AUTHORIZATION" in upper or "TRACEBACK" in upper:
            return "体检依赖调用失败"
        return message[:500]

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = ["AssessmentService", "AssessmentServiceError"]

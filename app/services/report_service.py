"""经营报告生成、失败恢复、历史读取与比较编排。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Blogger,
    DecisionLog,
    IndicatorObservation,
    MemoryRecord,
    Report,
    ReportEvidence,
    TaskSession,
)
from app.services.indicator_service import FormulaResult
from app.services.report_agent import DeepSeekReportAgent, ReportAgent, ReportAgentError
from app.services.report_comparison_service import ReportComparisonError, ReportComparisonService
from app.services.report_data_service import ReportDataError, ReportDataService
from app.services.report_validation_service import ReportValidationError, ReportValidationService
from app.services.task_memory_service import TaskMemoryService


class ReportServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool = False,
        status_code: int = 422,
    ) -> None:
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"{code}: {self.message}")


class ReportService:
    prompt_version = "phase4-report-v1"

    def __init__(
        self,
        db: Session,
        *,
        agent: ReportAgent | None = None,
        data_service: ReportDataService | None = None,
        validation_service: ReportValidationService | None = None,
        task_service: TaskMemoryService | None = None,
        comparison_service: ReportComparisonService | None = None,
    ) -> None:
        self.db = db
        self.agent = agent or DeepSeekReportAgent()
        self.data_service = data_service or ReportDataService(db)
        self.validation_service = validation_service or ReportValidationService()
        self.task_service = task_service or TaskMemoryService(db)
        self.comparison_service = comparison_service or ReportComparisonService(db)

    def generate(
        self,
        blogger_id: int,
        idempotency_key: str,
        *,
        user_instruction: str = "",
        task_id: str | None = None,
        request_id: str | None = None,
    ) -> Report:
        self._active_blogger(blogger_id)
        key = idempotency_key.strip()
        if not key:
            raise ReportServiceError("REPORT_VALIDATION_ERROR", "idempotency_key 不能为空")
        existing = self.db.scalar(select(Report).where(Report.blogger_id == blogger_id, Report.idempotency_key == key))
        if existing is not None:
            return existing
        snapshot = self.data_service.build_snapshot(blogger_id)
        task = self._task(blogger_id, key, task_id)
        report = Report(
            blogger_id=blogger_id,
            task_id=task.id,
            status="running",
            idempotency_key=key,
            snapshot_json=self._json(snapshot),
            snapshot_hash=snapshot["snapshot_hash"],
            prompt_version=self.prompt_version,
            model_name=getattr(self.agent, "model_name", "unknown"),
        )
        self.db.add(report)
        try:
            self.db.commit()
            self.db.refresh(report)
        except IntegrityError as exc:
            self.db.rollback()
            same = self.db.scalar(select(Report).where(Report.blogger_id == blogger_id, Report.idempotency_key == key))
            if same is not None:
                return same
            raise ReportServiceError("REPORT_PERSIST_FAILED", status_code=500) from exc
        self._safe_checkpoint(task.id, "snapshot", report)
        return self._execute(report, snapshot, user_instruction=user_instruction, request_id=request_id)

    generate_report = generate

    def retry(
        self,
        blogger_id: int,
        report_id: int,
        *,
        user_instruction: str = "",
        request_id: str | None = None,
    ) -> Report:
        report = self.get(blogger_id, report_id)
        if report.status == "succeeded":
            return report
        if report.status == "running":
            raise ReportServiceError("REPORT_ALREADY_RUNNING", status_code=409)
        current = self.data_service.build_snapshot(blogger_id)
        if report.snapshot_hash != current["snapshot_hash"]:
            raise ReportServiceError(
                "REPORT_SNAPSHOT_CONFLICT", "业务数据已变化，请使用新幂等键生成报告", status_code=409
            )
        task = self.db.get(TaskSession, report.task_id)
        if task is not None:
            task.status = "running"
            task.completed_at = None
            task.current_context = "经营报告失败后重试"
        report.status = "running"
        report.error_code = None
        report.error_message = None
        report.completed_at = None
        self.db.commit()
        if task is not None:
            try:
                self.task_service.sync_task_files(task.id)
            except Exception:
                self.db.rollback()
        return self._execute(report, current, user_instruction=user_instruction, request_id=request_id)

    retry_report = retry

    def get(self, blogger_id: int, report_id: int) -> Report:
        self._active_blogger(blogger_id)
        row = self.db.scalar(select(Report).where(Report.id == report_id, Report.blogger_id == blogger_id))
        if row is None:
            raise ReportServiceError("REPORT_NOT_FOUND", "报告不存在", status_code=404)
        return row

    get_report = get

    def list_reports(self, blogger_id: int, *, limit: int = 100, offset: int = 0) -> list[Report]:
        self._active_blogger(blogger_id)
        if limit < 1 or limit > 1000 or offset < 0:
            raise ReportServiceError("REPORT_VALIDATION_ERROR", "分页参数不合法")
        return list(
            self.db.scalars(
                select(Report)
                .where(Report.blogger_id == blogger_id)
                .order_by(Report.created_at.desc(), Report.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    def list_evidence(self, blogger_id: int, report_id: int) -> list[ReportEvidence]:
        self.get(blogger_id, report_id)
        return list(
            self.db.scalars(
                select(ReportEvidence)
                .where(ReportEvidence.report_id == report_id)
                .order_by(ReportEvidence.evidence_type, ReportEvidence.ref_id)
            )
        )

    get_evidence = list_evidence

    def compare(self, blogger_id: int, left_id: int, right_id: int) -> dict[str, Any]:
        try:
            return self.comparison_service.compare(blogger_id, left_id, right_id)
        except ReportComparisonError as exc:
            status = 404 if exc.code in {"BLOGGER_NOT_FOUND", "REPORT_NOT_FOUND"} else 409
            raise ReportServiceError(exc.code, exc.message, status_code=status) from exc

    compare_reports = compare

    def recover_unfinished(self, blogger_id: int | None = None) -> list[Report]:
        statement = select(Report).where(Report.status.in_(("pending", "running")))
        if blogger_id is not None:
            self._active_blogger(blogger_id)
            statement = statement.where(Report.blogger_id == blogger_id)
        rows = list(self.db.scalars(statement.order_by(Report.id)))
        for row in rows:
            row.status = "failed"
            row.error_code = "REPORT_INTERRUPTED"
            row.error_message = "服务中断，可安全重试"
            row.completed_at = datetime.utcnow()
        self.db.commit()
        return rows

    def _execute(
        self,
        report: Report,
        snapshot: dict[str, Any],
        *,
        user_instruction: str,
        request_id: str | None,
    ) -> Report:
        try:
            generated = self.agent.generate(
                snapshot,
                user_instruction,
                request_id=request_id or f"report-{report.id}-{report.snapshot_hash[:12]}",
            )
            normalized = self.validation_service.validate(generated, snapshot)
            current = self.data_service.build_snapshot(report.blogger_id)
            if current["snapshot_hash"] != report.snapshot_hash:
                raise ReportServiceError("REPORT_SNAPSHOT_CONFLICT", "分析期间业务数据发生变化", status_code=409)
            self._persist_success(report, snapshot, normalized)
            self._safe_task_success(report, normalized)
            return report
        except Exception as exc:
            code, retryable, status_code = self._error(exc)
            self._persist_failure(report.id, code, str(exc))
            self._safe_task_failure(report.task_id, code, str(exc))
            if isinstance(exc, ReportServiceError):
                raise
            raise ReportServiceError(code, str(exc), retryable=retryable, status_code=status_code) from exc

    def _persist_success(self, report: Report, snapshot: dict[str, Any], generated: dict[str, Any]) -> None:
        try:
            self.db.execute(delete(ReportEvidence).where(ReportEvidence.report_id == report.id))
            self.db.execute(delete(IndicatorObservation).where(IndicatorObservation.report_id == report.id))
            for evidence in snapshot["evidence"]:
                self.db.add(
                    ReportEvidence(
                        report_id=report.id,
                        evidence_type=evidence["evidence_type"],
                        ref_id=evidence["ref_id"],
                        claim=evidence["claim"],
                        snapshot_json=self._json(evidence["snapshot"]),
                    )
                )
            for indicator in snapshot["indicators"]:
                result = FormulaResult(
                    value=indicator["value"],
                    status=indicator["status"],
                    evidence=indicator["evidence"],
                )
                self.db.add(
                    IndicatorObservation(
                        indicator_id=indicator["indicator_id"],
                        report_id=report.id,
                        value=indicator["value"],
                        status=indicator["status"],
                        trend=self.data_service.indicators.observation_trend(indicator["indicator_id"], result),
                        evidence_json=self._json(indicator["evidence"]),
                        observed_at=datetime.utcnow(),
                    )
                )
            conclusion = {
                "money": snapshot["facts"]["money"],
                "traffic": snapshot["facts"]["traffic"],
                "product": snapshot["facts"]["product"],
                "supplier": snapshot["facts"]["supplier"],
                "explanations": generated["sections"],
                "summary": generated["summary"],
            }
            decision = DecisionLog(
                blogger_id=report.blogger_id,
                decision_type="report_generation",
                prompt_version=self.prompt_version,
                input_summary=self._json({"report_id": report.id, "snapshot_hash": report.snapshot_hash}),
                decision=self._json({"status": "succeeded", "conclusion": conclusion}),
                reason="后端白名单公式、确定性图表与受校验Agent解释共同生成",
            )
            self.db.add(decision)
            self.db.flush()
            memory_content = f"经营报告候选摘要\n报告ID：{report.id}\n{generated['summary']}"
            self.db.add(
                MemoryRecord(
                    blogger_id=report.blogger_id,
                    memory_type="decision_summary",
                    title="经营报告摘要候选",
                    content=memory_content,
                    source_type="decision_log",
                    source_id=str(decision.id),
                    confidence=0.7,
                    status="candidate",
                    version=1,
                    content_hash=hashlib.sha256(memory_content.encode("utf-8")).hexdigest(),
                )
            )
            report.conclusion_json = self._json(conclusion)
            report.charts_json = self._json(list(snapshot["charts"].values()))
            report.suggestions_json = self._json(generated["suggestions"])
            report.data_quality_json = self._json(snapshot["data_quality"])
            report.status = "succeeded"
            report.error_code = None
            report.error_message = None
            report.completed_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(report)
        except Exception:
            self.db.rollback()
            raise

    def _persist_failure(self, report_id: int, code: str, message: str) -> None:
        self.db.rollback()
        report = self.db.get(Report, report_id)
        if report is None or report.status == "succeeded":
            return
        # 失败记录不保留半成品证据、观察或伪结论。
        self.db.execute(delete(ReportEvidence).where(ReportEvidence.report_id == report.id))
        self.db.execute(delete(IndicatorObservation).where(IndicatorObservation.report_id == report.id))
        report.status = "failed"
        report.conclusion_json = None
        report.charts_json = None
        report.suggestions_json = None
        report.data_quality_json = None
        report.error_code = code
        report.error_message = message[:2000]
        report.completed_at = datetime.utcnow()
        self.db.commit()

    def _task(self, blogger_id: int, key: str, task_id: str | None) -> TaskSession:
        resolved = task_id or f"report-{blogger_id}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"
        existing = self.db.get(TaskSession, resolved)
        if existing is not None and existing.blogger_id != blogger_id:
            raise ReportServiceError("REPORT_NOT_FOUND", "任务不存在", status_code=404)
        return self.task_service.create_task(
            blogger_id,
            "report",
            "经营指标重算与报告生成",
            task_id=resolved,
            initial_context="等待冻结经营数据快照",
            metadata={"idempotency_key": key, "phase": "pending"},
        )

    def _safe_checkpoint(self, task_id: str, phase: str, report: Report) -> None:
        try:
            self.task_service.append_message(task_id, "assistant", f"经营报告进入{phase}阶段")
            self.task_service.create_checkpoint(
                task_id,
                {"phase": phase, "report_id": report.id, "snapshot_hash": report.snapshot_hash},
                context_snapshot=f"经营报告快照已冻结：{report.snapshot_hash}",
            )
        except Exception:
            self.db.rollback()

    def _safe_task_success(self, report: Report, generated: dict[str, Any]) -> None:
        if report.task_id is None:
            return
        try:
            self.task_service.record_decision(
                report.task_id,
                {"report_id": report.id, "snapshot_hash": report.snapshot_hash, "status": "succeeded"},
                reason="经营报告已由确定性数据和严格验证共同完成",
                input_summary={"report_id": report.id},
                decision_type="report",
                prompt_version=self.prompt_version,
            )
            self.task_service.complete_task(
                report.task_id,
                {"report_id": report.id, "snapshot_hash": report.snapshot_hash, "summary": generated["summary"]},
                memory_candidates=[
                    {
                        "memory_type": "decision_summary",
                        "title": "经营报告摘要候选",
                        "content": generated["summary"],
                    }
                ],
            )
        except Exception:
            self.db.rollback()

    def _safe_task_failure(self, task_id: str | None, code: str, message: str) -> None:
        if task_id is None:
            return
        try:
            task = self.db.get(TaskSession, task_id)
            if task is not None and task.status not in {"completed", "failed", "cancelled"}:
                self.task_service.fail_task(task_id, message[:1000], error_code=code)
        except Exception:
            self.db.rollback()

    def _active_blogger(self, blogger_id: int) -> Blogger:
        row = self.db.scalar(select(Blogger).where(Blogger.id == blogger_id, Blogger.deleted_at.is_(None)))
        if row is None:
            raise ReportServiceError("BLOGGER_NOT_FOUND", "博主不存在或已删除", status_code=404)
        return row

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _error(exc: Exception) -> tuple[str, bool, int]:
        if isinstance(exc, ReportServiceError):
            return exc.code, exc.retryable, exc.status_code
        if isinstance(exc, ReportAgentError):
            return exc.code, exc.retryable, 502
        if isinstance(exc, ReportValidationError):
            return exc.code, False, 422
        if isinstance(exc, ReportDataError):
            return exc.code, False, 404 if exc.code == "BLOGGER_NOT_FOUND" else 422
        return "REPORT_PERSIST_FAILED", True, 500


__all__ = ["ReportService", "ReportServiceError"]

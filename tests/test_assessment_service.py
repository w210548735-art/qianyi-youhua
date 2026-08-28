from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Assessment,
    AssessmentEvidence,
    AssessmentIndicator,
    Asset,
    Blogger,
    DecisionLog,
    MemoryRecord,
    TaskCheckpoint,
    TaskSession,
)
from app.services.assessment_agent import AssessmentAgentError, FakeAssessmentAgent
from app.services.assessment_service import AssessmentService, AssessmentServiceError
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.build_service import LibraryBuildService
from app.services.context_service import ContextService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.task_memory_service import TaskMemoryService


def make_blogger(db, name: str = "体检博主") -> Blogger:
    blogger = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食", "景区", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        routes="黔东南",
        frequency="周更",
        profile_state="complete",
    )
    db.add(blogger)
    db.commit()
    db.refresh(blogger)
    return blogger


def build_three_libraries(db, blogger: Blogger, key: str) -> None:
    service = LibraryBuildService(db, FakeDeepSeekClient(), FakeEmbeddingService())
    run = service.start_build(blogger.id, key)
    assert service.execute_build(run.id).status == "succeeded"


def make_service(
    db,
    tasks_root: Path,
    *,
    agent: FakeAssessmentAgent | None = None,
    analysis: LibraryAnalysisService | None = None,
) -> AssessmentService:
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding)
    return AssessmentService(
        db,
        agent=agent or FakeAssessmentAgent(),
        analysis_service=analysis or LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=TaskMemoryService(db, tasks_root),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )


class CapturingAssessmentAgent(FakeAssessmentAgent):
    def __init__(self) -> None:
        super().__init__()
        self.context_messages: list[dict[str, str]] = []

    def assess(self, context_messages, analysis, *, request_id=None):
        self.context_messages = [dict(item) for item in context_messages]
        return super().assess(context_messages, analysis, request_id=request_id)


class FailingCompletionTaskService(TaskMemoryService):
    def complete_task(self, *args, **kwargs):
        raise OSError("final summary write failed")


def test_success_persists_nonempty_evidence_children_task_decision_and_candidate_memory(db, tmp_path: Path):
    blogger = make_blogger(db)
    build_three_libraries(db, blogger, "assessment-build-success")
    agent = CapturingAssessmentAgent()
    service = make_service(db, tmp_path / "tasks", agent=agent)

    pending = service.start_assessment(blogger.id, "assessment-success-0001")
    result = service.execute_assessment(pending.id, blogger.id)
    loaded = service.get_assessment(blogger.id, result.id)

    assert loaded.status == "succeeded"
    assert [item["role"] for item in agent.context_messages] == ["system", "system", "system", "user"]
    assert "embedding\"" not in agent.context_messages[-1]["content"]
    assert loaded.snapshot_hash and loaded.overall_score is not None
    assert len(loaded.indicators) >= 3
    assert len(loaded.evidences) >= 3
    assert all(row.evidences for row in loaded.indicators)
    assert db.scalar(
        select(func.count()).select_from(AssessmentEvidence).where(
            AssessmentEvidence.assessment_id == loaded.id
        )
    ) == len(loaded.evidences)
    assert db.scalar(
        select(func.count()).select_from(DecisionLog).where(
            DecisionLog.id == loaded.decision_id,
            DecisionLog.decision_type == "assessment",
        )
    ) == 1
    task = db.get(TaskSession, loaded.task_id)
    assert task is not None and task.status == "completed"
    assert db.scalar(
        select(func.count()).select_from(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)
    ) >= 2
    candidate = db.scalar(
        select(MemoryRecord).where(
            MemoryRecord.blogger_id == blogger.id,
            MemoryRecord.memory_type == "decision_summary",
            MemoryRecord.status == "candidate",
        )
    )
    assert candidate is not None
    assert json.loads((tmp_path / "tasks" / task.id / "final_summary.json").read_text("utf-8"))[
        "overall_score"
    ] == loaded.overall_score

    relation_rows = [row for row in loaded.evidences if row.evidence_type.startswith("relation")]
    assert relation_rows
    assert {row.evidence_type for row in relation_rows} >= {"relation_from", "relation_to"}
    assert all(row.asset_id is not None for row in relation_rows)


def test_idempotency_history_and_cross_blogger_access_are_isolated(db, tmp_path: Path):
    owner = make_blogger(db, "甲")
    other = make_blogger(db, "乙")
    build_three_libraries(db, owner, "assessment-build-owner")
    service = make_service(db, tmp_path / "tasks")
    first = service.start_assessment(owner.id, "same-assessment-key")
    same = service.start_assessment(owner.id, "same-assessment-key")
    assert same.id == first.id
    service.execute_assessment(first.id, owner.id)
    second = service.start_assessment(owner.id, "new-assessment-key")
    service.execute_assessment(second.id, owner.id)

    assert [row.id for row in service.list_assessments(owner.id)] == [second.id, first.id]
    assert db.scalar(select(func.count()).select_from(Assessment)) == 2
    with pytest.raises(AssessmentServiceError, match="ASSESSMENT_NOT_FOUND"):
        service.get_assessment(other.id, first.id)


def test_empty_and_incomplete_libraries_are_rejected_before_task_creation(db, tmp_path: Path):
    blogger = make_blogger(db)
    service = make_service(db, tmp_path / "tasks")
    with pytest.raises(AssessmentServiceError, match="LIBRARY_EMPTY"):
        service.start_assessment(blogger.id, "empty-library-key")
    assert db.scalar(select(func.count()).select_from(TaskSession)) == 0

    db.add(
        Asset(
            blogger_id=blogger.id,
            lib_type="knowledge",
            category="美食",
            title="只有知识",
            content="只有美食知识",
            tags_json='["美食"]',
            source_type="manual",
            credibility=2,
            origin="manual",
            manual_locked=True,
            dedupe_key="only-knowledge",
        )
    )
    db.commit()
    with pytest.raises(AssessmentServiceError, match="THREE_LIBRARIES_INCOMPLETE"):
        service.start_assessment(blogger.id, "incomplete-library-key")
    assert db.scalar(select(func.count()).select_from(TaskSession)) == 0


def test_agent_failure_has_no_partial_report_and_retry_reuses_assessment(db, tmp_path: Path):
    blogger = make_blogger(db)
    build_three_libraries(db, blogger, "assessment-build-retry")
    failing = FakeAssessmentAgent(
        fail_with=AssessmentAgentError("AGENT_INVALID_JSON", "无效结构", retryable=True)
    )
    service = make_service(db, tmp_path / "tasks", agent=failing)
    pending = service.start_assessment(blogger.id, "assessment-retry-key")
    with pytest.raises(AssessmentServiceError, match="AGENT_INVALID_JSON"):
        service.execute_assessment(pending.id, blogger.id)

    failed = db.get(Assessment, pending.id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "AGENT_INVALID_JSON"
    assert failed.overall_score is None and failed.summary is None and failed.decision_id is None
    assert db.scalar(select(func.count()).select_from(AssessmentIndicator)) == 0
    assert db.scalar(select(func.count()).select_from(AssessmentEvidence)) == 0

    service.agent = FakeAssessmentAgent()
    recovered = service.retry_assessment(blogger.id, pending.id)
    assert recovered.id == pending.id and recovered.status == "succeeded"
    assert db.scalar(select(func.count()).select_from(Assessment)) == 1
    assert db.scalar(select(func.count()).select_from(AssessmentIndicator)) >= 3


class ChangingSnapshotAnalysis(LibraryAnalysisService):
    def __init__(self, db) -> None:
        super().__init__(db, FakeEmbeddingService())
        self.calls = 0

    def build_snapshot(self, blogger_id: int):
        snapshot = super().build_snapshot(blogger_id)
        self.calls += 1
        if self.calls >= 2:
            snapshot = deepcopy(snapshot)
            snapshot["assets"][0]["title"] = "执行期间发生变化"
        return snapshot


def test_snapshot_change_fails_without_indicator_or_evidence_garbage(db, tmp_path: Path):
    blogger = make_blogger(db)
    build_three_libraries(db, blogger, "assessment-build-snapshot")
    analysis = ChangingSnapshotAnalysis(db)
    service = make_service(db, tmp_path / "tasks", analysis=analysis)
    pending = service.start_assessment(blogger.id, "assessment-snapshot-key")

    with pytest.raises(AssessmentServiceError, match="LIBRARY_SNAPSHOT_CHANGED"):
        service.execute_assessment(pending.id, blogger.id)

    failed = db.get(Assessment, pending.id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "LIBRARY_SNAPSHOT_CHANGED"
    assert db.scalar(select(func.count()).select_from(AssessmentIndicator)) == 0
    assert db.scalar(select(func.count()).select_from(AssessmentEvidence)) == 0


def test_final_summary_failure_leaves_no_success_decision_memory_or_report(db, tmp_path: Path):
    blogger = make_blogger(db)
    build_three_libraries(db, blogger, "assessment-build-final-summary-failure")
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding)
    service = AssessmentService(
        db,
        agent=FakeAssessmentAgent(),
        analysis_service=LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=FailingCompletionTaskService(db, tmp_path / "tasks"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )
    pending = service.start_assessment(blogger.id, "assessment-final-summary-failure")

    with pytest.raises(AssessmentServiceError, match="ASSESSMENT_PERSIST_FAILED"):
        service.execute_assessment(pending.id, blogger.id)

    failed = db.get(Assessment, pending.id)
    assert failed is not None and failed.status == "failed"
    assert db.scalar(select(func.count()).select_from(AssessmentIndicator)) == 0
    assert db.scalar(select(func.count()).select_from(AssessmentEvidence)) == 0
    assert db.scalar(
        select(func.count()).select_from(DecisionLog).where(DecisionLog.decision_type == "assessment")
    ) == 0
    assert db.scalar(
        select(func.count()).select_from(MemoryRecord).where(MemoryRecord.source_type == "decision_log")
    ) == 0


def test_recover_unfinished_assessment_marks_it_retryable_and_preserves_task_checkpoint(db, tmp_path: Path):
    blogger = make_blogger(db)
    build_three_libraries(db, blogger, "assessment-build-recover")
    service = make_service(db, tmp_path / "tasks")
    pending = service.start_assessment(blogger.id, "assessment-recover-after-restart")
    pending.status = "running"
    db.commit()

    recovered = service.recover_unfinished_assessments()

    assert [row.id for row in recovered] == [pending.id]
    row = db.get(Assessment, pending.id)
    assert row is not None and row.status == "failed"
    assert row.error_code == "ASSESSMENT_PERSIST_FAILED"
    assert service.retry_assessment(blogger.id, pending.id).status == "succeeded"

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Assessment,
    Blogger,
    DecisionLog,
    MemoryRecord,
    Output,
    OutputAsset,
    TaskCheckpoint,
    TaskSession,
)
from app.services.assessment_agent import FakeAssessmentAgent
from app.services.assessment_service import AssessmentService
from app.services.assessment_validation_service import AssessmentValidationService
from app.services.build_service import LibraryBuildService
from app.services.context_service import ContextService
from app.services.deepseek_client import FakeDeepSeekClient
from app.services.embedding_service import FakeEmbeddingService
from app.services.library_analysis_service import LibraryAnalysisService
from app.services.memory_service import MemoryService
from app.services.output_agent import FakeOutputAgent, OutputAgentError
from app.services.output_service import OutputService, OutputServiceError
from app.services.output_validation_service import OutputValidationService
from app.services.task_memory_service import TaskMemoryService


def make_blogger(db, name: str = "第三阶段博主") -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json=json.dumps(["美食", "景区", "非遗"], ensure_ascii=False),
        style="口播",
        follower_band="1万-10万",
        monetization_types_json=json.dumps(["商单"], ensure_ascii=False),
        routes="黔东南",
        viral_topic="酸汤鱼",
        frequency="周更",
        profile_state="complete",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_assessment(db, blogger: Blogger, tasks_root: Path) -> Assessment:
    embedding = FakeEmbeddingService()
    build = LibraryBuildService(db, FakeDeepSeekClient(), embedding)
    run = build.start_build(blogger.id, f"phase3-build-{blogger.id}")
    assert build.execute_build(run.id).status == "succeeded"
    memory = MemoryService(db, embedding)
    service = AssessmentService(
        db,
        agent=FakeAssessmentAgent(),
        analysis_service=LibraryAnalysisService(db, embedding),
        validation_service=AssessmentValidationService(),
        task_service=TaskMemoryService(db, tasks_root / "assessment"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
    )
    pending = service.start_assessment(blogger.id, f"phase3-assessment-{blogger.id}")
    return service.execute_assessment(pending.id, blogger.id)


def make_service(db, tasks_root: Path, agent: FakeOutputAgent | None = None) -> OutputService:
    embedding = FakeEmbeddingService()
    memory = MemoryService(db, embedding)
    return OutputService(
        db,
        agent=agent or FakeOutputAgent(),
        validation_service=OutputValidationService(),
        task_service=TaskMemoryService(db, tasks_root / "output"),
        context_service=ContextService(db, memory_service=memory),
        memory_service=memory,
        analysis_service=LibraryAnalysisService(db, embedding),
    )


def test_script_and_storyboard_persist_evidence_task_memory_and_candidate(db, tmp_path: Path):
    blogger = make_blogger(db)
    assessment = make_assessment(db, blogger, tmp_path)
    service = make_service(db, tmp_path)

    pending = service.start_generation(
        blogger.id,
        "script",
        assessment.id,
        "phase3-script-idempotency",
        user_instruction="做一条酸汤鱼口播",
    )
    same = service.start_generation(
        blogger.id,
        "script",
        assessment.id,
        "phase3-script-idempotency",
    )
    assert same.id == pending.id
    script = service.execute_generation(pending.id, blogger.id)

    assert script.status == "succeeded" and script.assessment_id == assessment.id
    payload = json.loads(script.content_json)
    assert {"category", "title", "hook", "body", "ending", "tags", "style", "platform", "source_refs"} <= payload.keys()
    assert script.assets and all(item.asset_id for item in script.assets)
    assert db.scalar(select(func.count()).select_from(OutputAsset).where(OutputAsset.output_id == script.id)) > 0
    assert db.get(DecisionLog, script.decision_id).decision_type == "output_generation"
    task = db.get(TaskSession, script.task_id)
    assert task is not None and task.status == "completed"
    assert db.scalar(select(func.count()).select_from(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)) >= 2
    assert db.scalar(
        select(func.count()).select_from(MemoryRecord).where(
            MemoryRecord.blogger_id == blogger.id,
            MemoryRecord.source_type == "decision_log",
            MemoryRecord.status == "candidate",
        )
    ) >= 1

    storyboard_pending = service.start_generation(
        blogger.id,
        "storyboard",
        assessment.id,
        "phase3-storyboard-idempotency",
        parent_output_id=script.id,
    )
    storyboard = service.execute_generation(storyboard_pending.id, blogger.id)
    storyboard_payload = json.loads(storyboard.content_json)
    assert storyboard.status == "succeeded"
    assert storyboard_payload["script_id"] == script.id
    assert storyboard_payload["shots"]
    assert all(
        {"sequence", "visual", "dialogue", "duration", "bgm", "transition", "source_refs"} <= shot.keys()
        for shot in storyboard_payload["shots"]
    )


def test_invalid_reference_failure_has_no_partial_output_evidence_and_can_retry(db, tmp_path: Path):
    blogger = make_blogger(db)
    assessment = make_assessment(db, blogger, tmp_path)
    bad = FakeOutputAgent(
        response={
            "script": {
                "category": "美食",
                "title": "虚假引用",
                "hook": "开场",
                "body": "正文",
                "ending": "结尾",
                "tags": ["贵州"],
                "style": "口播",
                "platform": "抖音",
                "source_refs": [{"asset_id": 999999, "claim": "不存在"}],
            }
        }
    )
    service = make_service(db, tmp_path, bad)
    pending = service.start_generation(blogger.id, "script", assessment.id, "phase3-bad-reference")

    with pytest.raises(OutputServiceError, match="OUTPUT_EVIDENCE_INVALID"):
        service.execute_generation(pending.id, blogger.id)

    failed = db.get(Output, pending.id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "OUTPUT_EVIDENCE_INVALID"
    assert failed.decision_id is None
    assert db.scalar(select(func.count()).select_from(OutputAsset).where(OutputAsset.output_id == failed.id)) == 0

    service.agent = FakeOutputAgent()
    recovered = service.retry_generation(blogger.id, failed.id)
    assert recovered.id == failed.id and recovered.status == "succeeded"


def test_cross_blogger_soft_delete_and_revision_history_are_isolated(db, tmp_path: Path):
    owner = make_blogger(db, "甲")
    other = make_blogger(db, "乙")
    assessment = make_assessment(db, owner, tmp_path)
    service = make_service(db, tmp_path)
    pending = service.start_generation(owner.id, "script", assessment.id, "phase3-revision")
    original = service.execute_generation(pending.id, owner.id)
    content = json.loads(original.content_json)
    content["title"] = "人工修订版"
    revised = service.revise_output(owner.id, original.id, content)

    assert revised.id != original.id and revised.version == 2 and revised.manual_locked is True
    assert db.get(Output, original.id).title != revised.title
    assert [row.id for row in service.list_outputs(owner.id)] == [revised.id, original.id]
    with pytest.raises(OutputServiceError, match="OUTPUT_NOT_FOUND"):
        service.get_output(other.id, original.id)

    service.soft_delete_output(owner.id, revised.id)
    assert [row.id for row in service.list_outputs(owner.id)] == [original.id]
    with pytest.raises(OutputServiceError, match="OUTPUT_NOT_FOUND"):
        service.get_output(owner.id, revised.id)


class ChangingOutputSnapshotService(OutputService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def build_snapshot(self, blogger_id: int, assessment_id: int | None):
        result = super().build_snapshot(blogger_id, assessment_id)
        self.calls += 1
        if self.calls >= 2:
            result = deepcopy(result)
            result["assets"][0]["title"] = "执行期间被修改"
        return result


def test_snapshot_conflict_and_agent_failure_are_retryable_without_garbage(db, tmp_path: Path):
    blogger = make_blogger(db)
    assessment = make_assessment(db, blogger, tmp_path)
    base = make_service(db, tmp_path)
    service = ChangingOutputSnapshotService(
        db,
        agent=FakeOutputAgent(),
        validation_service=base.validation_service,
        task_service=base.task_service,
        context_service=base.context_service,
        memory_service=base.memory_service,
        analysis_service=base.analysis_service,
    )
    pending = service.start_generation(blogger.id, "script", assessment.id, "phase3-snapshot-change")
    with pytest.raises(OutputServiceError, match="OUTPUT_SNAPSHOT_CHANGED"):
        service.execute_generation(pending.id, blogger.id)
    assert db.get(Output, pending.id).error_code == "OUTPUT_SNAPSHOT_CHANGED"
    assert db.scalar(select(func.count()).select_from(OutputAsset).where(OutputAsset.output_id == pending.id)) == 0

    failing = make_service(
        db,
        tmp_path,
        FakeOutputAgent(fail_with=OutputAgentError("OUTPUT_INVALID_JSON", "非法响应", retryable=True)),
    )
    failed_pending = failing.start_generation(blogger.id, "script", assessment.id, "phase3-agent-failure")
    with pytest.raises(OutputServiceError, match="OUTPUT_INVALID_JSON"):
        failing.execute_generation(failed_pending.id, blogger.id)
    assert db.get(Output, failed_pending.id).status == "failed"

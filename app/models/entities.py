from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class Blogger(Base):
    __tablename__ = "blogger"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    content_types_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    style: Mapped[str] = mapped_column(String(50), nullable=False)
    follower_band: Mapped[str] = mapped_column(String(50), nullable=False)
    monetization_types_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    routes: Mapped[str | None] = mapped_column(Text)
    viral_topic: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str | None] = mapped_column(String(50))
    suit_type: Mapped[str | None] = mapped_column(Text)
    knowledge_focus: Mapped[str | None] = mapped_column(Text)
    profile_state: Mapped[str] = mapped_column(String(30), nullable=False, default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    assets: Mapped[list[Asset]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    places: Mapped[list[Place]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    outputs: Mapped[list[Output]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    schedules: Mapped[list[Schedule]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    feedback_runs: Mapped[list[FeedbackRun]] = relationship(
        back_populates="blogger", passive_deletes=True
    )
    feedback_profile_revisions: Mapped[list[ProfileFeedbackRevision]] = relationship(
        back_populates="blogger", passive_deletes=True
    )
    operational_indicators: Mapped[list[OperationalIndicator]] = relationship(
        back_populates="blogger", passive_deletes=True
    )
    reports: Mapped[list[Report]] = relationship(back_populates="blogger", passive_deletes=True)


class ConversationSession(Base):
    __tablename__ = "conversation_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="collecting")
    current_question: Mapped[str] = mapped_column(String(50), nullable=False, default="name")
    collected_profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    blogger_id: Mapped[int | None] = mapped_column(ForeignKey("blogger.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ConversationMessage(Base):
    __tablename__ = "conversation_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceDocument(Base):
    __tablename__ = "source_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    publisher: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    verified_at: Mapped[str] = mapped_column(String(20), nullable=False)
    content_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class BuildRun(Base):
    __tablename__ = "build_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    input_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    output_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class DecisionLog(Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int | None] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), index=True)
    build_run_id: Mapped[int | None] = mapped_column(ForeignKey("build_run.id", ondelete="SET NULL"), index=True)
    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(30), nullable=False, default="v1")
    input_summary: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint("credibility >= 0 AND credibility <= 5", name="ck_asset_credibility"),
        CheckConstraint(
            "effect_weight IS NULL OR (effect_weight >= 0 AND effect_weight <= 1)",
            name="ck_asset_effect_weight",
        ),
        UniqueConstraint("blogger_id", "dedupe_key", name="uq_asset_blogger_dedupe"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), index=True)
    lib_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    credibility: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="seed")
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    manual_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effect: Mapped[str | None] = mapped_column(String(50))
    effect_weight: Mapped[float | None] = mapped_column(Float)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision_log.id", ondelete="SET NULL"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="assets")
    embedding: Mapped[AssetEmbedding | None] = relationship(
        back_populates="asset", cascade="all, delete-orphan", uselist=False
    )
    sources: Mapped[list[AssetSource]] = relationship(cascade="all, delete-orphan")
    output_assets: Mapped[list[OutputAsset]] = relationship(back_populates="asset")
    asset_places: Mapped[list[AssetPlace]] = relationship(back_populates="asset")
    effect_revisions: Mapped[list[AssetEffectRevision]] = relationship(
        back_populates="asset", passive_deletes=True
    )


class AssetSource(Base):
    __tablename__ = "asset_source"
    __table_args__ = (UniqueConstraint("asset_id", "source_document_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id", ondelete="CASCADE"), index=True)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("source_document.id", ondelete="RESTRICT"), index=True)


class AssetEmbedding(Base):
    __tablename__ = "asset_embedding"

    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vector_norm: Mapped[float] = mapped_column(Float, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="embedding")


class Place(Base):
    __tablename__ = "place"
    __table_args__ = (
        CheckConstraint("credibility >= 0 AND credibility <= 5", name="ck_place_credibility"),
        UniqueConstraint("blogger_id", "dedupe_key", name="uq_place_blogger_dedupe"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    location: Mapped[str | None] = mapped_column(Text)
    specialty: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    credibility: Mapped[int] = mapped_column(Integer, nullable=False)
    like_level: Mapped[int | None] = mapped_column(Integer)
    est_cost: Mapped[float | None] = mapped_column(Float)
    est_benefit: Mapped[float | None] = mapped_column(Float)
    fits_koc: Mapped[bool | None] = mapped_column(Boolean)
    fits_shoot: Mapped[bool | None] = mapped_column(Boolean)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision_log.id", ondelete="SET NULL"))
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    manual_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="places")
    output_places: Mapped[list[OutputPlace]] = relationship(back_populates="place")
    asset_places: Mapped[list[AssetPlace]] = relationship(back_populates="place")
    commercial_revisions: Mapped[list[PlaceCommercialRevision]] = relationship(
        back_populates="place", passive_deletes=True
    )


class Assessment(Base):
    """一次知识库体检的不可变结果快照。

    体检执行过程中的状态和错误保存在本表；只有成功体检才会写入指标和证据
    子表。指标子表不提供更新字段，服务层应通过重新体检创建新的快照。
    """

    __tablename__ = "assessment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_assessment_status",
        ),
        CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)",
            name="ck_assessment_overall_score",
        ),
        UniqueConstraint("blogger_id", "idempotency_key", name="uq_assessment_blogger_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_session.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    library_analysis_json: Mapped[str | None] = mapped_column(Text)
    feature_readiness_json: Mapped[str | None] = mapped_column(Text)
    suggestions_json: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    overall_score: Mapped[float | None] = mapped_column(Float)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_log.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False, default="phase2-v1")
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, default="deepseek-v4-flash")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="assessments")
    task: Mapped[TaskSession | None] = relationship(back_populates="assessments")
    decision: Mapped[DecisionLog | None] = relationship()
    indicators: Mapped[list[AssessmentIndicator]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", order_by="AssessmentIndicator.ordinal"
    )
    evidences: Mapped[list[AssessmentEvidence]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", order_by="AssessmentEvidence.id"
    )
    outputs: Mapped[list[Output]] = relationship(back_populates="assessment")


class AssessmentIndicator(Base):
    """体检时由 Agent 定义并固化的指标快照。"""

    __tablename__ = "assessment_indicator"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_assessment_indicator_ordinal"),
        CheckConstraint("weight > 0 AND weight <= 100", name="ck_assessment_indicator_weight"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="ck_assessment_indicator_score",
        ),
        UniqueConstraint("assessment_id", "ordinal", name="uq_assessment_indicator_ordinal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    score_logic: Mapped[str] = mapped_column(Text, nullable=False)
    business_meaning: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    weight_reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assessment: Mapped[Assessment] = relationship(back_populates="indicators")
    evidences: Mapped[list[AssessmentEvidence]] = relationship(
        back_populates="indicator", cascade="all, delete-orphan", order_by="AssessmentEvidence.id"
    )


class AssessmentEvidence(Base):
    """把体检结论绑定到本博主当前快照中的资产与来源。"""

    __tablename__ = "assessment_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indicator_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_indicator.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("asset.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_document.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assessment: Mapped[Assessment] = relationship(back_populates="evidences")
    indicator: Mapped[AssessmentIndicator] = relationship(back_populates="evidences")
    asset: Mapped[Asset | None] = relationship()
    source_document: Mapped[SourceDocument | None] = relationship()


class MemoryRecord(Base):
    __tablename__ = "memory_record"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        UniqueConstraint("blogger_id", "memory_type", "content_hash", "version", name="uq_memory_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="candidate", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_memory_id: Mapped[int | None] = mapped_column(ForeignKey("memory_record.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    embedding: Mapped[MemoryEmbedding | None] = relationship(
        back_populates="memory", cascade="all, delete-orphan", uselist=False
    )


class MemoryEmbedding(Base):
    __tablename__ = "memory_embedding"

    memory_id: Mapped[int] = mapped_column(ForeignKey("memory_record.id", ondelete="CASCADE"), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vector_norm: Mapped[float] = mapped_column(Float, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    memory: Mapped[MemoryRecord] = relationship(back_populates="embedding")


class TaskSession(Base):
    __tablename__ = "task_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    blogger_id: Mapped[int] = mapped_column(ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running", index=True)
    current_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recovery_state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    task_dir: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    assessments: Mapped[list[Assessment]] = relationship(back_populates="task")
    outputs: Mapped[list[Output]] = relationship(back_populates="task")
    feedback_runs: Mapped[list[FeedbackRun]] = relationship(back_populates="task")
    reports: Mapped[list[Report]] = relationship(back_populates="task")


class SessionMessage(Base):
    __tablename__ = "session_message"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_task_message_sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_session.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskCheckpoint(Base):
    __tablename__ = "task_checkpoint"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_checkpoint_sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_session.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskArtifact(Base):
    __tablename__ = "task_artifact"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_session.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Output(Base):
    """脚本、分镜和路线推荐的不可变版本化输出。"""

    __tablename__ = "output"
    __table_args__ = (
        CheckConstraint(
            "type IN ('script', 'storyboard', 'route_rec')",
            name="ck_output_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'draft', 'deleted')",
            name="ck_output_status",
        ),
        CheckConstraint("version > 0", name="ck_output_version"),
        UniqueConstraint(
            "blogger_id", "parent_output_id", "version", name="uq_output_version"
        ),
        UniqueConstraint(
            "blogger_id", "idempotency_key", name="uq_output_blogger_idempotency"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_session.id", ondelete="SET NULL"), nullable=True, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assessment.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_output_id: Mapped[int | None] = mapped_column(
        ForeignKey("output.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    manual_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_log.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False, default="phase3-v1")
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, default="deepseek-v4-flash")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="outputs")
    task: Mapped[TaskSession | None] = relationship(back_populates="outputs")
    assessment: Mapped[Assessment | None] = relationship(back_populates="outputs")
    decision: Mapped[DecisionLog | None] = relationship()
    parent_output: Mapped[Output | None] = relationship(
        remote_side="Output.id", back_populates="child_outputs"
    )
    child_outputs: Mapped[list[Output]] = relationship(back_populates="parent_output")
    assets: Mapped[list[OutputAsset]] = relationship(
        back_populates="output", cascade="all, delete-orphan", order_by="OutputAsset.id"
    )
    places: Mapped[list[OutputPlace]] = relationship(
        back_populates="output", cascade="all, delete-orphan", order_by="OutputPlace.sequence"
    )
    schedules: Mapped[list[Schedule]] = relationship(back_populates="output")
    metrics: Mapped[list[Metric]] = relationship(back_populates="output")
    feedback_runs: Mapped[list[FeedbackRun]] = relationship(back_populates="output")


class OutputAsset(Base):
    """输出中引用的知识、素材或算法资产。"""

    __tablename__ = "output_asset"
    __table_args__ = (
        UniqueConstraint("output_id", "asset_id", "usage_type", name="uq_output_asset_usage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    output_id: Mapped[int] = mapped_column(
        ForeignKey("output.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("asset.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    usage_type: Mapped[str] = mapped_column(String(50), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)

    output: Mapped[Output] = relationship(back_populates="assets")
    asset: Mapped[Asset] = relationship(back_populates="output_assets")


class OutputPlace(Base):
    """输出中显式引用的地点及其顺序。"""

    __tablename__ = "output_place"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_output_place_sequence"),
        UniqueConstraint(
            "output_id", "place_id", "role", "sequence", name="uq_output_place_reference"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    output_id: Mapped[int] = mapped_column(
        ForeignKey("output.id", ondelete="CASCADE"), nullable=False, index=True
    )
    place_id: Mapped[int] = mapped_column(
        ForeignKey("place.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)

    output: Mapped[Output] = relationship(back_populates="places")
    place: Mapped[Place] = relationship(back_populates="output_places")


class AssetPlace(Base):
    """知识/素材资产与地点的可追溯关系。"""

    __tablename__ = "asset_place"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "place_id", "relation_type", name="uq_asset_place_relation"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    place_id: Mapped[int] = mapped_column(
        ForeignKey("place.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)

    asset: Mapped[Asset] = relationship(back_populates="asset_places")
    place: Mapped[Place] = relationship(back_populates="asset_places")


class Schedule(Base):
    """内容排期；取消状态承担软删除语义，历史发布记录不删除。"""

    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'published', 'collected', 'cancelled')",
            name="ck_schedule_status",
        ),
        UniqueConstraint(
            "blogger_id", "output_id", "plan_date", "platform", "content_type",
            name="uq_schedule_output_date_channel",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    output_id: Mapped[int] = mapped_column(
        ForeignKey("output.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="schedules")
    output: Mapped[Output] = relationship(back_populates="schedules")
    publish_events: Mapped[list[PublishEvent]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", order_by="PublishEvent.id"
    )
    reminder_events: Mapped[list[ReminderEvent]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", order_by="ReminderEvent.id"
    )
    metrics: Mapped[list[Metric]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", order_by="Metric.id"
    )
    collection_jobs: Mapped[list[CollectionJob]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", order_by="CollectionJob.id"
    )


class PublishEvent(Base):
    """模拟发布事件；不代表真实平台发布。"""

    __tablename__ = "publish_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'published', 'failed', 'cancelled')",
            name="ck_publish_event_status",
        ),
        UniqueConstraint("schedule_id", "idempotency_key", name="uq_publish_schedule_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    schedule: Mapped[Schedule] = relationship(back_populates="publish_events")


class ReminderEvent(Base):
    """排期提醒事件；同一排期同一天通过唯一约束去重。"""

    __tablename__ = "reminder_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'cancelled')",
            name="ck_reminder_event_status",
        ),
        UniqueConstraint("schedule_id", "reminder_date", name="uq_reminder_schedule_date"),
        UniqueConstraint("dedupe_key", name="uq_reminder_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reminder_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    schedule: Mapped[Schedule] = relationship(back_populates="reminder_events")


class Metric(Base):
    """手工或模拟回收的原始指标，不在本阶段做反馈判断。"""

    __tablename__ = "metric"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('manual', 'simulated')",
            name="ck_metric_source_type",
        ),
        CheckConstraint("views >= 0", name="ck_metric_views_nonnegative"),
        CheckConstraint("likes >= 0", name="ck_metric_likes_nonnegative"),
        CheckConstraint("comments >= 0", name="ck_metric_comments_nonnegative"),
        CheckConstraint("collects >= 0", name="ck_metric_collects_nonnegative"),
        CheckConstraint("shares >= 0", name="ck_metric_shares_nonnegative"),
        CheckConstraint(
            "actual_revenue IS NULL OR actual_revenue >= 0",
            name="ck_metric_actual_revenue_nonnegative",
        ),
        CheckConstraint(
            "actual_cost IS NULL OR actual_cost >= 0",
            name="ck_metric_actual_cost_nonnegative",
        ),
        CheckConstraint(
            "user_confirmed = 0 OR source_type = 'manual'",
            name="ck_metric_confirmation_manual_only",
        ),
        CheckConstraint(
            "(actual_revenue IS NULL AND actual_cost IS NULL) "
            "OR (source_type = 'manual' AND user_confirmed = 1)",
            name="ck_metric_actual_values_confirmed_manual",
        ),
        UniqueConstraint(
            "schedule_id",
            "idempotency_key",
            name="uq_metric_schedule_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    output_id: Mapped[int] = mapped_column(
        ForeignKey("output.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_revenue: Mapped[float | None] = mapped_column(Float)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    output: Mapped[Output] = relationship(back_populates="metrics")
    schedule: Mapped[Schedule] = relationship(back_populates="metrics")
    feedback_runs: Mapped[list[FeedbackRun]] = relationship(back_populates="primary_metric")


class CollectionJob(Base):
    """一次排期指标回收任务。"""

    __tablename__ = "collection_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_collection_job_status",
        ),
        UniqueConstraint("schedule_id", "idempotency_key", name="uq_collection_schedule_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    schedule: Mapped[Schedule] = relationship(back_populates="collection_jobs")


class FeedbackRun(Base):
    """一次反馈分析及其冻结输入，候选确认前不改变任何业务数据。"""

    __tablename__ = "feedback_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'analyzed', 'applied', 'rejected', 'failed')",
            name="ck_feedback_run_status",
        ),
        UniqueConstraint(
            "blogger_id",
            "idempotency_key",
            name="uq_feedback_run_blogger_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    output_id: Mapped[int] = mapped_column(
        ForeignKey("output.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    primary_metric_id: Mapped[int] = mapped_column(
        ForeignKey("metric.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_session.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    analysis_json: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False, default="phase4-feedback-v1")
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, default="deepseek-v4-flash")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)

    blogger: Mapped[Blogger] = relationship(back_populates="feedback_runs")
    output: Mapped[Output] = relationship(back_populates="feedback_runs")
    primary_metric: Mapped[Metric] = relationship(back_populates="feedback_runs")
    task: Mapped[TaskSession | None] = relationship(back_populates="feedback_runs")
    evidences: Mapped[list[FeedbackEvidence]] = relationship(
        back_populates="feedback_run", cascade="all, delete-orphan", order_by="FeedbackEvidence.id"
    )
    profile_revisions: Mapped[list[ProfileFeedbackRevision]] = relationship(
        back_populates="feedback_run",
        cascade="all, delete-orphan",
        order_by="ProfileFeedbackRevision.id",
    )
    asset_revisions: Mapped[list[AssetEffectRevision]] = relationship(
        back_populates="feedback_run",
        cascade="all, delete-orphan",
        order_by="AssetEffectRevision.id",
    )
    place_revisions: Mapped[list[PlaceCommercialRevision]] = relationship(
        back_populates="feedback_run",
        cascade="all, delete-orphan",
        order_by="PlaceCommercialRevision.id",
    )
    library_revisions: Mapped[list[LibraryEvolutionRevision]] = relationship(
        back_populates="feedback_run",
        cascade="all, delete-orphan",
        order_by="LibraryEvolutionRevision.id",
    )
    indicator_observations: Mapped[list[IndicatorObservation]] = relationship(
        back_populates="feedback_run"
    )


class FeedbackEvidence(Base):
    """反馈结论引用的冻结证据；ref_id 由 evidence_type 解释。"""

    __tablename__ = "feedback_evidence"
    __table_args__ = (
        CheckConstraint(
            "evidence_type IN ('metric', 'output', 'asset', 'place', "
            "'output_asset', 'output_place', 'decision')",
            name="ck_feedback_evidence_type",
        ),
        UniqueConstraint(
            "feedback_run_id",
            "evidence_type",
            "ref_id",
            name="uq_feedback_evidence_reference",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_run_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    feedback_run: Mapped[FeedbackRun] = relationship(back_populates="evidences")


class ProfileFeedbackRevision(Base):
    """画像字段的候选与确认历史。"""

    __tablename__ = "profile_feedback_revision"
    __table_args__ = (
        CheckConstraint(
            "field_name IN ('suit_type', 'knowledge_focus')",
            name="ck_profile_feedback_revision_field",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_profile_feedback_revision_status",
        ),
        CheckConstraint("version > 0", name="ck_profile_feedback_revision_version"),
        UniqueConstraint(
            "run_id",
            "blogger_id",
            "field_name",
            "version",
            name="uq_profile_feedback_revision_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)

    feedback_run: Mapped[FeedbackRun] = relationship(back_populates="profile_revisions")
    blogger: Mapped[Blogger] = relationship(back_populates="feedback_profile_revisions")


class AssetEffectRevision(Base):
    """资产有效性和权重的候选与确认历史，不存储或覆盖资产正文。"""

    __tablename__ = "asset_effect_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_asset_effect_revision_status",
        ),
        CheckConstraint("version > 0", name="ck_asset_effect_revision_version"),
        CheckConstraint(
            "before_weight IS NULL OR (before_weight >= 0 AND before_weight <= 1)",
            name="ck_asset_effect_revision_before_weight",
        ),
        CheckConstraint(
            "after_weight IS NULL OR (after_weight >= 0 AND after_weight <= 1)",
            name="ck_asset_effect_revision_after_weight",
        ),
        UniqueConstraint(
            "run_id", "asset_id", "version", name="uq_asset_effect_revision_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("asset.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    before_effect: Mapped[str | None] = mapped_column(String(50))
    after_effect: Mapped[str | None] = mapped_column(String(50))
    before_weight: Mapped[float | None] = mapped_column(Float)
    after_weight: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)

    feedback_run: Mapped[FeedbackRun] = relationship(back_populates="asset_revisions")
    asset: Mapped[Asset] = relationship(back_populates="effect_revisions")


class PlaceCommercialRevision(Base):
    """地点商业白名单字段的 JSON 候选与确认历史。"""

    __tablename__ = "place_commercial_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_place_commercial_revision_status",
        ),
        CheckConstraint("version > 0", name="ck_place_commercial_revision_version"),
        UniqueConstraint(
            "run_id", "place_id", "version", name="uq_place_commercial_revision_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    place_id: Mapped[int] = mapped_column(
        ForeignKey("place.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)

    feedback_run: Mapped[FeedbackRun] = relationship(back_populates="place_revisions")
    place: Mapped[Place] = relationship(back_populates="commercial_revisions")


class LibraryEvolutionRevision(Base):
    """知识、素材和算法三库的待确认进化候选。"""

    __tablename__ = "library_evolution_revision"
    __table_args__ = (
        CheckConstraint(
            "lib_type IN ('knowledge', 'material', 'algorithm')",
            name="ck_library_evolution_revision_lib_type",
        ),
        CheckConstraint(
            "action IN ('add', 'reinforce', 'review')",
            name="ck_library_evolution_revision_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_library_evolution_revision_status",
        ),
        CheckConstraint("version > 0", name="ck_library_evolution_revision_version"),
        UniqueConstraint(
            "run_id",
            "lib_type",
            "action",
            "candidate_json",
            name="uq_library_evolution_revision_candidate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lib_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    target_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("asset.id", ondelete="SET NULL"), nullable=True, index=True
    )
    candidate_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime)

    feedback_run: Mapped[FeedbackRun] = relationship(back_populates="library_revisions")
    target_asset: Mapped[Asset | None] = relationship()


class OperationalIndicator(Base):
    """绑定受控 formula_key 的经营指标定义。"""

    __tablename__ = "operational_indicator"
    __table_args__ = (
        CheckConstraint(
            "category IN ('money', 'traffic', 'product', 'supplier')",
            name="ck_operational_indicator_category",
        ),
        CheckConstraint(
            "direction IN ('higher_better', 'lower_better', 'neutral')",
            name="ck_operational_indicator_direction",
        ),
        CheckConstraint("version > 0", name="ck_operational_indicator_version"),
        UniqueConstraint(
            "blogger_id",
            "category",
            "name",
            name="uq_operational_indicator_blogger_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    formula_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_tables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(30), nullable=False, default="neutral")
    target_value: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="operational_indicators")
    observations: Mapped[list[IndicatorObservation]] = relationship(
        back_populates="indicator", passive_deletes=True, order_by="IndicatorObservation.observed_at"
    )


class IndicatorObservation(Base):
    """指标在某次反馈、报告或独立重算时形成的不可变观察。"""

    __tablename__ = "indicator_observation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'data_insufficient')",
            name="ck_indicator_observation_status",
        ),
        CheckConstraint(
            "trend IN ('up', 'down', 'flat', 'unknown')",
            name="ck_indicator_observation_trend",
        ),
        UniqueConstraint(
            "indicator_id", "feedback_run_id", name="uq_indicator_observation_feedback_run"
        ),
        UniqueConstraint(
            "indicator_id", "report_id", name="uq_indicator_observation_report"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator_id: Mapped[int] = mapped_column(
        ForeignKey("operational_indicator.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feedback_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("feedback_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("report.id", ondelete="SET NULL"), nullable=True, index=True
    )
    value: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    trend: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)

    indicator: Mapped[OperationalIndicator] = relationship(back_populates="observations")
    feedback_run: Mapped[FeedbackRun | None] = relationship(back_populates="indicator_observations")
    report: Mapped[Report | None] = relationship(back_populates="indicator_observations")


class Report(Base):
    """经营报告的确定性冻结快照及 Agent 受校验解释。"""

    __tablename__ = "report"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_report_status",
        ),
        UniqueConstraint(
            "blogger_id", "idempotency_key", name="uq_report_blogger_idempotency"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    blogger_id: Mapped[int] = mapped_column(
        ForeignKey("blogger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_session.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conclusion_json: Mapped[str | None] = mapped_column(Text)
    charts_json: Mapped[str | None] = mapped_column(Text)
    suggestions_json: Mapped[str | None] = mapped_column(Text)
    data_quality_json: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False, default="phase4-report-v1")
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, default="deepseek-v4-flash")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    blogger: Mapped[Blogger] = relationship(back_populates="reports")
    task: Mapped[TaskSession | None] = relationship(back_populates="reports")
    evidences: Mapped[list[ReportEvidence]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="ReportEvidence.id"
    )
    indicator_observations: Mapped[list[IndicatorObservation]] = relationship(
        back_populates="report"
    )


class ReportEvidence(Base):
    """报告事实与图表引用的冻结证据。"""

    __tablename__ = "report_evidence"
    __table_args__ = (
        CheckConstraint(
            "evidence_type IN ('metric', 'output', 'place', 'indicator', 'feedback_run')",
            name="ck_report_evidence_type",
        ),
        UniqueConstraint(
            "report_id", "evidence_type", "ref_id", name="uq_report_evidence_reference"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("report.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    report: Mapped[Report] = relationship(back_populates="evidences")

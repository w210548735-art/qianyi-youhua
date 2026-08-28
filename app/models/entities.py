from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    profile_state: Mapped[str] = mapped_column(String(30), nullable=False, default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    assets: Mapped[list[Asset]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    places: Mapped[list[Place]] = relationship(back_populates="blogger", cascade="all, delete-orphan")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="blogger", cascade="all, delete-orphan")


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
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision_log.id", ondelete="SET NULL"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    blogger: Mapped[Blogger] = relationship(back_populates="assets")
    embedding: Mapped[AssetEmbedding | None] = relationship(
        back_populates="asset", cascade="all, delete-orphan", uselist=False
    )
    sources: Mapped[list[AssetSource]] = relationship(cascade="all, delete-orphan")


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

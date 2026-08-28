"""第二阶段体检、指标快照与证据关联结构。

该迁移明确衔接第一阶段 ``0002_phase1_closure``。应用启动时历史上可能先由
SQLAlchemy ``create_all`` 预建新表，因此升级过程会采用“已有表校验、缺失表创建”
的幂等策略；迁移本身不依赖运行时 ``Base.metadata``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phase2_assessment"
down_revision = "0002_phase1_closure"
branch_labels = None
depends_on = None


_ASSESSMENT_COLUMNS = {
    "id",
    "blogger_id",
    "task_id",
    "status",
    "idempotency_key",
    "snapshot_hash",
    "input_snapshot_json",
    "library_analysis_json",
    "feature_readiness_json",
    "suggestions_json",
    "summary",
    "overall_score",
    "decision_id",
    "prompt_version",
    "model_name",
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
    "created_at",
}
_INDICATOR_COLUMNS = {
    "id",
    "assessment_id",
    "ordinal",
    "name",
    "meaning",
    "score_logic",
    "business_meaning",
    "weight",
    "weight_reason",
    "score",
    "reason",
    "evidence_json",
    "created_at",
}
_EVIDENCE_COLUMNS = {
    "id",
    "assessment_id",
    "indicator_id",
    "evidence_type",
    "asset_id",
    "source_document_id",
    "claim",
    "created_at",
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _ensure_table_columns(bind: sa.Connection, table_name: str, required: set[str]) -> None:
    missing = required - _column_names(bind, table_name)
    if missing:
        raise RuntimeError(f"{table_name.upper()}_SCHEMA_INCOMPLETE:{','.join(sorted(missing))}")


def _ensure_indexes(bind: sa.Connection, table_name: str, indexes: tuple[tuple[str, list[str]], ...]) -> None:
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    for index_name, columns in indexes:
        if index_name not in existing:
            op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    """创建体检结果、不可变指标快照和证据关联表。"""

    bind = op.get_bind()
    tables = _table_names(bind)

    if "assessment" not in tables:
        op.create_table(
            "assessment",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
            sa.Column("input_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("library_analysis_json", sa.Text(), nullable=True),
            sa.Column("feature_readiness_json", sa.Text(), nullable=True),
            sa.Column("suggestions_json", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("overall_score", sa.Float(), nullable=True),
            sa.Column("decision_id", sa.Integer(), nullable=True),
            sa.Column("prompt_version", sa.String(length=50), nullable=False, server_default="phase2-v1"),
            sa.Column("model_name", sa.String(length=200), nullable=False, server_default="deepseek-v4-flash"),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'succeeded', 'failed')",
                name="ck_assessment_status",
            ),
            sa.CheckConstraint(
                "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)",
                name="ck_assessment_overall_score",
            ),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["decision_id"], ["decision_log.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("blogger_id", "idempotency_key", name="uq_assessment_blogger_idempotency"),
        )
    else:
        _ensure_table_columns(bind, "assessment", _ASSESSMENT_COLUMNS)

    # 先建父表，再建指标和证据表，保证外键在空库上可创建。
    bind = op.get_bind()
    tables = _table_names(bind)
    if "assessment_indicator" not in tables:
        op.create_table(
            "assessment_indicator",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("assessment_id", sa.Integer(), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("meaning", sa.Text(), nullable=False),
            sa.Column("score_logic", sa.Text(), nullable=False),
            sa.Column("business_meaning", sa.Text(), nullable=False),
            sa.Column("weight", sa.Float(), nullable=False),
            sa.Column("weight_reason", sa.Text(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("ordinal > 0", name="ck_assessment_indicator_ordinal"),
            sa.CheckConstraint("weight > 0 AND weight <= 100", name="ck_assessment_indicator_weight"),
            sa.CheckConstraint(
                "score IS NULL OR (score >= 0 AND score <= 100)",
                name="ck_assessment_indicator_score",
            ),
            sa.ForeignKeyConstraint(["assessment_id"], ["assessment.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("assessment_id", "ordinal", name="uq_assessment_indicator_ordinal"),
        )
    else:
        _ensure_table_columns(bind, "assessment_indicator", _INDICATOR_COLUMNS)

    bind = op.get_bind()
    tables = _table_names(bind)
    if "assessment_evidence" not in tables:
        op.create_table(
            "assessment_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("assessment_id", sa.Integer(), nullable=False),
            sa.Column("indicator_id", sa.Integer(), nullable=False),
            sa.Column("evidence_type", sa.String(length=50), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=True),
            sa.Column("source_document_id", sa.Integer(), nullable=True),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["assessment_id"], ["assessment.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["indicator_id"], ["assessment_indicator.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_document_id"], ["source_document.id"], ondelete="SET NULL"),
        )
    else:
        _ensure_table_columns(bind, "assessment_evidence", _EVIDENCE_COLUMNS)

    bind = op.get_bind()
    _ensure_indexes(
        bind,
        "assessment",
        (
            ("ix_assessment_blogger_id", ["blogger_id"]),
            ("ix_assessment_task_id", ["task_id"]),
            ("ix_assessment_status", ["status"]),
            ("ix_assessment_snapshot_hash", ["snapshot_hash"]),
            ("ix_assessment_decision_id", ["decision_id"]),
        ),
    )
    _ensure_indexes(
        bind,
        "assessment_indicator",
        (("ix_assessment_indicator_assessment_id", ["assessment_id"]),),
    )
    _ensure_indexes(
        bind,
        "assessment_evidence",
        (
            ("ix_assessment_evidence_assessment_id", ["assessment_id"]),
            ("ix_assessment_evidence_indicator_id", ["indicator_id"]),
            ("ix_assessment_evidence_asset_id", ["asset_id"]),
            ("ix_assessment_evidence_source_document_id", ["source_document_id"]),
        ),
    )


def downgrade() -> None:
    """删除第二阶段表，保留 0002 的全部业务数据。"""

    bind = op.get_bind()
    tables = _table_names(bind)
    for table_name, indexes in (
        (
            "assessment_evidence",
            (
                "ix_assessment_evidence_source_document_id",
                "ix_assessment_evidence_asset_id",
                "ix_assessment_evidence_indicator_id",
                "ix_assessment_evidence_assessment_id",
            ),
        ),
        ("assessment_indicator", ("ix_assessment_indicator_assessment_id",)),
        (
            "assessment",
            (
                "ix_assessment_decision_id",
                "ix_assessment_snapshot_hash",
                "ix_assessment_status",
                "ix_assessment_task_id",
                "ix_assessment_blogger_id",
            ),
        ),
    ):
        if table_name not in tables:
            continue
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
        for index_name in indexes:
            if index_name in existing:
                op.drop_index(index_name, table_name=table_name)
        op.drop_table(table_name)

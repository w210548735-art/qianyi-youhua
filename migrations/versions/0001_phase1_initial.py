"""第一阶段初始结构（冻结于 4941d77）。"""

import sqlalchemy as sa
from alembic import op

revision = "0001_phase1_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """使用显式 DDL 创建 4941d77 时已经存在的全部表。"""
    op.create_table(
        "blogger",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("content_types_json", sa.Text(), nullable=False),
        sa.Column("style", sa.String(length=50), nullable=False),
        sa.Column("follower_band", sa.String(length=50), nullable=False),
        sa.Column("monetization_types_json", sa.Text(), nullable=False),
        sa.Column("routes", sa.Text(), nullable=True),
        sa.Column("viral_topic", sa.Text(), nullable=True),
        sa.Column("frequency", sa.String(length=50), nullable=True),
        sa.Column("suit_type", sa.Text(), nullable=True),
        sa.Column("profile_state", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "conversation_session",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_question", sa.String(length=50), nullable=False),
        sa.Column("collected_profile_json", sa.Text(), nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "conversation_message",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_session.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "source_document",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("verified_at", sa.String(length=20), nullable=False),
        sa.Column("content_excerpt", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True),
    )
    op.create_table(
        "build_run",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False, unique=True),
        sa.Column("input_snapshot", sa.Text(), nullable=False),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "decision_log",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=True),
        sa.Column("build_run_id", sa.Integer(), nullable=True),
        sa.Column("decision_type", sa.String(length=50), nullable=False),
        sa.Column("prompt_version", sa.String(length=30), nullable=False),
        sa.Column("input_summary", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_run_id"], ["build_run.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "asset",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=False),
        sa.Column("lib_type", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("credibility", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("manual_locked", sa.Boolean(), nullable=False),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("credibility >= 0 AND credibility <= 5", name="ck_asset_credibility"),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["decision_log.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("blogger_id", "dedupe_key", name="uq_asset_blogger_dedupe"),
    )
    op.create_table(
        "asset_source",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_document.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("asset_id", "source_document_id"),
    )
    op.create_table(
        "asset_embedding",
        sa.Column("asset_id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("vector_norm", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "memory_record",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=False),
        sa.Column("memory_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_memory_id", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_memory_id"], ["memory_record.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "blogger_id",
            "memory_type",
            "content_hash",
            "version",
            name="uq_memory_version",
        ),
    )
    op.create_table(
        "memory_embedding",
        sa.Column("memory_id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("vector_norm", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memory_record.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "task_session",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_context", sa.Text(), nullable=False),
        sa.Column("recovery_state_json", sa.Text(), nullable=False),
        sa.Column("task_dir", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "session_message",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_message_sequence"),
    )
    op.create_table(
        "task_checkpoint",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_checkpoint_sequence"),
    )
    op.create_table(
        "task_artifact",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="CASCADE"),
    )

    op.create_index("ix_conversation_message_session_id", "conversation_message", ["session_id"])
    op.create_index("ix_build_run_blogger_id", "build_run", ["blogger_id"])
    op.create_index("ix_decision_log_blogger_id", "decision_log", ["blogger_id"])
    op.create_index("ix_decision_log_build_run_id", "decision_log", ["build_run_id"])
    op.create_index("ix_asset_blogger_id", "asset", ["blogger_id"])
    op.create_index("ix_asset_lib_type", "asset", ["lib_type"])
    op.create_index("ix_asset_category", "asset", ["category"])
    op.create_index("ix_asset_source_asset_id", "asset_source", ["asset_id"])
    op.create_index("ix_asset_source_source_document_id", "asset_source", ["source_document_id"])
    op.create_index("ix_memory_record_blogger_id", "memory_record", ["blogger_id"])
    op.create_index("ix_memory_record_memory_type", "memory_record", ["memory_type"])
    op.create_index("ix_memory_record_status", "memory_record", ["status"])
    op.create_index("ix_task_session_blogger_id", "task_session", ["blogger_id"])
    op.create_index("ix_task_session_status", "task_session", ["status"])
    op.create_index("ix_session_message_task_id", "session_message", ["task_id"])
    op.create_index("ix_task_checkpoint_task_id", "task_checkpoint", ["task_id"])
    op.create_index("ix_task_artifact_task_id", "task_artifact", ["task_id"])


def downgrade() -> None:
    """删除初始结构。"""
    for index_name, table_name in (
        ("ix_task_artifact_task_id", "task_artifact"),
        ("ix_task_checkpoint_task_id", "task_checkpoint"),
        ("ix_session_message_task_id", "session_message"),
        ("ix_task_session_status", "task_session"),
        ("ix_task_session_blogger_id", "task_session"),
        ("ix_memory_record_status", "memory_record"),
        ("ix_memory_record_memory_type", "memory_record"),
        ("ix_memory_record_blogger_id", "memory_record"),
        ("ix_asset_source_source_document_id", "asset_source"),
        ("ix_asset_source_asset_id", "asset_source"),
        ("ix_asset_category", "asset"),
        ("ix_asset_lib_type", "asset"),
        ("ix_asset_blogger_id", "asset"),
        ("ix_decision_log_build_run_id", "decision_log"),
        ("ix_decision_log_blogger_id", "decision_log"),
        ("ix_build_run_blogger_id", "build_run"),
        ("ix_conversation_message_session_id", "conversation_message"),
    ):
        op.drop_index(index_name, table_name=table_name)

    for table_name in (
        "task_artifact",
        "task_checkpoint",
        "session_message",
        "task_session",
        "memory_embedding",
        "memory_record",
        "asset_embedding",
        "asset_source",
        "asset",
        "decision_log",
        "build_run",
        "conversation_message",
        "conversation_session",
        "source_document",
        "blogger",
    ):
        op.drop_table(table_name)

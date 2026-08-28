"""第三阶段内容产出、排期、模拟发布与指标回收结构。

本迁移明确衔接 ``0003_phase2_assessment``，使用显式 Alembic DDL。应用旧版本
曾可能在 Alembic 迁移前通过 SQLAlchemy ``create_all`` 预建表，因此升级时对
已存在的同名表执行结构校验并补齐缺失索引，但不依赖运行时 ORM metadata。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_phase3_output"
down_revision = "0003_phase2_assessment"
branch_labels = None
depends_on = None


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _ensure_table_columns(bind: sa.Connection, table_name: str, required: set[str]) -> None:
    missing = required - _column_names(bind, table_name)
    if missing:
        raise RuntimeError(f"{table_name.upper()}_SCHEMA_INCOMPLETE:{','.join(sorted(missing))}")


def _ensure_indexes(
    bind: sa.Connection,
    table_name: str,
    indexes: tuple[tuple[str, list[str]], ...],
) -> None:
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    for index_name, columns in indexes:
        if index_name not in existing:
            op.create_index(index_name, table_name, columns)


_TABLE_COLUMNS: dict[str, set[str]] = {
    "output": {
        "id",
        "blogger_id",
        "task_id",
        "idempotency_key",
        "type",
        "category",
        "title",
        "content_json",
        "status",
        "assessment_id",
        "parent_output_id",
        "version",
        "manual_locked",
        "decision_id",
        "prompt_version",
        "model_name",
        "error_code",
        "error_message",
        "deleted_at",
        "created_at",
        "updated_at",
    },
    "output_asset": {"id", "output_id", "asset_id", "usage_type", "claim"},
    "output_place": {"id", "output_id", "place_id", "role", "sequence", "claim"},
    "asset_place": {"id", "asset_id", "place_id", "relation_type", "source_type"},
    "schedule": {
        "id",
        "blogger_id",
        "output_id",
        "plan_date",
        "platform",
        "content_type",
        "title",
        "status",
        "publish_time",
        "created_at",
        "updated_at",
    },
    "publish_event": {
        "id",
        "schedule_id",
        "status",
        "idempotency_key",
        "published_at",
        "error_code",
        "error_message",
        "created_at",
    },
    "reminder_event": {"id", "schedule_id", "reminder_date", "status", "dedupe_key", "created_at"},
    "metric": {
        "id",
        "output_id",
        "schedule_id",
        "source_type",
        "views",
        "likes",
        "comments",
        "collects",
        "idempotency_key",
        "collected_at",
        "created_at",
    },
    "collection_job": {
        "id",
        "schedule_id",
        "status",
        "idempotency_key",
        "result_json",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
    },
}


def upgrade() -> None:
    """创建第三阶段数据表，允许兼容运行时预建表。"""

    bind = op.get_bind()
    tables = _table_names(bind)

    if "output" not in tables:
        op.create_table(
            "output",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=True),
            sa.Column("idempotency_key", sa.String(length=100), nullable=True),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("category", sa.String(length=100), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("assessment_id", sa.Integer(), nullable=True),
            sa.Column("parent_output_id", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("manual_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("decision_id", sa.Integer(), nullable=True),
            sa.Column("prompt_version", sa.String(length=50), nullable=False, server_default="phase3-v1"),
            sa.Column("model_name", sa.String(length=200), nullable=False, server_default="deepseek-v4-flash"),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "type IN ('script', 'storyboard', 'route_rec')",
                name="ck_output_type",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'succeeded', 'failed', 'draft', 'deleted')",
                name="ck_output_status",
            ),
            sa.CheckConstraint("version > 0", name="ck_output_version"),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["assessment_id"], ["assessment.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["parent_output_id"], ["output.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["decision_id"], ["decision_log.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("blogger_id", "parent_output_id", "version", name="uq_output_version"),
            sa.UniqueConstraint("blogger_id", "idempotency_key", name="uq_output_blogger_idempotency"),
        )
    else:
        _ensure_table_columns(bind, "output", _TABLE_COLUMNS["output"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "output_asset" not in tables:
        op.create_table(
            "output_asset",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("output_id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("usage_type", sa.String(length=50), nullable=False),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["output_id"], ["output.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("output_id", "asset_id", "usage_type", name="uq_output_asset_usage"),
        )
    else:
        _ensure_table_columns(bind, "output_asset", _TABLE_COLUMNS["output_asset"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "output_place" not in tables:
        op.create_table(
            "output_place",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("output_id", sa.Integer(), nullable=False),
            sa.Column("place_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=50), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.CheckConstraint("sequence > 0", name="ck_output_place_sequence"),
            sa.ForeignKeyConstraint(["output_id"], ["output.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["place_id"], ["place.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "output_id",
                "place_id",
                "role",
                "sequence",
                name="uq_output_place_reference",
            ),
        )
    else:
        _ensure_table_columns(bind, "output_place", _TABLE_COLUMNS["output_place"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "asset_place" not in tables:
        op.create_table(
            "asset_place",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("place_id", sa.Integer(), nullable=False),
            sa.Column("relation_type", sa.String(length=50), nullable=False),
            sa.Column("source_type", sa.String(length=50), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["place_id"], ["place.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("asset_id", "place_id", "relation_type", name="uq_asset_place_relation"),
        )
    else:
        _ensure_table_columns(bind, "asset_place", _TABLE_COLUMNS["asset_place"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "schedule" not in tables:
        op.create_table(
            "schedule",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("output_id", sa.Integer(), nullable=False),
            sa.Column("plan_date", sa.Date(), nullable=False),
            sa.Column("platform", sa.String(length=50), nullable=False),
            sa.Column("content_type", sa.String(length=50), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("publish_time", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'published', 'collected', 'cancelled')",
                name="ck_schedule_status",
            ),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["output_id"], ["output.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "blogger_id",
                "output_id",
                "plan_date",
                "platform",
                "content_type",
                name="uq_schedule_output_date_channel",
            ),
        )
    else:
        _ensure_table_columns(bind, "schedule", _TABLE_COLUMNS["schedule"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "publish_event" not in tables:
        op.create_table(
            "publish_event",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'published', 'failed', 'cancelled')",
                name="ck_publish_event_status",
            ),
            sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("schedule_id", "idempotency_key", name="uq_publish_schedule_idempotency"),
        )
    else:
        _ensure_table_columns(bind, "publish_event", _TABLE_COLUMNS["publish_event"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "reminder_event" not in tables:
        op.create_table(
            "reminder_event",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("reminder_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("dedupe_key", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'sent', 'failed', 'cancelled')",
                name="ck_reminder_event_status",
            ),
            sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("schedule_id", "reminder_date", name="uq_reminder_schedule_date"),
            sa.UniqueConstraint("dedupe_key", name="uq_reminder_dedupe_key"),
        )
    else:
        _ensure_table_columns(bind, "reminder_event", _TABLE_COLUMNS["reminder_event"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "metric" not in tables:
        op.create_table(
            "metric",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("output_id", sa.Integer(), nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=30), nullable=False),
            sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("collects", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("collected_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "source_type IN ('manual', 'simulated', 'platform')",
                name="ck_metric_source_type",
            ),
            sa.CheckConstraint("views >= 0", name="ck_metric_views_nonnegative"),
            sa.CheckConstraint("likes >= 0", name="ck_metric_likes_nonnegative"),
            sa.CheckConstraint("comments >= 0", name="ck_metric_comments_nonnegative"),
            sa.CheckConstraint("collects >= 0", name="ck_metric_collects_nonnegative"),
            sa.ForeignKeyConstraint(["output_id"], ["output.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("idempotency_key", name="uq_metric_idempotency"),
        )
    else:
        _ensure_table_columns(bind, "metric", _TABLE_COLUMNS["metric"])

    bind = op.get_bind()
    tables = _table_names(bind)
    if "collection_job" not in tables:
        op.create_table(
            "collection_job",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'succeeded', 'failed')",
                name="ck_collection_job_status",
            ),
            sa.ForeignKeyConstraint(["schedule_id"], ["schedule.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("schedule_id", "idempotency_key", name="uq_collection_schedule_idempotency"),
        )
    else:
        _ensure_table_columns(bind, "collection_job", _TABLE_COLUMNS["collection_job"])

    bind = op.get_bind()
    _ensure_indexes(
        bind,
        "output",
        (
            ("ix_output_blogger_id", ["blogger_id"]),
            ("ix_output_task_id", ["task_id"]),
            ("ix_output_idempotency_key", ["idempotency_key"]),
            ("ix_output_type", ["type"]),
            ("ix_output_category", ["category"]),
            ("ix_output_status", ["status"]),
            ("ix_output_assessment_id", ["assessment_id"]),
            ("ix_output_parent_output_id", ["parent_output_id"]),
            ("ix_output_decision_id", ["decision_id"]),
            ("ix_output_deleted_at", ["deleted_at"]),
        ),
    )
    _ensure_indexes(
        bind,
        "output_asset",
        (("ix_output_asset_output_id", ["output_id"]), ("ix_output_asset_asset_id", ["asset_id"])),
    )
    _ensure_indexes(
        bind,
        "output_place",
        (("ix_output_place_output_id", ["output_id"]), ("ix_output_place_place_id", ["place_id"])),
    )
    _ensure_indexes(
        bind,
        "asset_place",
        (("ix_asset_place_asset_id", ["asset_id"]), ("ix_asset_place_place_id", ["place_id"])),
    )
    _ensure_indexes(
        bind,
        "schedule",
        (
            ("ix_schedule_blogger_id", ["blogger_id"]),
            ("ix_schedule_output_id", ["output_id"]),
            ("ix_schedule_plan_date", ["plan_date"]),
            ("ix_schedule_status", ["status"]),
        ),
    )
    _ensure_indexes(
        bind,
        "publish_event",
        (("ix_publish_event_schedule_id", ["schedule_id"]), ("ix_publish_event_status", ["status"])),
    )
    _ensure_indexes(
        bind,
        "reminder_event",
        (
            ("ix_reminder_event_schedule_id", ["schedule_id"]),
            ("ix_reminder_event_reminder_date", ["reminder_date"]),
            ("ix_reminder_event_status", ["status"]),
        ),
    )
    _ensure_indexes(
        bind,
        "metric",
        (
            ("ix_metric_output_id", ["output_id"]),
            ("ix_metric_schedule_id", ["schedule_id"]),
            ("ix_metric_source_type", ["source_type"]),
        ),
    )
    _ensure_indexes(
        bind,
        "collection_job",
        (("ix_collection_job_schedule_id", ["schedule_id"]), ("ix_collection_job_status", ["status"])),
    )


def downgrade() -> None:
    """删除第三阶段表，保留 0003 及更早版本全部业务数据。"""

    bind = op.get_bind()
    tables = _table_names(bind)
    indexes_by_table = {
        "collection_job": (
            "ix_collection_job_status",
            "ix_collection_job_schedule_id",
        ),
        "metric": (
            "ix_metric_source_type",
            "ix_metric_schedule_id",
            "ix_metric_output_id",
        ),
        "reminder_event": (
            "ix_reminder_event_status",
            "ix_reminder_event_reminder_date",
            "ix_reminder_event_schedule_id",
        ),
        "publish_event": (
            "ix_publish_event_status",
            "ix_publish_event_schedule_id",
        ),
        "schedule": (
            "ix_schedule_status",
            "ix_schedule_plan_date",
            "ix_schedule_output_id",
            "ix_schedule_blogger_id",
        ),
        "asset_place": (
            "ix_asset_place_place_id",
            "ix_asset_place_asset_id",
        ),
        "output_place": (
            "ix_output_place_place_id",
            "ix_output_place_output_id",
        ),
        "output_asset": (
            "ix_output_asset_asset_id",
            "ix_output_asset_output_id",
        ),
        "output": (
            "ix_output_deleted_at",
            "ix_output_decision_id",
            "ix_output_parent_output_id",
            "ix_output_assessment_id",
            "ix_output_status",
            "ix_output_category",
            "ix_output_type",
            "ix_output_idempotency_key",
            "ix_output_task_id",
            "ix_output_blogger_id",
        ),
    }
    for table_name, index_names in indexes_by_table.items():
        if table_name not in tables:
            continue
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
        for index_name in index_names:
            if index_name in existing:
                op.drop_index(index_name, table_name=table_name)

    for table_name in (
        "collection_job",
        "metric",
        "reminder_event",
        "publish_event",
        "schedule",
        "asset_place",
        "output_place",
        "output_asset",
        "output",
    ):
        if table_name in tables:
            op.drop_table(table_name)

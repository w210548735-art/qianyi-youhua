"""第四阶段反馈闭环、经营指标与不可变报告结构。

Revision ID: 0006_phase4_feedback
Revises: 0005_phase3_metric_contract_fix

迁移先显式扩展第三阶段已有表，再创建第四阶段表。为兼容应用在 Alembic
升级前通过 SQLAlchemy ``create_all`` 预建最终新表的历史行为，已存在的新表会
接受列完整性校验并补齐索引；迁移不调用运行时 metadata。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_phase4_feedback"
down_revision = "0005_phase3_metric_contract_fix"
branch_labels = None
depends_on = None


PHASE4_TABLE_COLUMNS: dict[str, set[str]] = {
    "feedback_run": {
        "id", "blogger_id", "output_id", "primary_metric_id", "task_id", "status",
        "idempotency_key", "snapshot_json", "snapshot_hash", "analysis_json", "summary",
        "prompt_version", "model_name", "error_code", "error_message", "created_at",
        "updated_at", "applied_at", "rejected_at",
    },
    "feedback_evidence": {
        "id", "feedback_run_id", "evidence_type", "ref_id", "claim", "snapshot_json",
        "created_at",
    },
    "profile_feedback_revision": {
        "id", "run_id", "blogger_id", "field_name", "before", "after", "reason", "status",
        "version", "created_at", "updated_at", "confirmed_at", "applied_at", "rejected_at",
    },
    "asset_effect_revision": {
        "id", "run_id", "asset_id", "before_effect", "after_effect", "before_weight",
        "after_weight", "reason", "status", "version", "created_at", "updated_at",
        "confirmed_at", "applied_at", "rejected_at",
    },
    "place_commercial_revision": {
        "id", "run_id", "place_id", "before_json", "after_json", "reason", "status",
        "version", "created_at", "updated_at", "confirmed_at", "applied_at", "rejected_at",
    },
    "library_evolution_revision": {
        "id", "run_id", "lib_type", "action", "target_asset_id", "candidate_json", "reason",
        "status", "version", "created_at", "updated_at", "confirmed_at", "applied_at",
        "rejected_at",
    },
    "operational_indicator": {
        "id", "blogger_id", "category", "name", "meaning", "formula_key",
        "source_tables_json", "unit", "direction", "target_value", "active", "version",
        "created_at", "updated_at",
    },
    "indicator_observation": {
        "id", "indicator_id", "feedback_run_id", "report_id", "value", "status", "trend",
        "evidence_json", "observed_at",
    },
    "report": {
        "id", "blogger_id", "task_id", "status", "idempotency_key", "snapshot_json",
        "snapshot_hash", "conclusion_json", "charts_json", "suggestions_json",
        "data_quality_json", "prompt_version", "model_name", "error_code", "error_message",
        "created_at", "updated_at", "completed_at",
    },
    "report_evidence": {
        "id", "report_id", "evidence_type", "ref_id", "claim", "snapshot_json", "created_at",
    },
}


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _constraint_names(table_name: str, kind: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    rows = (
        inspector.get_check_constraints(table_name)
        if kind == "check"
        else inspector.get_unique_constraints(table_name)
    )
    return {str(row["name"]) for row in rows if row.get("name")}


def _ensure_table_columns(table_name: str) -> None:
    missing = PHASE4_TABLE_COLUMNS[table_name] - _column_names(table_name)
    if missing:
        raise RuntimeError(f"{table_name.upper()}_SCHEMA_INCOMPLETE:{','.join(sorted(missing))}")


def _ensure_indexes(table_name: str, indexes: tuple[tuple[str, list[str]], ...]) -> None:
    existing = {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }
    for index_name, columns in indexes:
        if index_name not in existing:
            op.create_index(index_name, table_name, columns)


def _extend_blogger() -> None:
    if "knowledge_focus" not in _column_names("blogger"):
        op.add_column("blogger", sa.Column("knowledge_focus", sa.Text(), nullable=True))


def _extend_asset() -> None:
    columns = _column_names("asset")
    checks = _constraint_names("asset", "check")
    if {"effect", "effect_weight"} <= columns and "ck_asset_effect_weight" in checks:
        return

    with op.batch_alter_table("asset", recreate="always") as batch_op:
        if "effect" not in columns:
            batch_op.add_column(sa.Column("effect", sa.String(length=50), nullable=True))
        if "effect_weight" not in columns:
            batch_op.add_column(sa.Column("effect_weight", sa.Float(), nullable=True))
        if "ck_asset_effect_weight" not in checks:
            batch_op.create_check_constraint(
                "ck_asset_effect_weight",
                "effect_weight IS NULL OR (effect_weight >= 0 AND effect_weight <= 1)",
            )


def _extend_metric() -> None:
    columns = _column_names("metric")
    checks = _constraint_names("metric", "check")
    expected_checks = {
        "ck_metric_shares_nonnegative",
        "ck_metric_actual_revenue_nonnegative",
        "ck_metric_actual_cost_nonnegative",
        "ck_metric_confirmation_manual_only",
        "ck_metric_actual_values_confirmed_manual",
    }
    if {
        "shares", "actual_revenue", "actual_cost", "user_confirmed"
    } <= columns and expected_checks <= checks:
        return

    with op.batch_alter_table("metric", recreate="always") as batch_op:
        if "shares" not in columns:
            batch_op.add_column(
                sa.Column("shares", sa.Integer(), nullable=False, server_default="0")
            )
        if "actual_revenue" not in columns:
            batch_op.add_column(sa.Column("actual_revenue", sa.Float(), nullable=True))
        if "actual_cost" not in columns:
            batch_op.add_column(sa.Column("actual_cost", sa.Float(), nullable=True))
        if "user_confirmed" not in columns:
            batch_op.add_column(
                sa.Column("user_confirmed", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "ck_metric_shares_nonnegative" not in checks:
            batch_op.create_check_constraint("ck_metric_shares_nonnegative", "shares >= 0")
        if "ck_metric_actual_revenue_nonnegative" not in checks:
            batch_op.create_check_constraint(
                "ck_metric_actual_revenue_nonnegative",
                "actual_revenue IS NULL OR actual_revenue >= 0",
            )
        if "ck_metric_actual_cost_nonnegative" not in checks:
            batch_op.create_check_constraint(
                "ck_metric_actual_cost_nonnegative",
                "actual_cost IS NULL OR actual_cost >= 0",
            )
        if "ck_metric_confirmation_manual_only" not in checks:
            batch_op.create_check_constraint(
                "ck_metric_confirmation_manual_only",
                "user_confirmed = 0 OR source_type = 'manual'",
            )
        if "ck_metric_actual_values_confirmed_manual" not in checks:
            batch_op.create_check_constraint(
                "ck_metric_actual_values_confirmed_manual",
                "(actual_revenue IS NULL AND actual_cost IS NULL) "
                "OR (source_type = 'manual' AND user_confirmed = 1)",
            )


def upgrade() -> None:
    """显式扩展旧表并创建反馈、指标和报告的第四阶段表。"""

    _extend_blogger()
    _extend_asset()
    _extend_metric()
    tables = _table_names()

    if "feedback_run" not in tables:
        op.create_table(
            "feedback_run",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("output_id", sa.Integer(), nullable=False),
            sa.Column("primary_metric_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
            sa.Column("analysis_json", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column(
                "prompt_version", sa.String(length=50), nullable=False,
                server_default="phase4-feedback-v1",
            ),
            sa.Column(
                "model_name", sa.String(length=200), nullable=False,
                server_default="deepseek-v4-flash",
            ),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'analyzed', 'applied', 'rejected', 'failed')",
                name="ck_feedback_run_status",
            ),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["output_id"], ["output.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["primary_metric_id"], ["metric.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="SET NULL"),
            sa.UniqueConstraint(
                "blogger_id", "idempotency_key", name="uq_feedback_run_blogger_idempotency"
            ),
        )
    else:
        _ensure_table_columns("feedback_run")

    tables = _table_names()
    if "feedback_evidence" not in tables:
        op.create_table(
            "feedback_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("feedback_run_id", sa.Integer(), nullable=False),
            sa.Column("evidence_type", sa.String(length=50), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=False),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "evidence_type IN ('metric', 'output', 'asset', 'place', "
                "'output_asset', 'output_place', 'decision')",
                name="ck_feedback_evidence_type",
            ),
            sa.ForeignKeyConstraint(["feedback_run_id"], ["feedback_run.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "feedback_run_id", "evidence_type", "ref_id",
                name="uq_feedback_evidence_reference",
            ),
        )
    else:
        _ensure_table_columns("feedback_evidence")

    tables = _table_names()
    if "profile_feedback_revision" not in tables:
        op.create_table(
            "profile_feedback_revision",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("field_name", sa.String(length=50), nullable=False),
            sa.Column("before", sa.Text(), nullable=True),
            sa.Column("after", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "field_name IN ('suit_type', 'knowledge_focus')",
                name="ck_profile_feedback_revision_field",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'applied', 'rejected')",
                name="ck_profile_feedback_revision_status",
            ),
            sa.CheckConstraint("version > 0", name="ck_profile_feedback_revision_version"),
            sa.ForeignKeyConstraint(["run_id"], ["feedback_run.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "run_id", "blogger_id", "field_name", "version",
                name="uq_profile_feedback_revision_version",
            ),
        )
    else:
        _ensure_table_columns("profile_feedback_revision")

    tables = _table_names()
    if "asset_effect_revision" not in tables:
        op.create_table(
            "asset_effect_revision",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("before_effect", sa.String(length=50), nullable=True),
            sa.Column("after_effect", sa.String(length=50), nullable=True),
            sa.Column("before_weight", sa.Float(), nullable=True),
            sa.Column("after_weight", sa.Float(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending', 'applied', 'rejected')",
                name="ck_asset_effect_revision_status",
            ),
            sa.CheckConstraint("version > 0", name="ck_asset_effect_revision_version"),
            sa.CheckConstraint(
                "before_weight IS NULL OR (before_weight >= 0 AND before_weight <= 1)",
                name="ck_asset_effect_revision_before_weight",
            ),
            sa.CheckConstraint(
                "after_weight IS NULL OR (after_weight >= 0 AND after_weight <= 1)",
                name="ck_asset_effect_revision_after_weight",
            ),
            sa.ForeignKeyConstraint(["run_id"], ["feedback_run.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "run_id", "asset_id", "version", name="uq_asset_effect_revision_version"
            ),
        )
    else:
        _ensure_table_columns("asset_effect_revision")

    tables = _table_names()
    if "place_commercial_revision" not in tables:
        op.create_table(
            "place_commercial_revision",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("place_id", sa.Integer(), nullable=False),
            sa.Column("before_json", sa.Text(), nullable=False),
            sa.Column("after_json", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending', 'applied', 'rejected')",
                name="ck_place_commercial_revision_status",
            ),
            sa.CheckConstraint("version > 0", name="ck_place_commercial_revision_version"),
            sa.ForeignKeyConstraint(["run_id"], ["feedback_run.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["place_id"], ["place.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "run_id", "place_id", "version", name="uq_place_commercial_revision_version"
            ),
        )
    else:
        _ensure_table_columns("place_commercial_revision")

    tables = _table_names()
    if "library_evolution_revision" not in tables:
        op.create_table(
            "library_evolution_revision",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("lib_type", sa.String(length=30), nullable=False),
            sa.Column("action", sa.String(length=30), nullable=False),
            sa.Column("target_asset_id", sa.Integer(), nullable=True),
            sa.Column("candidate_json", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "lib_type IN ('knowledge', 'material', 'algorithm')",
                name="ck_library_evolution_revision_lib_type",
            ),
            sa.CheckConstraint(
                "action IN ('add', 'reinforce', 'review')",
                name="ck_library_evolution_revision_action",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'applied', 'rejected')",
                name="ck_library_evolution_revision_status",
            ),
            sa.CheckConstraint("version > 0", name="ck_library_evolution_revision_version"),
            sa.ForeignKeyConstraint(["run_id"], ["feedback_run.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["target_asset_id"], ["asset.id"], ondelete="SET NULL"),
            sa.UniqueConstraint(
                "run_id", "lib_type", "action", "candidate_json",
                name="uq_library_evolution_revision_candidate",
            ),
        )
    else:
        _ensure_table_columns("library_evolution_revision")

    tables = _table_names()
    if "operational_indicator" not in tables:
        op.create_table(
            "operational_indicator",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("meaning", sa.Text(), nullable=False),
            sa.Column("formula_key", sa.String(length=100), nullable=False),
            sa.Column("source_tables_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("unit", sa.String(length=50), nullable=False),
            sa.Column("direction", sa.String(length=30), nullable=False, server_default="neutral"),
            sa.Column("target_value", sa.Float(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "category IN ('money', 'traffic', 'product', 'supplier')",
                name="ck_operational_indicator_category",
            ),
            sa.CheckConstraint(
                "direction IN ('higher_better', 'lower_better', 'neutral')",
                name="ck_operational_indicator_direction",
            ),
            sa.CheckConstraint("version > 0", name="ck_operational_indicator_version"),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "blogger_id", "category", "name",
                name="uq_operational_indicator_blogger_name",
            ),
        )
    else:
        _ensure_table_columns("operational_indicator")

    tables = _table_names()
    if "report" not in tables:
        op.create_table(
            "report",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blogger_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(length=100), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
            sa.Column("conclusion_json", sa.Text(), nullable=True),
            sa.Column("charts_json", sa.Text(), nullable=True),
            sa.Column("suggestions_json", sa.Text(), nullable=True),
            sa.Column("data_quality_json", sa.Text(), nullable=True),
            sa.Column(
                "prompt_version", sa.String(length=50), nullable=False,
                server_default="phase4-report-v1",
            ),
            sa.Column(
                "model_name", sa.String(length=200), nullable=False,
                server_default="deepseek-v4-flash",
            ),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'succeeded', 'failed')",
                name="ck_report_status",
            ),
            sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["task_id"], ["task_session.id"], ondelete="SET NULL"),
            sa.UniqueConstraint(
                "blogger_id", "idempotency_key", name="uq_report_blogger_idempotency"
            ),
        )
    else:
        _ensure_table_columns("report")

    tables = _table_names()
    if "indicator_observation" not in tables:
        op.create_table(
            "indicator_observation",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("indicator_id", sa.Integer(), nullable=False),
            sa.Column("feedback_run_id", sa.Integer(), nullable=True),
            sa.Column("report_id", sa.Integer(), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("trend", sa.String(length=30), nullable=False, server_default="unknown"),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('ok', 'data_insufficient')",
                name="ck_indicator_observation_status",
            ),
            sa.CheckConstraint(
                "trend IN ('up', 'down', 'flat', 'unknown')",
                name="ck_indicator_observation_trend",
            ),
            sa.ForeignKeyConstraint(
                ["indicator_id"], ["operational_indicator.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["feedback_run_id"], ["feedback_run.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="SET NULL"),
            sa.UniqueConstraint(
                "indicator_id", "feedback_run_id", name="uq_indicator_observation_feedback_run"
            ),
            sa.UniqueConstraint(
                "indicator_id", "report_id", name="uq_indicator_observation_report"
            ),
        )
    else:
        _ensure_table_columns("indicator_observation")

    tables = _table_names()
    if "report_evidence" not in tables:
        op.create_table(
            "report_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("report_id", sa.Integer(), nullable=False),
            sa.Column("evidence_type", sa.String(length=50), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=False),
            sa.Column("claim", sa.Text(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "evidence_type IN ('metric', 'output', 'place', 'indicator', 'feedback_run')",
                name="ck_report_evidence_type",
            ),
            sa.ForeignKeyConstraint(["report_id"], ["report.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "report_id", "evidence_type", "ref_id", name="uq_report_evidence_reference"
            ),
        )
    else:
        _ensure_table_columns("report_evidence")

    index_map: dict[str, tuple[tuple[str, list[str]], ...]] = {
        "feedback_run": (
            ("ix_feedback_run_blogger_id", ["blogger_id"]),
            ("ix_feedback_run_output_id", ["output_id"]),
            ("ix_feedback_run_primary_metric_id", ["primary_metric_id"]),
            ("ix_feedback_run_task_id", ["task_id"]),
            ("ix_feedback_run_status", ["status"]),
            ("ix_feedback_run_snapshot_hash", ["snapshot_hash"]),
        ),
        "feedback_evidence": (
            ("ix_feedback_evidence_feedback_run_id", ["feedback_run_id"]),
            ("ix_feedback_evidence_evidence_type", ["evidence_type"]),
        ),
        "profile_feedback_revision": (
            ("ix_profile_feedback_revision_run_id", ["run_id"]),
            ("ix_profile_feedback_revision_blogger_id", ["blogger_id"]),
            ("ix_profile_feedback_revision_field_name", ["field_name"]),
            ("ix_profile_feedback_revision_status", ["status"]),
        ),
        "asset_effect_revision": (
            ("ix_asset_effect_revision_run_id", ["run_id"]),
            ("ix_asset_effect_revision_asset_id", ["asset_id"]),
            ("ix_asset_effect_revision_status", ["status"]),
        ),
        "place_commercial_revision": (
            ("ix_place_commercial_revision_run_id", ["run_id"]),
            ("ix_place_commercial_revision_place_id", ["place_id"]),
            ("ix_place_commercial_revision_status", ["status"]),
        ),
        "library_evolution_revision": (
            ("ix_library_evolution_revision_run_id", ["run_id"]),
            ("ix_library_evolution_revision_lib_type", ["lib_type"]),
            ("ix_library_evolution_revision_action", ["action"]),
            ("ix_library_evolution_revision_target_asset_id", ["target_asset_id"]),
            ("ix_library_evolution_revision_status", ["status"]),
        ),
        "operational_indicator": (
            ("ix_operational_indicator_blogger_id", ["blogger_id"]),
            ("ix_operational_indicator_category", ["category"]),
            ("ix_operational_indicator_formula_key", ["formula_key"]),
            ("ix_operational_indicator_active", ["active"]),
        ),
        "indicator_observation": (
            ("ix_indicator_observation_indicator_id", ["indicator_id"]),
            ("ix_indicator_observation_feedback_run_id", ["feedback_run_id"]),
            ("ix_indicator_observation_report_id", ["report_id"]),
            ("ix_indicator_observation_status", ["status"]),
            ("ix_indicator_observation_observed_at", ["observed_at"]),
        ),
        "report": (
            ("ix_report_blogger_id", ["blogger_id"]),
            ("ix_report_task_id", ["task_id"]),
            ("ix_report_status", ["status"]),
            ("ix_report_snapshot_hash", ["snapshot_hash"]),
        ),
        "report_evidence": (
            ("ix_report_evidence_report_id", ["report_id"]),
            ("ix_report_evidence_evidence_type", ["evidence_type"]),
        ),
    }
    for table_name, indexes in index_map.items():
        _ensure_indexes(table_name, indexes)


def downgrade() -> None:
    """删除第四阶段表和扩展列，保留 0005 及之前全部业务数据。"""

    tables = _table_names()
    for table_name in (
        "report_evidence",
        "indicator_observation",
        "report",
        "operational_indicator",
        "library_evolution_revision",
        "place_commercial_revision",
        "asset_effect_revision",
        "profile_feedback_revision",
        "feedback_evidence",
        "feedback_run",
    ):
        if table_name in tables:
            op.drop_table(table_name)

    metric_columns = _column_names("metric")
    metric_checks = _constraint_names("metric", "check")
    phase4_metric_checks = (
        "ck_metric_actual_values_confirmed_manual",
        "ck_metric_confirmation_manual_only",
        "ck_metric_actual_cost_nonnegative",
        "ck_metric_actual_revenue_nonnegative",
        "ck_metric_shares_nonnegative",
    )
    with op.batch_alter_table("metric", recreate="always") as batch_op:
        for check_name in phase4_metric_checks:
            if check_name in metric_checks:
                batch_op.drop_constraint(check_name, type_="check")
        for column_name in ("user_confirmed", "actual_cost", "actual_revenue", "shares"):
            if column_name in metric_columns:
                batch_op.drop_column(column_name)

    asset_columns = _column_names("asset")
    asset_checks = _constraint_names("asset", "check")
    with op.batch_alter_table("asset", recreate="always") as batch_op:
        if "ck_asset_effect_weight" in asset_checks:
            batch_op.drop_constraint("ck_asset_effect_weight", type_="check")
        for column_name in ("effect_weight", "effect"):
            if column_name in asset_columns:
                batch_op.drop_column(column_name)

    if "knowledge_focus" in _column_names("blogger"):
        with op.batch_alter_table("blogger", recreate="always") as batch_op:
            batch_op.drop_column("knowledge_focus")

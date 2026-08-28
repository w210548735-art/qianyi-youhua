"""修正第三阶段 Metric 幂等范围与来源边界。

Revision ID: 0005_phase3_metric_contract_fix
Revises: 0004_phase3_output
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

revision = "0005_phase3_metric_contract_fix"
down_revision = "0004_phase3_output"
branch_labels = None
depends_on = None

OLD_UNIQUE = "uq_metric_idempotency"
NEW_UNIQUE = "uq_metric_schedule_idempotency"
SOURCE_CHECK = "ck_metric_source_type"


def _constraint_names(rows: Iterable[dict[str, object]]) -> set[str]:
    return {str(row["name"]) for row in rows if row.get("name")}


def _metric_contract() -> tuple[set[str], dict[str, str]]:
    inspector = sa.inspect(op.get_bind())
    uniques = _constraint_names(inspector.get_unique_constraints("metric"))
    checks = {
        str(row["name"]): str(row.get("sqltext") or "")
        for row in inspector.get_check_constraints("metric")
        if row.get("name")
    }
    return uniques, checks


def upgrade() -> None:
    uniques, checks = _metric_contract()
    source_sql = checks.get(SOURCE_CHECK, "").lower()
    if NEW_UNIQUE in uniques and "platform" not in source_sql:
        # 兼容应用先按最终 ORM create_all、Alembic 版本仍停在 0004 的数据库。
        return

    with op.batch_alter_table("metric", recreate="always") as batch_op:
        if OLD_UNIQUE in uniques:
            batch_op.drop_constraint(OLD_UNIQUE, type_="unique")
        if NEW_UNIQUE not in uniques:
            batch_op.create_unique_constraint(
                NEW_UNIQUE,
                ["schedule_id", "idempotency_key"],
            )
        if SOURCE_CHECK in checks:
            batch_op.drop_constraint(SOURCE_CHECK, type_="check")
        batch_op.create_check_constraint(
            SOURCE_CHECK,
            "source_type IN ('manual', 'simulated')",
        )


def downgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT idempotency_key
            FROM metric
            GROUP BY idempotency_key
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "无法安全降级：Metric 中已有跨排期复用的 idempotency_key；"
            "迁移拒绝删除或合并业务数据。"
        )

    uniques, checks = _metric_contract()
    source_sql = checks.get(SOURCE_CHECK, "").lower()
    if OLD_UNIQUE in uniques and "platform" in source_sql:
        return

    with op.batch_alter_table("metric", recreate="always") as batch_op:
        if NEW_UNIQUE in uniques:
            batch_op.drop_constraint(NEW_UNIQUE, type_="unique")
        if OLD_UNIQUE not in uniques:
            batch_op.create_unique_constraint(OLD_UNIQUE, ["idempotency_key"])
        if SOURCE_CHECK in checks:
            batch_op.drop_constraint(SOURCE_CHECK, type_="check")
        batch_op.create_check_constraint(
            SOURCE_CHECK,
            "source_type IN ('manual', 'simulated', 'platform')",
        )

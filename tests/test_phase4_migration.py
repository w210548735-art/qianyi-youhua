"""第四阶段 0006 数据结构、迁移兼容性与数据库约束验证。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.db.session import Base
from app.models import (
    Asset,
    AssetEffectRevision,
    Blogger,
    FeedbackEvidence,
    FeedbackRun,
    IndicatorObservation,
    LibraryEvolutionRevision,
    Metric,
    OperationalIndicator,
    PlaceCommercialRevision,
    ProfileFeedbackRevision,
    Report,
    ReportEvidence,
)

pytestmark = pytest.mark.migration

ROOT_DIR = Path(__file__).resolve().parents[1]
PHASE4_TABLES = {
    "feedback_run",
    "feedback_evidence",
    "profile_feedback_revision",
    "asset_effect_revision",
    "place_commercial_revision",
    "library_evolution_revision",
    "operational_indicator",
    "indicator_observation",
    "report",
    "report_evidence",
}


def migration_config(db_path: Path) -> Config:
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def upgrade(db_path: Path, revision: str = "head") -> None:
    command.upgrade(migration_config(db_path), revision)


def downgrade(db_path: Path, revision: str) -> None:
    command.downgrade(migration_config(db_path), revision)


def connection(db_path: Path) -> Connection:
    conn = create_engine(f"sqlite:///{db_path.as_posix()}").connect()
    conn.execute(text("PRAGMA foreign_keys=ON"))
    return conn


def insert_phase3_graph(db_path: Path) -> None:
    now = datetime.utcnow()
    with connection(db_path) as conn:
        conn.execute(
            text(
                """
                INSERT INTO blogger (
                    id, name, platform, content_types_json, style, follower_band,
                    monetization_types_json, routes, viral_topic, frequency, suit_type,
                    profile_state, created_at, updated_at
                ) VALUES (
                    41, '四期迁移博主', '抖音', '["美食"]', '口播', '1万-10万',
                    '["商单"]', '黔东南', NULL, '周更', NULL,
                    'complete', :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO asset (
                    id, blogger_id, lib_type, category, title, content, tags_json,
                    source_type, credibility, origin, dedupe_key, manual_locked,
                    created_at, updated_at
                ) VALUES (
                    401, 41, 'knowledge', '美食', '保留资产', '迁移不能覆盖正文', '[]',
                    'official', 5, 'seed', 'phase4-preserved-asset', 1, :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO place (
                    id, blogger_id, name, category, tags_json, source_type, credibility,
                    origin, manual_locked, dedupe_key, created_at, updated_at
                ) VALUES (
                    402, 41, '保留店铺', '餐饮', '[]', 'user_confirmed', 5,
                    'manual', 1, 'phase4-preserved-place', :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO output (
                    id, blogger_id, type, category, title, content_json, status,
                    version, manual_locked, prompt_version, model_name, created_at, updated_at
                ) VALUES (
                    403, 41, 'script', '美食', '保留输出', '{}', 'succeeded',
                    1, 0, 'phase3-v1', 'fake', :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO schedule (
                    id, blogger_id, output_id, plan_date, platform, content_type,
                    title, status, created_at, updated_at
                ) VALUES (
                    404, 41, 403, '2026-09-01', '抖音', '视频',
                    '保留排期', 'collected', :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO metric (
                    id, output_id, schedule_id, source_type, views, likes, comments,
                    collects, idempotency_key, collected_at, created_at
                ) VALUES (
                    405, 403, 404, 'manual', 100, 10, 2,
                    1, 'phase4-preserved-metric', :now, :now
                )
                """
            ),
            {"now": now},
        )
        conn.commit()


def assert_integrity_error(conn: Connection, sql: str, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(text(sql), params)
    conn.rollback()


def test_0006_is_explicit_and_has_single_0005_parent() -> None:
    source = (ROOT_DIR / "migrations/versions/0006_phase4_feedback.py").read_text(encoding="utf-8")

    assert 'revision = "0006_phase4_feedback"' in source
    assert 'down_revision = "0005_phase3_metric_contract_fix"' in source
    assert "Base.metadata.create_all" not in source
    assert "Base.metadata.drop_all" not in source
    assert source.count("op.create_table") >= len(PHASE4_TABLES)

    script = ScriptDirectory.from_config(migration_config(ROOT_DIR / "unused.db"))
    assert script.get_heads() == ["0006_phase4_feedback"]


def test_empty_database_upgrades_base_to_phase4_head(tmp_path: Path) -> None:
    db_path = tmp_path / "phase4-empty.db"
    upgrade(db_path)

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert PHASE4_TABLES <= set(inspector.get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006_phase4_feedback"

        assert "knowledge_focus" in {item["name"] for item in inspector.get_columns("blogger")}
        assert {"effect", "effect_weight"} <= {
            item["name"] for item in inspector.get_columns("asset")
        }
        assert {"shares", "actual_revenue", "actual_cost", "user_confirmed"} <= {
            item["name"] for item in inspector.get_columns("metric")
        }
        assert {
            "blogger_id",
            "output_id",
            "primary_metric_id",
            "task_id",
            "snapshot_json",
            "snapshot_hash",
            "analysis_json",
            "summary",
            "prompt_version",
            "model_name",
            "applied_at",
            "rejected_at",
        } <= {item["name"] for item in inspector.get_columns("feedback_run")}
        assert {
            "conclusion_json",
            "charts_json",
            "suggestions_json",
            "data_quality_json",
        } <= {item["name"] for item in inspector.get_columns("report")}


def test_0005_upgrade_preserves_existing_rows_and_null_semantics(tmp_path: Path) -> None:
    db_path = tmp_path / "phase4-preserve.db"
    upgrade(db_path, "0005_phase3_metric_contract_fix")
    insert_phase3_graph(db_path)

    upgrade(db_path)

    with connection(db_path) as conn:
        blogger = conn.execute(
            text("SELECT name, knowledge_focus FROM blogger WHERE id=41")
        ).one()
        asset = conn.execute(
            text("SELECT content, effect, effect_weight FROM asset WHERE id=401")
        ).one()
        metric = conn.execute(
            text(
                "SELECT views, shares, actual_revenue, actual_cost, user_confirmed "
                "FROM metric WHERE id=405"
            )
        ).one()

        assert blogger == ("四期迁移博主", None)
        assert asset == ("迁移不能覆盖正文", None, None)
        assert metric == (100, 0, None, None, 0)
        assert conn.scalar(text("SELECT name FROM place WHERE id=402")) == "保留店铺"


def test_downgrade_upgrade_round_trip_preserves_phase3_data(tmp_path: Path) -> None:
    db_path = tmp_path / "phase4-round-trip.db"
    upgrade(db_path, "0005_phase3_metric_contract_fix")
    insert_phase3_graph(db_path)
    upgrade(db_path)

    downgrade(db_path, "0005_phase3_metric_contract_fix")
    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert not PHASE4_TABLES.intersection(inspector.get_table_names())
        assert "knowledge_focus" not in {item["name"] for item in inspector.get_columns("blogger")}
        assert "effect" not in {item["name"] for item in inspector.get_columns("asset")}
        assert "shares" not in {item["name"] for item in inspector.get_columns("metric")}
        assert conn.scalar(text("SELECT name FROM blogger WHERE id=41")) == "四期迁移博主"
        assert conn.scalar(text("SELECT content FROM asset WHERE id=401")) == "迁移不能覆盖正文"
        assert conn.scalar(text("SELECT views FROM metric WHERE id=405")) == 100

    upgrade(db_path)
    with connection(db_path) as conn:
        assert PHASE4_TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT views FROM metric WHERE id=405")) == 100
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006_phase4_feedback"


def test_upgrade_adopts_runtime_precreated_phase4_tables(tmp_path: Path) -> None:
    """兼容应用先 create_all 新表、但旧表尚未增加 0006 列的数据库。"""

    db_path = tmp_path / "phase4-runtime-precreated.db"
    upgrade(db_path, "0005_phase3_metric_contract_fix")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    upgrade(db_path)
    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert PHASE4_TABLES <= set(inspector.get_table_names())
        assert "knowledge_focus" in {item["name"] for item in inspector.get_columns("blogger")}
        assert "effect_weight" in {item["name"] for item in inspector.get_columns("asset")}
        assert "actual_revenue" in {item["name"] for item in inspector.get_columns("metric")}
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006_phase4_feedback"


def test_database_enforces_phase4_status_range_idempotency_and_manual_money(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "phase4-constraints.db"
    upgrade(db_path)
    insert_phase3_graph(db_path)
    now = datetime.utcnow()

    with connection(db_path) as conn:
        assert_integrity_error(
            conn,
            "UPDATE asset SET effect_weight=1.01 WHERE id=401",
            {},
        )
        assert_integrity_error(
            conn,
            "UPDATE metric SET shares=-1 WHERE id=405",
            {},
        )
        assert_integrity_error(
            conn,
            "UPDATE metric SET actual_revenue=1 WHERE id=405",
            {},
        )
        assert_integrity_error(
            conn,
            "UPDATE metric SET source_type='simulated', user_confirmed=1 WHERE id=405",
            {},
        )
        conn.execute(
            text(
                "UPDATE metric SET user_confirmed=1, actual_revenue=120, actual_cost=20, shares=3 "
                "WHERE id=405"
            )
        )
        conn.commit()
        assert conn.execute(
            text("SELECT actual_revenue, actual_cost, shares FROM metric WHERE id=405")
        ).one() == (120.0, 20.0, 3)

        run_params = {
            "blogger_id": 41,
            "output_id": 403,
            "metric_id": 405,
            "status": "analyzed",
            "key": "feedback-key",
            "snapshot": "{}",
            "hash": "a" * 64,
            "now": now,
        }
        conn.execute(
            text(
                """
                INSERT INTO feedback_run (
                    blogger_id, output_id, primary_metric_id, status, idempotency_key,
                    snapshot_json, snapshot_hash, prompt_version, model_name,
                    created_at, updated_at
                ) VALUES (
                    :blogger_id, :output_id, :metric_id, :status, :key,
                    :snapshot, :hash, 'phase4-v1', 'fake', :now, :now
                )
                """
            ),
            run_params,
        )
        conn.commit()
        assert_integrity_error(
            conn,
            """
            INSERT INTO feedback_run (
                blogger_id, output_id, primary_metric_id, status, idempotency_key,
                snapshot_json, snapshot_hash, prompt_version, model_name, created_at, updated_at
            ) VALUES (41, 403, 405, 'analyzed', 'feedback-key', '{}', :hash,
                      'phase4-v1', 'fake', :now, :now)
            """,
            {"hash": "b" * 64, "now": now},
        )
        assert_integrity_error(
            conn,
            """
            INSERT INTO feedback_run (
                blogger_id, output_id, primary_metric_id, status, idempotency_key,
                snapshot_json, snapshot_hash, prompt_version, model_name, created_at, updated_at
            ) VALUES (41, 403, 405, 'invalid', 'feedback-other', '{}', :hash,
                      'phase4-v1', 'fake', :now, :now)
            """,
            {"hash": "c" * 64, "now": now},
        )
        assert_integrity_error(
            conn,
            """
            INSERT INTO profile_feedback_revision (
                run_id, blogger_id, field_name, before, after, reason, status, version,
                created_at, updated_at
            ) VALUES (
                1, 41, 'suit_type', NULL, '美食探店', '证据支持', 'pending', 0, :now, :now
            )
            """,
            {"now": now},
        )
        assert_integrity_error(
            conn,
            """
            INSERT INTO library_evolution_revision (
                run_id, lib_type, action, candidate_json, reason, status, version,
                created_at, updated_at
            ) VALUES (
                1, 'unknown', 'add', '{}', '非法库类型', 'pending', 1, :now, :now
            )
            """,
            {"now": now},
        )
        assert_integrity_error(
            conn,
            """
            INSERT INTO feedback_evidence (
                feedback_run_id, evidence_type, ref_id, claim, snapshot_json, created_at
            ) VALUES (99999, 'metric', 405, '跨运行证据', '{}', :now)
            """,
            {"now": now},
        )
        assert_integrity_error(
            conn,
            """
            INSERT INTO operational_indicator (
                blogger_id, category, name, meaning, formula_key, source_tables_json,
                unit, direction, active, version, created_at, updated_at
            ) VALUES (
                41, 'traffic', '非法方向', '方向必须受控', 'traffic_views', '["metric"]',
                'views', 'up', 1, 1, :now, :now
            )
            """,
            {"now": now},
        )
        report_params = {"hash": "d" * 64, "now": now}
        conn.execute(
            text(
                """
                INSERT INTO report (
                    blogger_id, status, idempotency_key, snapshot_json, snapshot_hash,
                    prompt_version, model_name, created_at, updated_at
                ) VALUES (
                    41, 'pending', 'report-key', '{}', :hash,
                    'phase4-report-v1', 'fake', :now, :now
                )
                """
            ),
            report_params,
        )
        conn.commit()
        assert_integrity_error(
            conn,
            """
            INSERT INTO report (
                blogger_id, status, idempotency_key, snapshot_json, snapshot_hash,
                prompt_version, model_name, created_at, updated_at
            ) VALUES (
                41, 'pending', 'report-key', '{}', :hash,
                'phase4-report-v1', 'fake', :now, :now
            )
            """,
            report_params,
        )


def test_phase4_orm_constraints_and_public_models_match_migration_contract() -> None:
    metric_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Metric.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_metric_shares_nonnegative" in metric_checks
    assert "ck_metric_actual_revenue_nonnegative" in metric_checks
    assert "ck_metric_actual_cost_nonnegative" in metric_checks
    assert "ck_metric_actual_values_confirmed_manual" in metric_checks

    feedback_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in FeedbackRun.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert feedback_uniques["uq_feedback_run_blogger_idempotency"] == (
        "blogger_id",
        "idempotency_key",
    )
    report_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Report.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert report_uniques["uq_report_blogger_idempotency"] == (
        "blogger_id",
        "idempotency_key",
    )

    assert {
        FeedbackRun.__tablename__,
        FeedbackEvidence.__tablename__,
        ProfileFeedbackRevision.__tablename__,
        AssetEffectRevision.__tablename__,
        PlaceCommercialRevision.__tablename__,
        LibraryEvolutionRevision.__tablename__,
        OperationalIndicator.__tablename__,
        IndicatorObservation.__tablename__,
        Report.__tablename__,
        ReportEvidence.__tablename__,
    } == PHASE4_TABLES
    assert Blogger.__table__.c.knowledge_focus.nullable
    assert Asset.__table__.c.effect.nullable
    assert Asset.__table__.c.effect_weight.nullable


def test_alembic_check_reports_no_model_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "phase4-alembic-check.db"
    upgrade(db_path)
    command.check(migration_config(db_path))

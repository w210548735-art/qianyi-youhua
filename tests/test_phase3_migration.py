"""第三阶段 0004 迁移的空库、升级保留与往返验证。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.db.session import Base
from app.models import (
    AssetPlace,
    CollectionJob,
    Metric,
    Output,
    OutputAsset,
    OutputPlace,
    PublishEvent,
    ReminderEvent,
    Schedule,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
PHASE1_TABLES = {
    "blogger",
    "conversation_session",
    "conversation_message",
    "source_document",
    "build_run",
    "decision_log",
    "asset",
    "asset_source",
    "asset_embedding",
    "memory_record",
    "memory_embedding",
    "task_session",
    "session_message",
    "task_checkpoint",
    "task_artifact",
    "place",
}
PHASE2_TABLES = {"assessment", "assessment_indicator", "assessment_evidence"}
PHASE3_TABLES = {
    "output",
    "output_asset",
    "output_place",
    "asset_place",
    "schedule",
    "publish_event",
    "reminder_event",
    "metric",
    "collection_job",
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


def connection(db_path: Path):
    return create_engine(f"sqlite:///{db_path.as_posix()}").connect()


def insert_legacy_data(db_path: Path) -> None:
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
                    31, '三期迁移博主', '抖音', '[\"美食\"]', '口播', '1万-10万',
                    '[\"商单\"]', '黔东南', NULL, '周更', '探店',
                    'complete', :created_at, :updated_at
                )
                """
            ),
            {"created_at": now, "updated_at": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO asset (
                    id, blogger_id, lib_type, category, title, content, tags_json,
                    source_type, credibility, origin, dedupe_key, manual_locked,
                    created_at, updated_at
                ) VALUES (
                    301, 31, 'knowledge', '美食', '迁移资产', '迁移保留的知识', '[]',
                    'official', 5, 'seed', 'phase3-migration-asset', 0,
                    :created_at, :updated_at
                )
                """
            ),
            {"created_at": now, "updated_at": now},
        )
        conn.commit()


def test_0004_revision_is_explicit_and_does_not_use_runtime_metadata() -> None:
    source = (ROOT_DIR / "migrations/versions/0004_phase3_output.py").read_text(encoding="utf-8")

    assert 'revision = "0004_phase3_output"' in source
    assert 'down_revision = "0003_phase2_assessment"' in source
    assert "Base.metadata.create_all" not in source
    assert "Base.metadata.drop_all" not in source
    assert source.count("op.create_table") >= len(PHASE3_TABLES)


def test_empty_database_upgrades_base_to_phase3_head(tmp_path: Path) -> None:
    db_path = tmp_path / "phase3-empty.db"

    upgrade(db_path)

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert set(inspector.get_table_names()) == PHASE1_TABLES | PHASE2_TABLES | PHASE3_TABLES | {"alembic_version"}
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_phase3_output"

        output_columns = {column["name"] for column in inspector.get_columns("output")}
        assert {
            "id",
            "blogger_id",
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
        } <= output_columns
        schedule_columns = {column["name"] for column in inspector.get_columns("schedule")}
        assert {
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
        } <= schedule_columns

        unique_output_asset = {
            item["name"] for item in inspector.get_unique_constraints("output_asset")
        }
        assert "uq_output_asset_usage" in unique_output_asset
        unique_reminder = {
            item["name"] for item in inspector.get_unique_constraints("reminder_event")
        }
        assert "uq_reminder_schedule_date" in unique_reminder


def test_existing_phase2_database_upgrades_without_data_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "phase2-existing.db"

    upgrade(db_path, "0003_phase2_assessment")
    insert_legacy_data(db_path)
    upgrade(db_path)

    with connection(db_path) as conn:
        assert conn.execute(text("SELECT name FROM blogger WHERE id = 31")).scalar_one() == "三期迁移博主"
        assert conn.execute(text("SELECT title FROM asset WHERE id = 301")).scalar_one() == "迁移资产"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_phase3_output"
        assert PHASE3_TABLES <= set(inspect(conn).get_table_names())


def test_downgrade_upgrade_round_trip_preserves_phase2_data(tmp_path: Path) -> None:
    db_path = tmp_path / "phase3-round-trip.db"

    upgrade(db_path)
    insert_legacy_data(db_path)
    with connection(db_path) as conn:
        conn.execute(
            text(
                """
                INSERT INTO output (
                    blogger_id, type, category, title, content_json, status,
                    version, manual_locked, prompt_version, model_name,
                    created_at, updated_at
                ) VALUES (
                    31, 'script', '美食', '迁移输出', '{}', 'succeeded',
                    1, 0, 'phase3-v1', 'fake', :created_at, :updated_at
                )
                """
            ),
            {"created_at": datetime.utcnow(), "updated_at": datetime.utcnow()},
        )
        conn.commit()

    downgrade(db_path, "0003_phase2_assessment")
    with connection(db_path) as conn:
        tables = set(inspect(conn).get_table_names())
        assert not PHASE3_TABLES.intersection(tables)
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 31")) == "三期迁移博主"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_phase2_assessment"

    upgrade(db_path)
    with connection(db_path) as conn:
        assert PHASE3_TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 31")) == "三期迁移博主"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_phase3_output"


def test_upgrade_adopts_runtime_precreated_phase3_tables(tmp_path: Path) -> None:
    """兼容旧应用先 create_all、版本仍停在 0003 的数据库。"""

    db_path = tmp_path / "phase3-runtime-precreated.db"
    upgrade(db_path, "0003_phase2_assessment")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    upgrade(db_path)
    with connection(db_path) as conn:
        assert PHASE3_TABLES <= set(inspect(conn).get_table_names())
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004_phase3_output"


def test_phase3_models_expose_required_table_names() -> None:
    assert {
        Output.__tablename__,
        OutputAsset.__tablename__,
        OutputPlace.__tablename__,
        AssetPlace.__tablename__,
        Schedule.__tablename__,
        PublishEvent.__tablename__,
        ReminderEvent.__tablename__,
        Metric.__tablename__,
        CollectionJob.__tablename__,
    } == PHASE3_TABLES

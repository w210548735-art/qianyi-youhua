"""Alembic 迁移链的空库、升级保留和往返测试。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import Place

pytestmark = pytest.mark.migration

ROOT_DIR = Path(__file__).resolve().parents[1]
INITIAL_TABLES = {
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
}
ALL_TABLES = INITIAL_TABLES | {"place"}
PHASE1_HEAD = "0002_phase1_closure"


def migration_config(db_path: Path) -> Config:
    """创建指向指定临时 SQLite 数据库的 Alembic 配置。"""
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def upgrade(db_path: Path, revision: str = PHASE1_HEAD) -> None:
    command.upgrade(migration_config(db_path), revision)


def downgrade(db_path: Path, revision: str) -> None:
    command.downgrade(migration_config(db_path), revision)


def connection(db_path: Path):
    return create_engine(f"sqlite:///{db_path.as_posix()}").connect()


def insert_blogger(db_path: Path, blogger_id: int = 1) -> None:
    """用 0001 的字段写入一条旧版本数据。"""
    with connection(db_path) as conn:
        conn.execute(
            text(
                """
                INSERT INTO blogger (
                    id, name, platform, content_types_json, style, follower_band,
                    monetization_types_json, routes, viral_topic, frequency, suit_type,
                    profile_state, created_at, updated_at
                ) VALUES (
                    :id, :name, :platform, :content_types_json, :style, :follower_band,
                    :monetization_types_json, :routes, :viral_topic, :frequency, :suit_type,
                    :profile_state, :created_at, :updated_at
                )
                """
            ),
            {
                "id": blogger_id,
                "name": f"旧版本博主{blogger_id}",
                "platform": "抖音",
                "content_types_json": "[\"美食\"]",
                "style": "口播",
                "follower_band": "1万-10万",
                "monetization_types_json": "[\"商单\"]",
                "routes": "黔东南",
                "viral_topic": None,
                "frequency": "周更",
                "suit_type": "探店",
                "profile_state": "complete",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            },
        )
        conn.commit()


def test_0001_is_frozen_as_explicit_alembic_ddl() -> None:
    source = (ROOT_DIR / "migrations/versions/0001_phase1_initial.py").read_text(encoding="utf-8")

    assert "Base.metadata.create_all" not in source
    assert "Base.metadata.drop_all" not in source
    assert "op.create_table" in source
    assert "op.create_index" in source


def test_empty_database_upgrades_from_base_to_phase1_head(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"

    upgrade(db_path)

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert set(inspector.get_table_names()) == ALL_TABLES | {"alembic_version"}
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == PHASE1_HEAD
        assert {column["name"] for column in inspector.get_columns("blogger")} >= {
            "deleted_at",
        }
        assert [column["name"] for column in inspector.get_columns("place")] == [
            "id",
            "blogger_id",
            "name",
            "category",
            "location",
            "specialty",
            "tags_json",
            "source_type",
            "source_url",
            "credibility",
            "like_level",
            "est_cost",
            "est_benefit",
            "fits_koc",
            "fits_shoot",
            "decision_id",
            "origin",
            "manual_locked",
            "dedupe_key",
            "deleted_at",
            "created_at",
            "updated_at",
        ]
        assert {index["name"] for index in inspector.get_indexes("place")} >= {
            "ix_place_blogger_id",
            "ix_place_category",
            "ix_place_source_type",
            "ix_place_deleted_at",
        }
        unique_names = {item["name"] for item in inspector.get_unique_constraints("place")}
        assert "uq_place_blogger_dedupe" in unique_names


def test_existing_0001_data_survives_upgrade_to_0002(tmp_path: Path) -> None:
    db_path = tmp_path / "existing.db"

    upgrade(db_path, "0001_phase1_initial")
    insert_blogger(db_path, blogger_id=7)
    upgrade(db_path)

    with connection(db_path) as conn:
        blogger = conn.execute(
            text("SELECT id, name, deleted_at FROM blogger WHERE id = :id"),
            {"id": 7},
        ).one()
        assert blogger.id == 7
        assert blogger.name == "旧版本博主7"
        assert blogger.deleted_at is None
        assert conn.scalar(text("SELECT COUNT(*) FROM place")) == 0
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == PHASE1_HEAD


def test_upgrade_adopts_place_precreated_by_runtime_metadata(tmp_path: Path) -> None:
    """兼容旧应用启动时 create_all 提前创建新表、revision 仍停在 0001 的数据库。"""
    db_path = tmp_path / "runtime_precreated.db"
    upgrade(db_path, "0001_phase1_initial")
    insert_blogger(db_path, blogger_id=9)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Place.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO place (
                    id, blogger_id, name, category, tags_json, source_type, credibility,
                    origin, manual_locked, dedupe_key, created_at, updated_at
                ) VALUES (
                    1, 9, '预建地点', '景区', '[]', 'manual', 3,
                    'manual', 1, 'runtime-place', :now, :now
                )"""
            ),
            {"now": datetime.utcnow()},
        )

    upgrade(db_path)

    with connection(db_path) as conn:
        assert conn.scalar(text("SELECT name FROM place WHERE id = 1")) == "预建地点"
        assert "deleted_at" in {
            column["name"] for column in inspect(conn).get_columns("blogger")
        }
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == PHASE1_HEAD


def test_downgrade_upgrade_round_trip_preserves_0001_data(tmp_path: Path) -> None:
    db_path = tmp_path / "round_trip.db"

    upgrade(db_path)
    insert_blogger(db_path, blogger_id=11)
    downgrade(db_path, "0001_phase1_initial")

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert "place" not in inspector.get_table_names()
        assert "deleted_at" not in {column["name"] for column in inspector.get_columns("blogger")}
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 11")) == "旧版本博主11"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001_phase1_initial"

    upgrade(db_path)

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert "place" in inspector.get_table_names()
        assert "deleted_at" in {column["name"] for column in inspector.get_columns("blogger")}
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 11")) == "旧版本博主11"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == PHASE1_HEAD

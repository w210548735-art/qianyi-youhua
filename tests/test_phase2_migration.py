"""第二阶段 0003 迁移的空库、升级保留与往返验证。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Float, create_engine, inspect, text

from app.models import Assessment, AssessmentEvidence, AssessmentIndicator

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


def insert_legacy_blogger(db_path: Path, blogger_id: int = 7) -> None:
    """使用 0001 字段写入数据，验证升级不会丢失第一阶段记录。"""

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
                    :id, :name, '抖音', '[\"美食\"]', '口播', '1万-10万',
                    '[\"商单\"]', '黔东南', NULL, '周更', '探店',
                    'complete', :created_at, :updated_at
                )
                """
            ),
            {"id": blogger_id, "name": f"历史博主{blogger_id}", "created_at": now, "updated_at": now},
        )
        conn.commit()


def test_0003_revision_is_explicit_and_has_no_runtime_metadata_dependency() -> None:
    source = (ROOT_DIR / "migrations/versions/0003_phase2_assessment.py").read_text(encoding="utf-8")

    assert 'revision = "0003_phase2_assessment"' in source
    assert 'down_revision = "0002_phase1_closure"' in source
    assert "Base.metadata.create_all" not in source
    assert "Base.metadata.drop_all" not in source
    assert source.count("op.create_table") >= 3


def test_empty_database_upgrades_from_base_to_phase2_head(tmp_path: Path) -> None:
    db_path = tmp_path / "phase2-empty.db"

    upgrade(db_path)

    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert set(inspector.get_table_names()) == PHASE1_TABLES | PHASE2_TABLES | {"alembic_version"}
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_phase2_assessment"
        assessment_columns = {column["name"]: column for column in inspector.get_columns("assessment")}
        assert set(assessment_columns) == {
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
        assert isinstance(assessment_columns["overall_score"]["type"], Float)
        indicator_columns = {
            column["name"]: column for column in inspector.get_columns("assessment_indicator")
        }
        assert set(indicator_columns) == {
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
        assert isinstance(indicator_columns["score"]["type"], Float)
        assert indicator_columns["score"]["nullable"] is False
        assert indicator_columns["reason"]["nullable"] is False
        assert set(column["name"] for column in inspector.get_columns("assessment_evidence")) == {
            "id",
            "assessment_id",
            "indicator_id",
            "evidence_type",
            "asset_id",
            "source_document_id",
            "claim",
            "created_at",
        }
        assert {
            item["name"] for item in inspector.get_unique_constraints("assessment")
        } >= {"uq_assessment_blogger_idempotency"}


def test_existing_phase1_database_upgrades_without_data_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "phase1-existing.db"

    upgrade(db_path, "0002_phase1_closure")
    insert_legacy_blogger(db_path)
    upgrade(db_path)

    with connection(db_path) as conn:
        blogger = conn.execute(text("SELECT id, name, deleted_at FROM blogger WHERE id = 7")).one()
        assert blogger.id == 7
        assert blogger.name == "历史博主7"
        assert blogger.deleted_at is None
        assert conn.scalar(text("SELECT COUNT(*) FROM assessment")) == 0
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_phase2_assessment"


def test_downgrade_upgrade_round_trip_keeps_phase1_data_and_recreates_phase2_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "phase2-round-trip.db"

    upgrade(db_path)
    insert_legacy_blogger(db_path, blogger_id=11)
    with connection(db_path) as conn:
        conn.execute(
            text(
                """
                INSERT INTO assessment (
                    blogger_id, status, idempotency_key, input_snapshot_json,
                    prompt_version, model_name, created_at
                ) VALUES (11, 'failed', 'round-trip-key', '{}', 'phase2-v1',
                          'deepseek-v4-flash', :created_at)
                """
            ),
            {"created_at": datetime.utcnow()},
        )
        conn.commit()

    downgrade(db_path, "0002_phase1_closure")
    with connection(db_path) as conn:
        inspector = inspect(conn)
        assert not PHASE2_TABLES.intersection(inspector.get_table_names())
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 11")) == "历史博主11"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0002_phase1_closure"

    upgrade(db_path)
    with connection(db_path) as conn:
        assert PHASE2_TABLES.issubset(set(inspect(conn).get_table_names()))
        assert conn.scalar(text("SELECT name FROM blogger WHERE id = 11")) == "历史博主11"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_phase2_assessment"


def test_upgrade_adopts_runtime_precreated_phase2_tables(tmp_path: Path) -> None:
    """兼容应用先 create_all、但 Alembic 版本仍停在 0002 的数据库。"""

    db_path = tmp_path / "phase2-runtime-precreated.db"
    upgrade(db_path, "0002_phase1_closure")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Assessment.__table__.create(engine)  # type: ignore[attr-defined]
    AssessmentIndicator.__table__.create(engine)  # type: ignore[attr-defined]
    AssessmentEvidence.__table__.create(engine)  # type: ignore[attr-defined]

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO blogger (
                    id, name, platform, content_types_json, style, follower_band,
                    monetization_types_json, profile_state, created_at, updated_at
                ) VALUES (22, '预建博主', '抖音', '[\"美食\"]', '口播', '1万-10万',
                          '[]', 'complete', :created_at, :updated_at)
                """
            ),
            {"created_at": datetime.utcnow(), "updated_at": datetime.utcnow()},
        )

    upgrade(db_path)
    with connection(db_path) as conn:
        assert PHASE2_TABLES.issubset(set(inspect(conn).get_table_names()))
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_phase2_assessment"

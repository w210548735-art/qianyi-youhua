"""第一阶段收尾结构：博主软删除字段与地点库。"""

import sqlalchemy as sa
from alembic import op

revision = "0002_phase1_closure"
down_revision = "0001_phase1_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加 Blogger 软删除字段，并创建按博主隔离的地点库。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    blogger_columns = {column["name"] for column in inspector.get_columns("blogger")}
    if "deleted_at" not in blogger_columns:
        op.add_column("blogger", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    inspector = sa.inspect(bind)
    blogger_indexes = {index["name"] for index in inspector.get_indexes("blogger")}
    if "ix_blogger_deleted_at" not in blogger_indexes:
        op.create_index("ix_blogger_deleted_at", "blogger", ["deleted_at"])

    inspector = sa.inspect(bind)
    if "place" not in inspector.get_table_names():
        op.create_table(
        "place",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("blogger_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("specialty", sa.Text(), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("credibility", sa.Integer(), nullable=False),
        sa.Column("like_level", sa.Integer(), nullable=True),
        sa.Column("est_cost", sa.Float(), nullable=True),
        sa.Column("est_benefit", sa.Float(), nullable=True),
        sa.Column("fits_koc", sa.Boolean(), nullable=True),
        sa.Column("fits_shoot", sa.Boolean(), nullable=True),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("manual_locked", sa.Boolean(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("credibility >= 0 AND credibility <= 5", name="ck_place_credibility"),
        sa.ForeignKeyConstraint(["blogger_id"], ["blogger.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["decision_log.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("blogger_id", "dedupe_key", name="uq_place_blogger_dedupe"),
        )
    else:
        required_columns = {
            "id", "blogger_id", "name", "category", "location", "specialty",
            "tags_json", "source_type", "source_url", "credibility", "like_level",
            "est_cost", "est_benefit", "fits_koc", "fits_shoot", "decision_id",
            "origin", "manual_locked", "dedupe_key", "deleted_at", "created_at", "updated_at",
        }
        existing_columns = {column["name"] for column in inspector.get_columns("place")}
        missing = required_columns - existing_columns
        if missing:
            raise RuntimeError(f"PLACE_SCHEMA_INCOMPLETE:{','.join(sorted(missing))}")

    inspector = sa.inspect(bind)
    existing_indexes = {index["name"] for index in inspector.get_indexes("place")}
    for index_name, columns in (
        ("ix_place_blogger_id", ["blogger_id"]),
        ("ix_place_category", ["category"]),
        ("ix_place_source_type", ["source_type"]),
        ("ix_place_deleted_at", ["deleted_at"]),
    ):
        if index_name not in existing_indexes:
            op.create_index(index_name, "place", columns)


def downgrade() -> None:
    """移除地点库与博主软删除字段。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "place" in inspector.get_table_names():
        existing_indexes = {index["name"] for index in inspector.get_indexes("place")}
        for index_name in (
            "ix_place_deleted_at",
            "ix_place_source_type",
            "ix_place_category",
            "ix_place_blogger_id",
        ):
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="place")
        op.drop_table("place")
    inspector = sa.inspect(bind)
    blogger_indexes = {index["name"] for index in inspector.get_indexes("blogger")}
    if "ix_blogger_deleted_at" in blogger_indexes:
        op.drop_index("ix_blogger_deleted_at", table_name="blogger")
    blogger_columns = {column["name"] for column in sa.inspect(bind).get_columns("blogger")}
    if "deleted_at" in blogger_columns:
        op.drop_column("blogger", "deleted_at")

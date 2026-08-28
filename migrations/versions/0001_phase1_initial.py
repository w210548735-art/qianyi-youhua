"""第一阶段初始数据库结构。"""

from alembic import op

import app.models  # noqa: F401
from app.db.session import Base

revision = "0001_phase1_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

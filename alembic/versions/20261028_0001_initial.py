"""初始表结构，以 app.models 为准。

Revision ID: 20261028_0001
Revises:
Create Date: 2026-10-28
"""

from app.models import Base

revision = "20261028_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from alembic import op

    Base.metadata.drop_all(bind=op.get_bind())

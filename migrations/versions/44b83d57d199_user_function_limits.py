"""User feature access and concurrency overrides."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "44b83d57d199"
down_revision = "7a9b3e018c21"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("restrictions", postgresql.JSONB(), nullable=False, server_default="{}")
    )
    op.alter_column("users", "restrictions", server_default=None)


def downgrade():
    op.drop_column("users", "restrictions")

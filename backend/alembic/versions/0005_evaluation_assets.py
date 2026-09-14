"""add persisted benchmark assets and optional experiment baseline"""

import sqlalchemy as sa

from alembic import op

revision = "0005_evaluation_assets"
down_revision = "0004_tool_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "benchmarks",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("version", sa.String(120), nullable=False),
        sa.Column("environment_snapshot", sa.String(200), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    with op.batch_alter_table("experiments") as batch:
        batch.alter_column(
            "baseline_id",
            existing_type=sa.String(80),
            nullable=True,
        )


def downgrade():
    with op.batch_alter_table("experiments") as batch:
        batch.alter_column(
            "baseline_id",
            existing_type=sa.String(80),
            nullable=False,
        )
    op.drop_table("benchmarks")

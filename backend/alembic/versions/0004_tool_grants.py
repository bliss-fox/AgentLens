"""add hashed run-scoped tool grants"""

import sqlalchemy as sa

from alembic import op

revision = "0004_tool_grants"
down_revision = "0003_event_span_lineage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_grants",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(120), nullable=False),
        sa.Column("experiment_id", sa.String(80), nullable=False),
        sa.Column("cassette_id", sa.String(80), nullable=False),
        sa.Column("cassette_content_sha256", sa.String(64), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("remaining_calls", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("run_id", "experiment_id", "status", "expires_at"):
        op.create_index(f"ix_tool_grants_{column}", "tool_grants", [column])


def downgrade():
    op.drop_table("tool_grants")

"""backfill timestamps and align nullability with ORM metadata"""

import sqlalchemy as sa

from alembic import op

revision = "0002_schema_parity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text("UPDATE candidates SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    )
    op.execute(
        sa.text("UPDATE experiments SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    )
    for table_name in ("candidates", "experiments"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                existing_server_default=sa.func.now(),
                nullable=False,
            )


def downgrade():
    for table_name in ("experiments", "candidates"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "created_at",
                existing_type=sa.DateTime(timezone=True),
                existing_server_default=sa.func.now(),
                nullable=True,
            )

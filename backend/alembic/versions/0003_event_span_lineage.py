"""persist trace span lineage in the independent event audit table"""

import sqlalchemy as sa

from alembic import op

revision = "0003_event_span_lineage"
down_revision = "0002_schema_parity"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("span_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("parent_span_id", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("parent_span_id")
        batch_op.drop_column("span_id")

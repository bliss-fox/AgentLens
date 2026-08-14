"""initial AgentLens storage"""
import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("candidates", sa.Column("id", sa.String(80), primary_key=True), sa.Column("name", sa.String(120), nullable=False), sa.Column("version", sa.String(40), nullable=False), sa.Column("fingerprint", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_table("experiments", sa.Column("id", sa.String(80), primary_key=True), sa.Column("status", sa.String(24), nullable=False), sa.Column("prompt", sa.Text(), nullable=False), sa.Column("candidate_id", sa.String(80), sa.ForeignKey("candidates.id"), nullable=False), sa.Column("baseline_id", sa.String(80), sa.ForeignKey("candidates.id"), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("metrics", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_table("runs", sa.Column("id", sa.String(120), primary_key=True), sa.Column("experiment_id", sa.String(80), sa.ForeignKey("experiments.id"), nullable=False), sa.Column("task_id", sa.String(80), nullable=False), sa.Column("candidate_id", sa.String(80), nullable=False), sa.Column("seed", sa.Integer(), nullable=False), sa.Column("status", sa.String(24), nullable=False), sa.Column("success", sa.Integer(), nullable=False), sa.Column("trajectory_score", sa.Float(), nullable=False), sa.Column("cost_cny", sa.Float()), sa.Column("result", sa.JSON(), nullable=False))
    op.create_table("events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.String(120), sa.ForeignKey("runs.id"), nullable=False), sa.Column("seq", sa.Integer(), nullable=False), sa.Column("type", sa.String(40), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("run_id", "seq", name="uq_run_event_seq"))
    for table, columns in [("experiments", ["status", "created_at"]), ("runs", ["experiment_id", "task_id", "candidate_id"]), ("events", ["run_id", "type"])]:
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("events"); op.drop_table("runs"); op.drop_table("experiments"); op.drop_table("candidates")

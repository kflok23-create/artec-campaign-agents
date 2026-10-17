"""Phase 0: agent_runs and spend_ledger.

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USD = sa.Numeric(12, 6)


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("brand", sa.Text, nullable=False),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column("task_id", sa.BigInteger),
        sa.Column("run_id", sa.Text),
        sa.Column("attempt", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", USD, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("session_id", sa.Text),
        sa.Column("output", postgresql.JSONB),
        sa.Column("usage", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'invalid_output', 'error')", name="agent_runs_status"
        ),
    )
    op.create_index("agent_runs_run_id", "agent_runs", ["run_id"])
    op.create_index("agent_runs_created_at", "agent_runs", ["created_at"])

    op.create_table(
        "spend_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("brand", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="reserved"),
        sa.Column("reserved_usd", USD, nullable=False),
        sa.Column("amount_usd", USD, nullable=False, server_default="0"),
        sa.Column(
            "agent_run_id",
            sa.BigInteger,
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("note", sa.Text),
        sa.CheckConstraint("provider IN ('anthropic', 'fal')", name="spend_ledger_provider"),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'released')", name="spend_ledger_status"
        ),
        sa.CheckConstraint(
            "reserved_usd >= 0 AND amount_usd >= 0", name="spend_ledger_nonnegative"
        ),
    )
    op.create_index("spend_ledger_window", "spend_ledger", ["provider", "brand", "created_at"])
    op.create_index("spend_ledger_run_id", "spend_ledger", ["run_id"])


def downgrade() -> None:
    op.drop_table("spend_ledger")
    op.drop_table("agent_runs")

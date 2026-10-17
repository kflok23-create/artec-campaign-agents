"""SQLAlchemy models. Every schema change goes through a migration in db/migrations.

Phase 0 creates only the two audit tables the gate needs: `agent_runs` and `spend_ledger`.
The rest of the data model (brands, campaigns, tasks, ...) arrives in Phase 1.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

USD = Numeric(12, 6)


class Base(DeclarativeBase):
    pass


class AgentRun(Base):
    """One row per agent call: what ran, on which model, what it cost and what it returned.

    The row is written as `running` before the call, so a crash mid-call still leaves a trace.
    Never store prompts with secrets here; agents never receive secrets in the first place.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brand: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(BigInteger)  # FK to tasks once Phase 1 adds it
    run_id: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(SmallInteger, default=1)
    model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    session_id: Mapped[str | None] = mapped_column(Text)
    output: Mapped[Any | None] = mapped_column(JSONB)
    usage: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'invalid_output', 'error')", name="agent_runs_status"
        ),
        Index("agent_runs_run_id", "run_id"),
        Index("agent_runs_created_at", "created_at"),
    )


class SpendLedger(Base):
    """Money spent or held, per paid call.

    The budget guard writes a `reserved` row before a paid call (holding the call's ceiling),
    then settles it with the actual cost. Reserved rows count against the cap until settled,
    so concurrent calls cannot overrun it, and a crash mid-call keeps its hold.
    """

    __tablename__ = "spend_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brand: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="reserved")
    reserved_usd: Mapped[Decimal] = mapped_column(USD)
    amount_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("provider IN ('anthropic', 'fal')", name="spend_ledger_provider"),
        CheckConstraint(
            "status IN ('reserved', 'settled', 'released')", name="spend_ledger_status"
        ),
        CheckConstraint("reserved_usd >= 0 AND amount_usd >= 0", name="spend_ledger_nonnegative"),
        Index("spend_ledger_window", "provider", "brand", "created_at"),
        Index("spend_ledger_run_id", "run_id"),
    )

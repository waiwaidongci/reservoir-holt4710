"""Append-only ledger versions, their per-reservoir heads, and pins.

No existing table is altered: all bookkeeping lives in new tables so an
upgraded installation only needs ``create_all`` to add them.
"""

from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class LedgerHead(Base):
    """One row per reservoir; serialises merges and points at the current version."""

    __tablename__ = 'ledger_heads'
    __table_args__ = (UniqueConstraint('reservoir_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reservoir_id: Mapped[str] = mapped_column(String(40), index=True)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(default=0)
    current_version_id: Mapped[int | None] = mapped_column(ForeignKey('ledger_versions.id'))
    updated_at: Mapped[datetime] = mapped_column(default=now)


class LedgerVersion(Base):
    """An immutable view of the merged ledger and its folded readings."""

    __tablename__ = 'ledger_versions'
    __table_args__ = (UniqueConstraint('ledger_id', 'version'), UniqueConstraint('ledger_id', 'checksum'))

    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey('ledger_heads.id'), index=True)
    version: Mapped[int]
    reservoir_id: Mapped[str] = mapped_column(String(40))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class DatasetLedger(Base):
    """Which batch was taken into which ledger, and which version it produced."""

    __tablename__ = 'dataset_ledgers'

    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey('ledger_heads.id'), index=True)
    version_id: Mapped[int | None] = mapped_column(ForeignKey('ledger_versions.id'))
    changed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=now)


class RunLedger(Base):
    """Pins a run to one ledger version, so old forecasts keep their readings."""

    __tablename__ = 'run_ledgers'

    run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey('ledger_heads.id'), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey('ledger_versions.id'))
    created_at: Mapped[datetime] = mapped_column(default=now)


class Recompute(Base):
    """One idempotency row per (earlier run, new version) re-forecast."""

    __tablename__ = 'recomputes'
    __table_args__ = (UniqueConstraint('parent_run_id', 'version_id'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_run_id: Mapped[int] = mapped_column(ForeignKey('runs.id'), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey('runs.id'))
    version_id: Mapped[int] = mapped_column(ForeignKey('ledger_versions.id'))
    created_at: Mapped[datetime] = mapped_column(default=now)

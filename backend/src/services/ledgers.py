"""Ledger heads, immutable versions, and idempotent batch intake.

Importing a batch never mutates an earlier version.  Each import merges its
rows into the reservoir's current ledger under a row lock, appends a new version
only if something actually changed, and pins the batch to the resulting version.
Replaying the same batch therefore changes nothing and creates no new version.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.domain import ledger as ledger_domain
from src.models.ledger import DatasetLedger, LedgerHead, LedgerVersion
from src.models.source import Dataset


class LedgerError(ValueError):
    """A ledger operation the operator has to resolve."""


def _lock_head(db, reservoir_id):
    """Lock (or create) the reservoir's head row.

    Creation races are resolved by the unique constraint: the loser retries the
    lookup and then takes the row lock, so imports of the same reservoir are
    always serialised.
    """
    head = db.scalar(select(LedgerHead).where(LedgerHead.reservoir_id == reservoir_id)
                     .with_for_update())
    if head is not None:
        return head, False
    try:
        with db.begin_nested():
            db.add(LedgerHead(reservoir_id=reservoir_id, rows=[], version=0))
            db.flush()
    except IntegrityError:
        pass
    head = db.scalar(select(LedgerHead).where(LedgerHead.reservoir_id == reservoir_id)
                     .with_for_update())
    return head, True


def reservoir_of(rows):
    return ledger_domain.normalize_rows(rows)[0]


def _used_levels(snapshot):
    return {record["date"]: record["level_m"] for record in snapshot.get("records", [])
            if record["state"] == ledger_domain.USED}


def _value_drift(before, after):
    """Whether a date both versions share now folds to a different level.

    Purely appending newer days is not drift: that is handled by the segment
    boundaries.  Drift is what makes an earlier segment's forecast stale, e.g.
    when a late dual reading re-derives a handover offset.
    """
    old = _used_levels(before)
    new = _used_levels(after)
    shared = old.keys() & new.keys()
    return any(old[day] != new[day] for day in shared)


def _changes(before, after):
    """Summarise what a new version changed, segment by segment and by value."""
    old = {(segment["start"], segment["end"], segment["state"]): segment
           for segment in before["segments"]}
    new = {(segment["start"], segment["end"], segment["state"]): segment
           for segment in after["segments"]}
    added = [segment for key, segment in new.items() if key not in old]
    removed = [segment for key, segment in old.items() if key not in new]
    kept = [segment for key, segment in new.items() if old.get(key) != segment]
    return {
        "added": added,
        "removed": removed,
        "changed": kept,
        "value_drift": _value_drift(before, after),
        "handovers_before": len(before["handovers"]),
        "handovers_after": len(after["handovers"]),
        "isolation_before": before["isolation"],
        "isolation_after": after["isolation"],
    }


def _append(db, head, rows, author, note):
    snapshot = ledger_domain.build_ledger(rows)
    previous = db.get(LedgerVersion, head.current_version_id) if head.current_version_id else None
    before = previous.snapshot if previous else {"segments": [], "handovers": [], "isolation": None}
    version = LedgerVersion(
        ledger_id=head.id, version=head.version + 1, reservoir_id=head.reservoir_id,
        checksum=snapshot["checksum"], rows=rows, snapshot=snapshot,
        changes=_changes(before, snapshot), note=note, author=author,
    )
    db.add(version)
    db.flush()
    head.rows = rows
    head.version = version.version
    head.current_version_id = version.id
    head.updated_at = version.created_at
    return version


def ensure(db, rows, author, note):
    """Merge a batch into its reservoir's ledger and return ``(head, version, changed)``.

    The head row is locked, so concurrent imports of the same reservoir are
    serialised and every version is a complete, ordered snapshot.
    """
    reservoir_id = reservoir_of(rows)
    head, _created = _lock_head(db, reservoir_id)
    if head is None:
        raise LedgerError('台账无法建立，请刷新后重试')
    merged, entries = ledger_domain.merge_rows(head.rows or [], rows)
    canonical = ledger_domain.canonical_rows(merged, entries)
    if canonical == head.rows:
        return head, db.get(LedgerVersion, head.current_version_id), False
    return head, _append(db, head, canonical, author, note), True


def register(db, dataset, head, version, changed):
    """Idempotently record which ledger version a batch produced."""
    item = db.get(DatasetLedger, dataset.id)
    if item is not None:
        return item
    item = DatasetLedger(dataset_id=dataset.id, ledger_id=head.id, version_id=version.id,
                         changed=changed)
    db.add(item)
    db.flush()
    return item


def current(db, ledger_id):
    head = db.get(LedgerHead, ledger_id)
    if head is None or head.current_version_id is None:
        return None
    return db.get(LedgerVersion, head.current_version_id)


def version(db, ledger_id, wanted):
    if wanted is None:
        return current(db, ledger_id)
    head = db.get(LedgerHead, ledger_id)
    if head is None:
        return None
    return db.scalar(select(LedgerVersion).where(LedgerVersion.ledger_id == head.id,
                                                 LedgerVersion.version == wanted))


def head_for_dataset(db, dataset_id):
    link = db.get(DatasetLedger, dataset_id)
    return db.get(LedgerHead, link.ledger_id) if link else None


def version_for_dataset(db, dataset_id):
    link = db.get(DatasetLedger, dataset_id)
    if link is None:
        return None
    return db.get(LedgerVersion, link.version_id) if link.version_id else current(db, link.ledger_id)


def unbind_dataset(db, dataset):
    """Detach a dataset from its ledger so a corrected re-import re-registers cleanly."""
    link = db.get(DatasetLedger, dataset.id)
    if link is not None:
        db.delete(link)

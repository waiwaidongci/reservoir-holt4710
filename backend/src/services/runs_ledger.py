"""Pinning runs to one immutable ledger version."""

from sqlalchemy.exc import IntegrityError

from src.domain import ledger as ledger_domain
from src.models.ledger import LedgerHead, LedgerVersion, RunLedger
from src.models.source import Dataset
from src.services import ledgers


def pin(db, run, dataset_id, note=None):
    """Bind a run to one ledger version and return it.

    The version is the reservoir ledger's current version at the moment the run
    is queued.  Later supplements create new versions but never rewrite this
    one, so an old forecast can always show the readings and offsets it used.
    """
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ValueError('找不到指定的数据批次')
    head = ledgers.head_for_dataset(db, dataset_id)
    if head is None:
        try:
            with db.begin_nested():
                head, version, changed = ledgers.ensure(db, dataset.rows, dataset.author,
                                                        f'绑定时补建台账 · 批次 {dataset.id}')
                ledgers.register(db, dataset, head, version, changed)
        except IntegrityError:
            head = ledgers.head_for_dataset(db, dataset_id)
            if head is None:
                raise
    existing = db.get(RunLedger, run.id)
    if existing is not None:
        return db.get(LedgerVersion, existing.version_id)
    version = ledgers.current(db, head.id)
    db.add(RunLedger(run_id=run.id, ledger_id=head.id, version_id=version.id))
    db.flush()
    return version


def of_run(db, run_id):
    link = db.get(RunLedger, run_id)
    if link is None:
        return None, None
    return db.get(LedgerHead, link.ledger_id), db.get(LedgerVersion, link.version_id)


def version_view(version):
    snapshot = version.snapshot
    return {
        'id': version.id,
        'version': version.version,
        'reservoir_id': version.reservoir_id,
        'checksum': version.checksum,
        'note': version.note,
        'author': version.author,
        'created_at': version.created_at,
        'changes': version.changes or {},
        'segments': snapshot.get('segments', []),
        'handovers': snapshot.get('handovers', []),
        'isolation': snapshot.get('isolation'),
        'counts': snapshot.get('counts', {}),
        'gaps': snapshot.get('gaps', []),
        'records': snapshot.get('records', []),
    }

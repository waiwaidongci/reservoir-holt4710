"""End-to-end flow: intake, versions, run pinning, recompute and idempotency."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-only')

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.domain import ledger as ledger_domain
from src.models.jobs import Run
from src.models.ledger import LedgerHead, LedgerVersion, Recompute, RunLedger
from src.models.source import Dataset
from src.services import ledgers, runs_ledger
from src.services.datasets import save
from src.services.execution import recompute_affected


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def ingest(self, name, rows):
        with self.Session.begin() as db:
            dataset, created = save(db, name, f'{name}.csv', rows, 'analyst')
            head, version, changed = ledgers.ensure(db, rows, 'analyst', name)
            ledgers.register(db, dataset, head, version, changed)
            return dataset.id, version.version, changed

    def test_replaying_a_batch_creates_no_new_version(self):
        rows = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 1.0 + d / 100}
                for d in range(1, 6)]
        _id, version, changed = self.ingest('batch a', rows)
        self.assertTrue(changed)
        self.assertEqual(version, 1)
        _id2, version2, changed2 = self.ingest('batch a again', rows)
        self.assertFalse(changed2)
        self.assertEqual(version2, 1)
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(LedgerVersion)).all()), 1)

    def test_versions_are_immutable_and_carry_changes(self):
        base = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 1.0 + d / 100, 'meter_id': 'A'}
                for d in range(1, 6)]
        self.ingest('base', base)
        late = [{'reservoir_id': 'RW-02', 'reading_date': '2026-11-06', 'level_m': 10.05, 'meter_id': 'B'},
                {'reservoir_id': 'RW-02', 'reading_date': '2026-11-06', 'level_m': 1.05, 'meter_id': 'A'}]
        _id, version, changed = self.ingest('late dual', late)
        self.assertTrue(changed)
        self.assertEqual(version, 2)
        with self.Session() as db:
            first = ledgers.version(db, 1, 1)
            second = ledgers.version(db, 1, 2)
            self.assertEqual(len(first.snapshot['records']), 5)  # old version unchanged
            self.assertEqual(second.snapshot['handovers'][0]['offset_m'], 9.0)
            self.assertEqual(second.changes['handovers_before'], 0)
            self.assertEqual(second.changes['handovers_after'], 1)

    def test_runs_pin_a_version_and_recomputation_is_idempotent(self):
        base = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 1.0 + d / 100, 'meter_id': 'A'}
                for d in range(1, 8)]
        dataset_id, _version, _changed = self.ingest('base', base)
        with self.Session.begin() as db:
            run = Run(name='first', dataset_id=dataset_id, params={}, author='analyst')
            db.add(run)
            db.flush()
            pinned = runs_ledger.pin(db, run, dataset_id)
            run_id = run.id
            self.assertEqual(pinned.version, 1)
        supplement = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 1.0 + d / 100, 'meter_id': 'A'}
                      for d in (8, 9)]
        _id, version, _changed = self.ingest('supplement', supplement)
        with self.Session.begin() as db:
            head = db.scalar(select(LedgerHead))
            new_version = ledgers.version(db, head.id, version)
            created = recompute_affected(db, head, new_version, 'analyst')
            self.assertEqual(len(created), 1)
            again = recompute_affected(db, head, new_version, 'analyst')
            self.assertEqual(again, [])
        with self.Session() as db:
            child = db.get(Run, created[0].id)
            link = db.get(RunLedger, child.id)
            self.assertEqual(link.version_id, new_version.id)
            self.assertEqual(len(db.scalars(select(Recompute)).all()), 1)
            # The original run stays pinned to its original version.
            original_link = db.get(RunLedger, run_id)
            self.assertEqual(original_link.version_id, new_version.id - 1)

    def test_out_of_order_supplement_rebuilds_only_affected_segments(self):
        base = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 1.0 + d / 100, 'meter_id': 'A'}
                for d in range(1, 6)]
        self.ingest('base', base)
        switched = [{'reservoir_id': 'RW-02', 'reading_date': f'2026-11-{d:02d}', 'level_m': 9.0 + 1.0 + d / 100, 'meter_id': 'B'}
                    for d in range(6, 11)]
        self.ingest('switch without dual', switched)
        with self.Session() as db:
            head = db.scalar(select(LedgerHead))
            before = ledgers.current(db, head.id)
            self.assertIsNotNone(before.snapshot['isolation'])
        dual = [{'reservoir_id': 'RW-02', 'reading_date': '2026-11-06', 'level_m': 1.06, 'meter_id': 'A'},
                {'reservoir_id': 'RW-02', 'reading_date': '2026-11-06', 'level_m': 10.06, 'meter_id': 'B'}]
        _id, version, _changed = self.ingest('late dual', dual)
        with self.Session() as db:
            head = db.scalar(select(LedgerHead))
            after = ledgers.current(db, head.id)
            self.assertIsNone(after.snapshot['isolation'])
            self.assertEqual(after.changes['isolation_before'],
                             {'date': '2026-11-06',
                              'reason': '该次换表缺少同日双表读数，无法计算刻度偏差'})
            self.assertIsNone(after.changes['isolation_after'])
            touched = ledger_domain.affected_segments(before.snapshot, after.snapshot)
            self.assertTrue(touched)
            # The late dual only rebuilds the segment that contains the handover
            # date; the untouched prefix keeps its earlier boundaries.
            self.assertTrue(any(segment['start'] <= '2026-11-06' <= segment['end'] for segment in touched))
            self.assertEqual(after.snapshot['segments'][0]['start'], '2026-11-01')


if __name__ == '__main__':
    unittest.main()

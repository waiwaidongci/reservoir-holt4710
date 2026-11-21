import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze_snapshot
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.ledger import LedgerHead, LedgerVersion, Recompute, RunLedger
from src.models.source import Dataset
from src.services import runs_ledger
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            run = Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                      name=item.name, params=params, author=item.author)
            db.add(run)
            db.flush()
            # Every run pins the ledger version current when it is queued.
            runs_ledger.pin(db, run, dataset_id)


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        link = db.get(RunLedger, job.id)
        version = db.get(LedgerVersion, link.version_id) if link else None
        if version is None:
            # Legacy or unpinned run: fall back to the batch as imported.
            snapshot = None
            rows = db.get(Dataset, job.dataset_id).rows
        else:
            snapshot = version.snapshot
            rows = None
        return job.id, job.attempt, rows, job.params, snapshot


def execute(lease):
    job_id, attempt, rows, params, snapshot = lease
    result = export = failure = None
    try:
        result = analyze_snapshot(snapshot, params) if snapshot is not None else _analyze_rows(rows, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            # A retried attempt must not create a second receipt.
            if db.get(Delivery, job.id) is None:
                ready = not failure and result.get('ready', True)
                detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
                receipt = Delivery(run_id=job.id, recipient=plan.recipient,
                                   state='delivered' if ready else 'blocked', detail=detail[:500])
                db.add(receipt)
                record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)


def _analyze_rows(rows, params):
    from src.domain.analysis import analyze
    return analyze(rows, params)


def _overlaps(segment, ranges):
    return any(segment['start'] <= end and start <= segment['end'] for start, end in ranges)


def _changed_ranges(changes):
    ranges = []
    for key in ('added', 'removed', 'changed'):
        for segment in changes.get(key, []):
            ranges.append((segment['start'], segment['end']))
    return ranges


def recompute_affected(db, head, version, actor):
    """Queue fresh runs for the newest forecast of each lineage this version touched.

    Old runs are never rewritten: each lineage gets one new run pinned to the new
    version, so the earlier forecast still shows the readings and offsets it used.
    A ``recomputes`` row per (parent run, version) keeps repeated imports of the
    same supplement idempotent, and feeding a recomputed run back in never starts
    a chain because only the lineage's newest run is re-forecast.
    """
    changes = version.changes or {}
    ranges = _changed_ranges(changes)
    isolation_changed = changes.get('isolation_before') != changes.get('isolation_after')
    values_changed = bool(changes.get('value_drift'))
    if not ranges and not isolation_changed and not values_changed:
        return []
    links = db.scalars(select(RunLedger).where(RunLedger.ledger_id == head.id,
                                               RunLedger.version_id != version.id)).all()
    # Choose the newest run per lineage: following Recompute.parent_run_id back to
    # the root, a lineage's most recent run already reflects all earlier versions.
    parents = {row.run_id: row.parent_run_id for row in db.scalars(select(Recompute))}
    newest = {}
    for link in links:
        root = link.run_id
        seen = set()
        while root in parents and root not in seen:
            seen.add(root)
            root = parents[root]
        if root not in newest or link.run_id > newest[root]:
            newest[root] = link.run_id
    created = []
    for root, run_id in sorted(newest.items()):
        link = db.get(RunLedger, run_id)
        previous = db.get(LedgerVersion, link.version_id)
        if previous is None:
            continue
        if ranges and not any(_overlaps(segment, ranges) for segment in previous.snapshot['segments']):
            # A value-only change (for example a re-derived offset) affects every
            # later segment, so it always re-forecasts the newest lineage run.
            if not isolation_changed and not values_changed:
                continue
        if db.scalar(select(Recompute).where(Recompute.parent_run_id == run_id,
                                             Recompute.version_id == version.id)):
            continue
        source = db.get(Run, run_id)
        if source is None:
            continue
        root_name = source.name.split(' · 补录重算')[0]
        child = Run(dataset_id=source.dataset_id, name=f'{root_name} · 补录重算 v{version.version}',
                    params=source.params, author=actor)
        db.add(child)
        db.flush()
        db.add(RunLedger(run_id=child.id, ledger_id=head.id, version_id=version.id))
        db.add(Recompute(parent_run_id=run_id, run_id=child.id, version_id=version.id))
        record(db, actor, 'analysis.recomputed', child.id, parent=run_id,
               ledger_version=version.version)
        created.append(child)
    return created

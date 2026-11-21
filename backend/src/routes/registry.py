from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.ledger import LedgerHead, LedgerVersion
from src.models.source import Dataset
from src.schemas.requests import Upload
from src.services import ledgers, runs_ledger
from src.services.audit import record
from src.services.datasets import handover_template, save, template
from src.services.execution import recompute_affected
from src.services.validation import parse_rows
from src.utils import views
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/spec')
def spec():
    return {key: value for key, value in SPEC.items() if key != 'fixture'}


@router.get('/template')
def sample(actor: User = Depends(current)):
    filename = quote(SPEC['sample_name'])
    return Response(template(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})


@router.get('/template/handover')
def handover_sample(actor: User = Depends(current)):
    filename = quote('澄湾_RW-02_换表交接示例.csv')
    return Response(handover_template(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})


@router.get('/datasets')
def datasets(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.dataset(item) for item in db.scalars(select(Dataset).order_by(Dataset.id.desc()).limit(100))]


@router.get('/datasets/{dataset_id}')
def details(dataset_id: int, offset: int = Query(0, ge=0), limit: int = Query(16, ge=1, le=100),
            db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(Dataset, dataset_id)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    link = ledgers.version_for_dataset(db, dataset_id)
    return {**views.dataset(item), 'rows': item.rows[offset:offset + limit],
            'ledger': runs_ledger.version_view(link) if link else None}


@router.post('/datasets', status_code=201)
def upload(body: Upload, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    rows = parse_rows(body.csv_text)
    item, created = save(db, body.name, body.filename, rows, actor.username)
    if created:
        record(db, actor.username, 'dataset.imported', item.id, rows=item.row_count)
    head, version, changed = ledgers.ensure(db, rows, actor.username,
                                            f"导入批次 {item.id} · {body.name}")
    ledgers.register(db, item, head, version, changed)
    recomputed = []
    if changed:
        record(db, actor.username, 'ledger.version', version.id, version=version.version,
               ledger=head.reservoir_id, dataset=item.id, checksum=version.checksum)
        recomputed = recompute_affected(db, head, version, actor.username)
    db.commit()
    if recomputed:
        wake()
    return {**views.dataset(item), 'created': created, 'ledger': runs_ledger.version_view(version),
            'ledger_changed': changed,
            'recomputed': [{'id': run.id, 'parent': run.name} for run in recomputed]}


@router.get('/ledgers')
def ledger_heads(db: Session = Depends(connection), actor: User = Depends(current)):
    return [{'id': head.id, 'reservoir_id': head.reservoir_id, 'version': head.version,
             'current_version_id': head.current_version_id, 'rows': len(head.rows or []),
             'updated_at': iso(head.updated_at)}
            for head in db.scalars(select(LedgerHead).order_by(LedgerHead.id))]


@router.get('/ledgers/{ledger_id}/versions')
def ledger_versions(ledger_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    versions = db.scalars(select(LedgerVersion).where(LedgerVersion.ledger_id == ledger_id)
                          .order_by(LedgerVersion.version.desc()).limit(50))
    return [{'id': version.id, 'version': version.version, 'checksum': version.checksum,
             'created_at': iso(version.created_at),
             'changes': version.changes, 'note': version.note, 'author': version.author}
            for version in versions]


@router.get('/ledgers/{ledger_id}/versions/{version_number}')
def ledger_version(ledger_id: int, version_number: int, db: Session = Depends(connection),
                   actor: User = Depends(current)):
    version = ledgers.version(db, ledger_id, version_number)
    if version is None:
        raise HTTPException(404, '找不到该台账版本')
    return runs_ledger.version_view(version)

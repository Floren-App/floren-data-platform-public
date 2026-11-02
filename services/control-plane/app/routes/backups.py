"""Backup configuration and inventory; restore is an offline operator action."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from .. import backups, permissions, rbac
from ..app_profiles import acting_as_admin
from ..db import get_db
from ..deps import get_actor
from ..models import PlatformBackup, PlatformSetting
from .common import audit

router = APIRouter(prefix='/v1/backups', tags=['platform recovery'])


def require(db, user, action):
    from ..app_profiles import interactive
    from .. import page_access
    user = rbac.resolve_user(db, user)
    if interactive(user) and not page_access.allowed(db, user, 'set-backups'):
        raise HTTPException(403, 'Viewer on Backup and recovery required')
    return permissions.require(db, user, action, 'set-backups')


def admin(db, user):
    if not acting_as_admin(rbac.resolve_user(db, user)):
        raise HTTPException(403, 'active Global Admin required for recovery configuration')


def output(row):
    return {k:getattr(row, k) for k in ('id','created_at','finished_at','actor','status',
        'bucket','object_key','sha256','size_bytes','error')}


@router.get('/configuration')
def read(db=Depends(get_db), user=Depends(get_actor)):
    require(db, user, 'backups.configuration.read')
    return backups.configuration(db).model_dump()


@router.put('/configuration')
def configure(body: backups.BackupConfig, db=Depends(get_db), user=Depends(get_actor)):
    admin(db, user)
    require(db, user, 'backups.configuration.update')
    if body.enabled:
        backups.destination(db, body)
    row = db.get(PlatformSetting, backups.SETTING)
    if row is None:
        row = PlatformSetting(key=backups.SETTING, value={}); db.add(row)
    row.value = body.model_dump()
    audit(db, user.username, 'backup.configuration.update', 'set-backups',
          enabled=body.enabled, source_id=body.source_id,
          key_id=backups.envelope.fingerprint(backups.envelope.public_key(body.recovery_public_key)) if body.recovery_public_key else '')
    db.commit()
    return row.value


@router.delete('/configuration', status_code=204)
def disable(db=Depends(get_db), user=Depends(get_actor)):
    admin(db, user)
    require(db, user, 'backups.configuration.update')
    row = db.get(PlatformSetting, backups.SETTING)
    if row:
        row.value = {**row.value, 'enabled':False}
    audit(db, user.username, 'backup.configuration.disable', 'set-backups')
    db.commit()  # Never delete existing archives or forget recovery inventory.


@router.get('')
def history(db=Depends(get_db), user=Depends(get_actor), limit: int = 50):
    require(db, user, 'backups.history.read')
    if not 1 <= limit <= 200:
        raise HTTPException(422, 'limit must be between 1 and 200')
    return [output(r) for r in db.scalars(select(PlatformBackup).order_by(PlatformBackup.created_at.desc()).limit(limit))]


@router.post('', status_code=202)
def create(db=Depends(get_db), user=Depends(get_actor)):
    require(db, user, 'backups.create')
    row = backups.queue(db, user.username)
    audit(db, user.username, 'backup.create', row.id)
    db.commit()
    return output(row)


@router.get('/recovery')
def recovery(db=Depends(get_db), user=Depends(get_actor)):
    admin(db, user)
    row = db.get(PlatformSetting, backups.QUARANTINE)
    if backups.incomplete_restore(db):
        return {'blocked':True,'incomplete_restore':True,'reason':'Offline recovery did not finalize; use another empty recovery target'}
    return row.value if row else {'blocked':False}


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    directory_reconciled: bool
    permissions_reviewed: bool
    expired_access_reconciled: bool
    secrets_and_data_verified: bool


@router.post('/recovery/resume')
def resume(body: ResumeIn, db=Depends(get_db), user=Depends(get_actor)):
    admin(db, user)
    require(db, user, 'backups.recovery.resume')
    if not all(body.model_dump().values()):
        raise HTTPException(422, 'complete all recovery checks before resuming the installation')
    row = db.get(PlatformSetting, backups.QUARANTINE)
    if backups.incomplete_restore(db):
        raise HTTPException(409, 'offline recovery did not finalize; an incomplete restore cannot be approved')
    if not row or not row.value.get('blocked'):
        raise HTTPException(409, 'installation is not in recovery quarantine')
    row.value = {**row.value, 'blocked':False, 'approved_by':user.username}
    audit(db, user.username, 'backup.recovery.resume', 'set-backups', checks=body.model_dump())
    db.commit()
    return {'restart_required':True, 'message':'Restart all platform controllers and API replicas before admitting traffic.'}

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


def require(db, user, action, target="set-backups"):
    from ..app_profiles import interactive
    from .. import page_access
    user = rbac.resolve_user(db, user)
    if interactive(user) and not page_access.allowed(db, user, 'set-backups'):
        raise HTTPException(403, 'Viewer on Backup and recovery required')
    return permissions.require(db, user, action, target)


def admin(db, user):
    if not acting_as_admin(rbac.resolve_user(db, user)):
        raise HTTPException(403, 'active Global Admin required for recovery configuration')


def output(row):
    return {**{k:getattr(row, k) for k in ('id','created_at','finished_at','actor','status',
        'bucket','object_key','sha256','size_bytes','error')}, 'plan_id':row.destination.get('_plan_id'),
        'source_kind':row.destination.get('_source',{}).get('kind','control_plane'),
        'source_name':row.destination.get('_source',{}).get('name','Control plane')}


def plan_output(row,db,user):
    ident=row.key[len(plans.PLAN):]
    actions=('backups.plans.read','backups.plans.update','backups.plans.schedule.change','backups.plans.source.change',
        'backups.plans.destination.change','backups.plans.retention.change','backups.plans.delete','backups.plans.run',
        'backups.history.read','backups.archives.delete')
    return {**plans.output(row),'capabilities':[a for a in actions if permissions.allowed(db,user,a,ident)]}


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
    return [output(r) for r in db.scalars(select(PlatformBackup).order_by(PlatformBackup.created_at.desc()).limit(limit))
            if not r.destination.get('_plan_id') or permissions.allowed(db,user,'backups.history.read',r.destination['_plan_id'])]


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


# Independent plans coexist with the legacy single control-plane configuration.
from .. import backup_plans as plans
from ..models import DataSource, Resource, new_id


def view_page(db,user):
    from .. import page_access
    user=rbac.resolve_user(db,user)
    if not page_access.allowed(db,user,'set-backups'):
        raise HTTPException(403,'Viewer on Backup and recovery required')
    return user


@router.get('/sources')
def sources(db=Depends(get_db),user=Depends(get_actor)):
    user=view_page(db,user);rows=[]
    if acting_as_admin(user):rows.append({'id':'','name':'Control plane','kind':'control_plane','supported':True,'detail':'Metadata and vault secret references; vault contents are not archived.'})
    for source in db.scalars(select(DataSource)):
        resource=rbac.source_resource(db,source)
        if permissions.allowed(db,user,'backups.sources.export',resource.id):
            rows.append({'id':source.id,'name':source.name,'kind':source.kind,'supported':source.kind in ('postgres','s3','iceberg'),
                'detail':'Captured Iceberg v1/v2 metadata and all retained snapshot files; per-table consistency' if source.kind=='iceberg' else 'Consistent native database dump' if source.kind=='postgres' else 'Current objects; per-object conditional reads' if source.kind=='s3' else 'Requires a coordinated native backup adapter; not available yet.'})
    return rows


@router.get('/destinations')
def destinations(db=Depends(get_db),user=Depends(get_actor)):
    user=view_page(db,user)
    return [plans.output(r) for r in plans.rows(db,plans.DESTINATION)
            if not r.value.get('_deleted') and plans.allowed(db,user,'backups.destinations.read',r.key[len(plans.DESTINATION):])]


@router.post('/destinations',status_code=201)
def create_destination(body: plans.DestinationIn,db=Depends(get_db),user=Depends(get_actor)):
    admin(db,user);require(db,user,'backups.destinations.create')
    ident=new_id();value=body.model_dump()
    from ..deps import get_secret_store
    config=body.backup_config();config.recovery_public_key=get_secret_store().read(body.recovery_key_ref).get('public_key','')
    try:
        backups.destination(db,config)
    except ValueError as exc:
        raise HTTPException(422,'invalid backup destination or vault recovery key') from exc
    row=PlatformSetting(key=plans.DESTINATION+ident,value=value);db.add(row)
    db.add(Resource(id=ident,kind='backup-dest',name=body.name,config={}))
    audit(db,user.username,'backup.destination.create',ident);db.commit();return plans.output(row)


@router.get('/destinations/{ident}')
def destination_detail(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.destinations.read',ident)
    return plans.output(plans.get(db,plans.DESTINATION,ident))


@router.put('/destinations/{ident}')
def update_destination(ident: str,body: plans.DestinationIn,db=Depends(get_db),user=Depends(get_actor)):
    admin(db,user);require(db,user,'backups.destinations.update',ident)
    row=plans.get(db,plans.DESTINATION,ident,True)
    from ..deps import get_secret_store
    config=body.backup_config();config.recovery_public_key=get_secret_store().read(body.recovery_key_ref).get('public_key','')
    try:
        backups.destination(db,config)
    except ValueError as exc:
        raise HTTPException(422,'invalid backup destination or vault recovery key') from exc
    row.value=body.model_dump();db.get(Resource,ident).name=body.name
    audit(db,user.username,'backup.destination.update',ident);db.commit();return plans.output(row)


@router.delete('/destinations/{ident}',status_code=204)
def delete_destination(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    admin(db,user);require(db,user,'backups.destinations.delete',ident)
    row=plans.get(db,plans.DESTINATION,ident,True)
    if any(r.value['destination_id']==ident and not r.value.get('_deleted') for r in plans.rows(db,plans.PLAN)):
        raise HTTPException(409,'remove or move plans referencing this destination first')
    row.value={**row.value,'_deleted':True}
    audit(db,user.username,'backup.destination.delete',ident);db.commit()


@router.get('/plans')
def list_plans(db=Depends(get_db),user=Depends(get_actor)):
    user=view_page(db,user)
    return [plan_output(r,db,user) for r in plans.rows(db,plans.PLAN) if not r.value.get('_deleted') and
            plans.allowed(db,user,'backups.plans.read',r.key[len(plans.PLAN):])]


@router.post('/plans',status_code=201)
def create_plan(body: plans.PlanIn,db=Depends(get_db),user=Depends(get_actor)):
    row=plans.save_plan(db,user,body);audit(db,user.username,'backup.plan.create',row.key[len(plans.PLAN):]);db.commit()
    return plan_output(row,db,user)


@router.get('/plans/{ident}')
def plan_detail(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.plans.read',ident)
    return plan_output(plans.get(db,plans.PLAN,ident),db,user)


@router.put('/plans/{ident}')
def update_plan(ident: str,body: plans.PlanIn,db=Depends(get_db),user=Depends(get_actor)):
    row=plans.save_plan(db,user,body,ident);audit(db,user.username,'backup.plan.update',ident);db.commit();return plan_output(row,db,user)


@router.delete('/plans/{ident}',status_code=204)
def delete_plan(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.plans.delete',ident);row=plans.get(db,plans.PLAN,ident,True)
    row.value={**row.value,'enabled':False,'_deleted':True}
    audit(db,user.username,'backup.plan.delete',ident);db.commit()  # Archive inventory and objects are retained.


@router.post('/plans/{ident}/runs',status_code=202)
def run_plan(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.plans.run',ident)
    row=plans.queue_plan(db,rbac.resolve_user(db,user),ident);audit(db,user.username,'backup.plan.run',row.id,plan_id=ident);db.commit()
    return {**output(row),'plan_id':ident}


@router.get('/plans/{ident}/runs')
def plan_history(ident: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.history.read',ident)
    return [output(r) for r in db.scalars(select(PlatformBackup).where(
        PlatformBackup.destination['_plan_id'].as_string()==ident).order_by(PlatformBackup.created_at.desc()).limit(200))]


@router.delete('/plans/{ident}/runs/{run_id}',status_code=204)
def delete_archive(ident: str,run_id: str,db=Depends(get_db),user=Depends(get_actor)):
    require(db,user,'backups.archives.delete',ident)
    row=db.scalar(select(PlatformBackup).where(PlatformBackup.id==run_id).with_for_update())
    if not row or row.destination.get('_plan_id')!=ident:
        raise HTTPException(404,'backup archive not found')
    if row.status=='expired':return
    if row.status!='succeeded':raise HTTPException(409,'only completed verified archives can be deleted')
    permissions.require(db,user,'backups.destinations.use',row.destination['_plan']['destination_id'])
    from ..deps import get_secret_store
    try:
        client=backups.client({**row.destination,'_allow_delete':True},get_secret_store(),db=db)
        args={'Bucket':row.bucket,'Key':row.object_key}
        if row.destination.get('_archive_version_id'):args['VersionId']=row.destination['_archive_version_id']
        client.delete_object(**args)
    except Exception:
        raise HTTPException(409,'archive deletion failed; check retention lock and destination permissions') from None
    row.status='expired';audit(db,user.username,'backup.archive.delete',row.id,plan_id=ident);db.commit()

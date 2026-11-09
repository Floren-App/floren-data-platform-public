"""Independent backup policies. Settings rows hold policies; archives retain frozen inputs."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from . import backups, permissions, rbac
from .auth import User
from .app_profiles import acting_as_admin
from .models import PlatformSetting, PlatformBackup, DataSource, Resource, new_id, utcnow

PLAN = 'backup.plan.'
DESTINATION = 'backup.destination.'


class DestinationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    bucket: str = Field(default='', max_length=63)
    prefix: str = Field(default='backups', min_length=1, max_length=200)
    endpoint_url: str = ''
    region: str = 'us-east-1'
    source_id: str = Field(default='', max_length=12)
    recovery_key_ref: str = Field(min_length=1, max_length=256)
    server_side_encryption: str = ''
    kms_key_id: str = ''
    object_lock_days: int = Field(default=0, ge=0, le=36500)

    @model_validator(mode='after')
    def validate_destination(self):
        from .storage_access import prefix_path
        if prefix_path('',self.recovery_key_ref) != self.recovery_key_ref:
            raise ValueError('use a vault secret path for the recovery key')
        self.backup_config()
        return self

    def backup_config(self):
        return backups.BackupConfig(**self.model_dump(exclude={'name','recovery_key_ref'}))


class PlanIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    source_kind: Literal['control_plane', 'storage'] = 'control_plane'
    source_id: str = Field(default='', max_length=12)
    destination_id: str = Field(min_length=1, max_length=12)
    prefix: str = Field(default='daily', min_length=1, max_length=200)
    schedule: str = Field(default='0 2 * * *', max_length=120)
    timezone: str = Field(default='UTC', max_length=80)
    schemas: List[str] = Field(default_factory=list, max_length=100)
    source_prefix: str = Field(default='', max_length=512)
    retention_days: int = Field(default=30, ge=0, le=36500)
    max_bytes: int = Field(default=10737418240, ge=1, le=1099511627776)
    max_objects: int = Field(default=100000, ge=1, le=1000000)

    @model_validator(mode='after')
    def validate_plan(self):
        from croniter import croniter
        if len(self.schedule.split()) != 5 or not croniter.is_valid(self.schedule):
            raise ValueError('use a five-field cron schedule')
        try:
            ZoneInfo(self.timezone)
            next_due(self, utcnow())
        except (ValueError, KeyError) as exc:
            raise ValueError('invalid timezone or schedule without a run in the next five years') from exc
        # The bounded search also rejects impossible schedules.
        backups.BackupConfig(prefix=self.prefix)
        from .storage_access import prefix_path
        prefix_path('', self.source_prefix)
        if self.source_kind == 'control_plane' and (self.source_id or self.schemas or self.source_prefix):
            raise ValueError('control-plane plans cannot select storage objects')
        if self.source_kind == 'storage' and not self.source_id:
            raise ValueError('choose a storage integration')
        if any(not s or '\x00' in s or len(s)>255 for s in self.schemas):
            raise ValueError('invalid schema or namespace name')
        return self


def next_due(plan, after):
    from croniter import croniter
    return croniter(plan.schedule, after.astimezone(ZoneInfo(plan.timezone)),
                    max_years_between_matches=5).get_next(datetime).astimezone(timezone.utc)


def upcoming(schedule, tz, count=5):
    """The next runs of a schedule, computed exactly as the scheduler does (daylight-saving changes included)."""
    from croniter import croniter
    if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
        raise ValueError('use a five-field cron schedule')
    plan = type('Schedule', (), {'schedule': schedule, 'timezone': tz})()
    ZoneInfo(tz)
    runs, after = [], utcnow()
    for _ in range(count):
        after = next_due(plan, after)
        runs.append(after)
    return runs


def status(db, ident, enabled):
    """What a plan is doing: paused, running, failed or scheduled, and when it last completed. A completed backup is a
    verified archive (checksum), not evidence that a restore works."""
    history = list(db.scalars(select(PlatformBackup).where(PlatformBackup.destination['_plan_id'].as_string() == ident)
                              .order_by(PlatformBackup.created_at.desc()).limit(50)))
    latest = history[0] if history else None
    success = next((r for r in history if r.status == 'succeeded'), None)
    state = ('running' if latest and latest.status in ('queued', 'running') else 'paused' if not enabled
             else 'failed' if latest and latest.status == 'failed' else 'scheduled')
    return {'state': state, 'last_status': latest.status if latest else None,
            'last_run_at': (latest.finished_at or latest.created_at).isoformat() if latest else None,
            'last_success_at': success.finished_at.isoformat() if success and success.finished_at else None,
            'last_error': latest.error if latest and latest.status == 'failed' else ''}


def prerequisites(db, user, plan):
    """Checks a plan's source, destination and vault key reference with the existing adapters, changing nothing:
    no bucket policy, retention, encryption, credentials or schedule. Each result says what was checked."""
    checks = []
    def check(key, label, work, ok):
        try:
            work()
            checks.append({'key': key, 'label': label, 'state': 'ready', 'detail': ok})
        except HTTPException as exc:
            checks.append({'key': key, 'label': label, 'state': 'missing', 'detail': str(exc.detail)})
        except Exception as exc:  # an adapter or vault answer we could not use
            checks.append({'key': key, 'label': label, 'state': 'unavailable', 'detail': str(exc) or exc.__class__.__name__})
    def source_supported():
        value = authorize_source(db, user, plan)
        if value['kind'] not in ('control_plane', 'postgres', 's3', 'iceberg'):
            raise HTTPException(422, f"{value['kind']} integrations have no backup adapter yet")
    check('source', 'Source', source_supported, 'You may export it and it has a backup adapter.')
    def key():
        row = get(db, DESTINATION, plan.destination_id)
        from .deps import get_secret_store
        from .recovery_archive import public_key
        pem = get_secret_store().read(row.value['recovery_key_ref']).get('public_key', '')
        if not pem:
            raise HTTPException(422, 'the vault secret has no public_key field')
        public_key(pem)
    check('recovery_key', 'Recovery key in the vault', key, 'The referenced secret holds an RSA public key of at least 3072 bits.')
    check('destination', 'Destination', lambda: resolve_destination(db, user, plan),
          'You may use it, its location is valid and outside the source, and its retention fits its Object Lock.')
    return checks


def get(db, kind, ident, lock=False):
    stmt = select(PlatformSetting).where(PlatformSetting.key == kind+ident)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(stmt)
    if row is None or row.value.get('_deleted'):
        raise HTTPException(404, 'backup plan or destination not found')
    return row


def output(row):
    return {'id':row.key.rsplit('.',1)[-1], **{k:v for k,v in row.value.items() if not k.startswith('_')},
            **({'next_run_at':row.value.get('_next_run_at'),'layer_id':row.value.get('_layer_id')} if row.key.startswith(PLAN) else {})}


def rows(db, kind):
    return [r for r in db.scalars(select(PlatformSetting).where(PlatformSetting.key.startswith(kind)).order_by(PlatformSetting.key)) if not r.value.get('_deleted')]


def allowed(db, user, action, ident='set-backups'):
    return permissions.allowed(db, user, action, ident)


def require(db, user, action, ident='set-backups'):
    from .routes.backups import require as page_require
    page_require(db, user, action, target=ident)
    return permissions.require(db, user, action, ident)


def source(db, plan):
    if plan.source_kind == 'control_plane':
        return {'kind':'control_plane'}
    item = db.get(DataSource, plan.source_id)
    if not item:
        raise HTTPException(422, 'storage integration not found')
    if item.kind not in ('postgres','s3','iceberg'):
        raise HTTPException(422, 'this integration needs a native backup adapter; PostgreSQL, S3 files and Iceberg are supported')
    if item.kind=='postgres' and any(len(s)>63 for s in plan.schemas):
        raise HTTPException(422,'PostgreSQL schema names cannot exceed 63 characters')
    if item.kind in ('postgres','iceberg') and plan.source_prefix:
        raise HTTPException(422, 'Database and Iceberg plans select schemas/namespaces, not a file prefix')
    if item.kind == 's3' and plan.schemas:
        raise HTTPException(422, 'S3 plans select a prefix, not schemas')
    return {'kind':item.kind, 'id':item.id, 'name':item.name, 'config':dict(item.config),
            'schemas':plan.schemas, 'source_prefix':plan.source_prefix,'max_bytes':plan.max_bytes,'max_objects':plan.max_objects}


def authorize_source(db, user, plan):
    value = source(db, plan)
    if value['kind']=='control_plane':
        if not acting_as_admin(rbac.resolve_user(db,user)):
            raise HTTPException(403, 'active Global Admin required for control-plane backups')
    else:
        target = rbac.source_resource(db, db.get(DataSource, plan.source_id))
        permissions.require(db, user, 'backups.sources.export', target.id)
    return value


def resolve_destination(db, user, plan):
    row = get(db, DESTINATION, plan.destination_id)
    permissions.require(db, user, 'backups.destinations.use', plan.destination_id)
    config = DestinationIn.model_validate({k:v for k,v in row.value.items() if not k.startswith('_')}).backup_config()
    config.prefix += '/'+plan.prefix
    from .deps import get_secret_store
    recovery_key=get_secret_store().read(row.value['recovery_key_ref'])
    config.recovery_public_key=recovery_key.get('public_key','')
    try:
        result = backups.destination(db, config)
    except ValueError as exc:
        raise HTTPException(422, 'invalid backup destination or vault recovery key') from exc
    result['_recovery_key_ref']=row.value['recovery_key_ref']
    if plan.retention_days and plan.retention_days < config.object_lock_days:
        raise HTTPException(422, 'retention cannot be shorter than destination Object Lock')
    origin = source(db, plan)
    if origin['kind'] in ('s3','iceberg'):
        from .storage_access import prefix_path
        cfg=dict(origin['config'])
        if origin['kind']=='iceberg':
            from urllib.parse import urlsplit
            warehouse=urlsplit(cfg['warehouse']);cfg.update(bucket=warehouse.netloc,prefix=warehouse.path.strip('/'))
        prefix=prefix_path(cfg.get('prefix',''),plan.source_prefix)
        if cfg.get('bucket')==result['bucket'] and cfg.get('endpoint_url','').rstrip('/')==result.get('endpoint_url','').rstrip('/'):
            # A backup must not recursively include its own output or another source subtree.
            if not prefix or result['prefix']==prefix or result['prefix'].startswith(prefix+'/'):
                raise HTTPException(422, 'backup output must be outside the source prefix')
    return result


def save_plan(db, user, body, ident=None):
    user=rbac.resolve_user(db,user)
    old=get(db,PLAN,ident,True) if ident else None
    if old:
        before=PlanIn.model_validate({k:v for k,v in old.value.items() if not k.startswith('_')})
        require(db,user,'backups.plans.update',ident)
        for fields,action in [(('schedule','timezone','enabled'),'backups.plans.schedule.change'),
                (('source_kind','source_id','schemas','source_prefix','max_bytes','max_objects'),'backups.plans.source.change'),
                (('destination_id','prefix'),'backups.plans.destination.change'),
                (('retention_days',),'backups.plans.retention.change')]:
            if any(getattr(before,k)!=getattr(body,k) for k in fields):
                permissions.require(db,user,action,ident)
    else:
        from .routes.backups import view_page
        view_page(db,user)
        target=rbac.source_resource(db,db.get(DataSource,body.source_id)).id if body.source_kind=='storage' and db.get(DataSource,body.source_id) else 'set-backups'
        layer=permissions.resource_layer(db,target)
        if not (permissions.allowed(db,user,'backups.plans.create',target) or permissions.allowed(db,user,'backups.plans.create','set-backups',layer)):
            raise HTTPException(403,'backups.plans.create permission required in the source scope')
    value=authorize_source(db,user,body)
    resolve_destination(db,user,body)
    source_target=rbac.source_resource(db,db.get(DataSource,body.source_id)).id if body.source_kind=='storage' else 'set-backups'
    source_layer=permissions.resource_layer(db,source_target)
    if body.retention_days and not (permissions.allowed(db,user,'backups.archives.delete',ident or source_target) or
            (not ident and permissions.allowed(db,user,'backups.archives.delete','set-backups',source_layer))):
        raise HTTPException(403,'backups.archives.delete permission required for retention in this source scope')
    if old is not None and permissions.resource_layer(db,ident)!=source_layer:
        raise HTTPException(409,'create a new backup plan when changing layers; existing history retains its scope')
    ident=ident or new_id()
    if old is None:
        old=PlatformSetting(key=PLAN+ident,value={});db.add(old)
        db.add(Resource(id=ident,kind='backup-plan',name=body.name,parent_id=source_target,config={'layer_id':source_layer or ''}))
    else:
        db.get(Resource,ident).name=body.name
        db.get(Resource,ident).config={'layer_id':source_layer or ''}
        db.get(Resource,ident).parent_id=source_target
    old.value={**body.model_dump(), '_layer_id':source_layer, '_actor':{'username':user.username,'principal_id':user.principal_id,
        'active_group_id':user.active_group_id}, '_next_run_at':next_due(body,utcnow()).isoformat()}
    return old


def queue_plan(db, user, ident, scheduled=False):
    row=get(db,PLAN,ident,True)
    plan=PlanIn.model_validate({k:v for k,v in row.value.items() if not k.startswith('_')})
    permissions.require(db,user,'backups.plans.run',ident)
    if scheduled and not plan.enabled:
        raise HTTPException(409,'backup plan is paused')
    frozen=authorize_source(db,user,plan)
    dest=resolve_destination(db,user,plan)
    if db.scalar(select(PlatformBackup).where(PlatformBackup.status.in_(['queued','running']),
            PlatformBackup.destination['_plan_id'].as_string()==ident)):
        raise HTTPException(409,'this plan already has an active backup')
    dest.update(_plan_id=ident,_source=frozen,_actor={'username':user.username,'principal_id':user.principal_id,'active_group_id':user.active_group_id},_retention_days=plan.retention_days,_plan=plan.model_dump())
    result=PlatformBackup(actor=user.username,destination=dest);db.add(result);db.flush()
    return result


def schedule(factory):
    with factory() as db:
        for found in rows(db,PLAN):
            row=get(db,PLAN,found.key[len(PLAN):],True)
            if not row.value['enabled'] or datetime.fromisoformat(row.value['_next_run_at'])>utcnow():
                continue
            plan=PlanIn.model_validate({k:v for k,v in row.value.items() if not k.startswith('_')})
            try:
                with db.begin_nested():
                    queue_plan(db,rbac.resolve_user(db,User(**row.value['_actor'])),row.key[len(PLAN):],True)
            except HTTPException as exc:
                from .models import AuditEvent
                db.add(AuditEvent(actor='system:backup-scheduler',action='backup.schedule.rejected',target=row.key[len(PLAN):],detail={'status':exc.status_code}))
            # Coalesce missed occurrences, never replay a backlog after downtime.
            row.value={**row.value,'_next_run_at':next_due(plan,utcnow()).isoformat()}
            db.commit()


def finish(db, row, store):
    from . import notify
    from .models import AuditEvent
    ident=row.destination['_plan_id']
    notify.queue(db,'backup.'+row.status,[], 'Backup '+row.status,
                 'Backup plan '+ident+' completed with status '+row.status+'.',
                 key='backup:'+row.id,resource_id=ident)
    days=row.destination.get('_retention_days',0)
    if row.status!='succeeded' or not days:return
    try:
        actor=rbac.resolve_user(db,User(**row.destination['_actor']))
        permissions.require(db,actor,'backups.archives.delete',ident)
    except HTTPException:
        # Revocation during an export must not discard its verified success record.
        db.add(AuditEvent(actor='system:backup-retention',action='backup.retention.skipped',target=row.id,detail={'plan_id':ident,'reason':'authority_revoked'}))
        return
    cutoff=utcnow()-timedelta(days=days)
    for old in db.scalars(select(PlatformBackup).where(PlatformBackup.status=='succeeded',
            PlatformBackup.created_at<cutoff,PlatformBackup.destination['_plan_id'].as_string()==ident)):
        if old.id==row.id:continue
        try:
            permissions.require(db,actor,'backups.destinations.use',old.destination['_plan']['destination_id'])
            dest={**old.destination,'_allow_delete':True}
            client=backups.client(dest,store,db=db)
            args={'Bucket':old.bucket,'Key':old.object_key}
            if old.destination.get('_archive_version_id'):args['VersionId']=old.destination['_archive_version_id']
            client.delete_object(**args)
            old.status='expired'
            db.add(AuditEvent(actor='system:backup-retention',action='backup.expired',target=old.id,detail={'plan_id':ident}))
        except Exception:
            # Object Lock / IAM failures never turn a verified new archive into failure.
            db.add(AuditEvent(actor='system:backup-retention',action='backup.retention.failed',target=old.id,detail={'plan_id':ident}))
            notify.queue(db,'backup.retention.failed',[],'Backup retention failed','An old archive could not be deleted; inspect Object Lock and destination permissions.',key='backup-retention:'+old.id,resource_id=ident)

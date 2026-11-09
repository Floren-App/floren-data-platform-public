"""Independent scheduled plans, vault-only keys, raw export and destination boundaries."""
from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from app import backup_plans as plans, deps
from app.db import SessionLocal
from app.models import DataSource, PlatformBackup, PlatformSetting
from .conftest import as_
from .test_recovery import recovery_keys

ADMIN=as_('admin')


def destination(client,recovery_keys,name='Recovery'):
    private,public=recovery_keys
    deps.get_secret_store().put('backups/keys/enterprise',{'private_key':private.decode(),'public_key':public})
    response=client.post('/v1/backups/destinations',headers=ADMIN,json={'name':name,'bucket':'enterprise-recovery',
        'prefix':'backups/production','recovery_key_ref':'backups/keys/enterprise'})
    assert response.status_code==201,response.text
    assert 'private_key' not in response.text and public not in response.text
    return response.json()


def test_two_destinations_and_independent_plans_for_same_source(client,recovery_keys):
    first=destination(client,recovery_keys);second=destination(client,recovery_keys,'Offsite')
    with SessionLocal() as db:
        item=DataSource(name='orders',kind='postgres',config={'host':'db.internal','database':'orders'});db.add(item);db.flush();source=item.id;db.commit()
    ids=[]
    for dest in (first,second):
        response=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Nightly','source_kind':'storage','source_id':source,
            'destination_id':dest['id'],'schedule':'15 2 * * *','timezone':'Europe/Amsterdam','prefix':'database','enabled':True})
        assert response.status_code==201,response.text
        plan=response.json();ids.append(plan['id']);assert plan['next_run_at']
        run=client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=ADMIN)
        assert run.status_code==202,run.text
        assert client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=ADMIN).status_code==409
    assert ids[0]!=ids[1]
    assert len(client.get('/v1/backups/plans',headers=ADMIN).json())==2
    assert client.delete('/v1/backups/destinations/'+first['id'],headers=ADMIN).status_code==409
    client.delete('/v1/backups/plans/'+ids[0],headers=ADMIN).raise_for_status()
    assert len(client.get('/v1/backups/plans/'+ids[0]+'/runs',headers=ADMIN).json())==1
    client.delete('/v1/backups/destinations/'+first['id'],headers=ADMIN).raise_for_status()


def test_next_run_uses_wall_clock_and_dst():
    plan=plans.PlanIn(name='Nightly',destination_id='example',schedule='0 8 * * *',timezone='Europe/Amsterdam')
    assert plans.next_due(plan,datetime(2026,3,28,9,tzinfo=timezone.utc))==datetime(2026,3,29,6,tzinfo=timezone.utc)
    assert plans.next_due(plan,datetime(2026,10,24,9,tzinfo=timezone.utc))==datetime(2026,10,25,7,tzinfo=timezone.utc)
    for change in ({'schedule':'* * * * * *'},{'timezone':'Nowhere/Invalid'},{'prefix':'../outside'},{'source_kind':'storage'}):
        with pytest.raises(ValueError):plans.PlanIn(name='Invalid',destination_id='example',**change)


def test_nonadmin_cannot_export_control_plane_or_register_destination(client,recovery_keys):
    dest=destination(client,recovery_keys)
    assert client.get('/v1/backups/plans',headers=as_('ana')).status_code==403
    assert client.post('/v1/backups/plans',headers=as_('ana'),json={'name':'Forbidden','destination_id':dest['id']}).status_code==403
    assert client.post('/v1/backups/destinations',headers=as_('ana'),json={'name':'Exfiltration',
        'bucket':'external-copy','recovery_key_ref':'backups/keys/enterprise'}).status_code==403


def test_schedule_does_not_replay_missed_runs_or_schedule_paused_plans(client,recovery_keys):
    dest=destination(client,recovery_keys)
    response=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Daily','destination_id':dest['id'],'enabled':True})
    assert response.status_code==201,response.text
    ident=response.json()['id']
    with SessionLocal() as db:
        row=db.get(PlatformSetting,plans.PLAN+ident);row.value={**row.value,'_next_run_at':'2020-01-01T00:00:00+00:00'};db.commit()
    plans.schedule(SessionLocal);plans.schedule(SessionLocal)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(PlatformBackup))))==1
        row=db.get(PlatformSetting,plans.PLAN+ident)
        assert datetime.fromisoformat(row.value['_next_run_at'])>datetime.now(timezone.utc)


def test_storage_archive_roundtrip_and_object_scope(client,recovery_keys,monkeypatch,tmp_path):
    import io,hashlib
    from app import backup_adapters, storage_recovery, recovery_archive
    from app.models import DataSource
    class Body(io.BytesIO):
        def iter_chunks(self,chunk_size):return iter(lambda:self.read(chunk_size),b'')
    payload=b'customer object data'
    calls=[]
    class Pages:
        def paginate(self,**kwargs):
            calls.append(kwargs)
            return [{'Contents':[{'Key':'tenant/orders/invoice.json','ETag':'stable','Size':len(payload)}]}]
    class S3:
        def get_paginator(self,name):return Pages()
        def get_object(self,**kwargs):
            assert kwargs['IfMatch']=='stable'
            return {'Body':Body(payload),'VersionId':'v1'}
    monkeypatch.setattr(backup_adapters,'source_client',lambda *args:S3())
    with SessionLocal() as db:
        source=DataSource(name='orders-files',kind='s3',config={'bucket':'orders','prefix':'tenant'})
        db.add(source);db.flush()
        frozen={'id':source.id,'name':source.name,'kind':source.kind,'config':source.config,
            'source_prefix':'orders','max_bytes':1024,'max_objects':10}
        root=tmp_path/'export';root.mkdir()
        archive=tmp_path/'storage.tar';backup_adapters.bundle(db,frozen,deps.get_secret_store(),root,archive)
    assert calls==[{'Bucket':'orders','Prefix':'tenant/orders/'}]
    encrypted=tmp_path/'storage.fdp';private,public=recovery_keys
    recovery_archive.encrypt(archive,encrypted,public)
    verified=tmp_path/'verified.tar';recovery_archive.decrypt(encrypted,verified,private,recovery_archive.checksum(encrypted))
    output=tmp_path/'restore';output.mkdir()
    manifest=storage_recovery.unpack(verified,output)
    assert manifest['method']=='s3-current-objects' and manifest['objects'][0]['version_id']=='v1'
    assert (output/manifest['objects'][0]['member']).read_bytes()==payload
    assert all('private_key' not in p.name for p in output.rglob('*'))


def test_a_plan_cannot_recursively_back_up_its_own_output(client,recovery_keys):
    dest=destination(client,recovery_keys)
    with SessionLocal() as db:
        source=DataSource(name='source-backup-bucket',kind='s3',config={'bucket':dest['bucket'],'prefix':'backups'})
        db.add(source);db.flush();ident=source.id;db.commit()
    response=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Recursive','source_kind':'storage',
        'source_id':ident,'destination_id':dest['id'],'prefix':'production'})
    assert response.status_code==422,response.text


def test_iceberg_plans_accept_namespaces_and_reject_file_prefixes(client,recovery_keys):
    dest=destination(client,recovery_keys)
    with SessionLocal() as db:
        item=DataSource(name='lake-backup-test',kind='iceberg',config={'warehouse':'s3://table-warehouse/','catalog_uri':'https://catalog.example'})
        db.add(item);db.flush();ident=item.id;db.commit()
    body={'name':'Snapshots','source_kind':'storage','source_id':ident,'destination_id':dest['id'],'schemas':['reporting']}
    response=client.post('/v1/backups/plans',headers=ADMIN,json=body)
    assert response.status_code==201,response.text
    assert response.json()['schemas']==['reporting']
    assert client.post('/v1/backups/plans',headers=ADMIN,json={**body,'source_prefix':'arbitrary-files'}).status_code==422
    source=next(s for s in client.get('/v1/backups/sources',headers=ADMIN).json() if s['id']==ident)
    assert source['supported'] and 'snapshot' in source['detail']


def test_runtime_keys_load_only_from_chosen_vault_without_environment_fallback(monkeypatch):
    from app import config,secrets
    store=secrets.MemorySecretStore();store.put('platform/runtime',{'session_signing_key':'vault-only-key-that-is-long-enough-to-use'})
    monkeypatch.setattr(secrets,'make_secret_store',lambda settings:store)
    monkeypatch.setenv('FDP_RUNTIME_KEYS_REF','platform/runtime')
    monkeypatch.setenv('FDP_GATEWAY_CONFIG_TOKEN','must-not-be-used-as-fallback')
    settings=config.get_settings.__wrapped__()
    assert settings.session_signing_key=='vault-only-key-that-is-long-enough-to-use'
    assert settings.gateway_config_token==''
    store.delete('platform/runtime')
    with pytest.raises(RuntimeError):config.get_settings.__wrapped__()


def test_source_scoped_operator_can_manage_only_its_layer_plans(client,recovery_keys):
    from app import rbac,layers
    from app.models import PermissionGrant,Resource
    from .rbac_helpers import grant_group
    dest=destination(client,recovery_keys)
    group=grant_group(client,'set-backups','Database backup operators','Reader',users=['bob'])
    with SessionLocal() as db:
        item=DataSource(name='dev-orders',kind='postgres',config={'host':'db.internal','database':'orders','layer_id':layers.NON_PROD})
        db.add(item);db.flush();source_id=item.id;target=rbac.source_resource(db,item)
        for action in ('backups.plans.create','backups.plans.read','backups.plans.update','backups.plans.run',
                       'backups.history.read','backups.archives.delete','backups.sources.export'):
            db.add(PermissionGrant(target_id=target.id,namespace='backups',app_group_id=group,
                layer_id=layers.NON_PROD,actions=[action]))
        db.add(PermissionGrant(target_id=dest['id'],namespace='backups',app_group_id=group,
            actions=['backups.destinations.read','backups.destinations.use']))
        db.commit()
    body={'name':'Dev database','source_kind':'storage','source_id':source_id,'destination_id':dest['id']}
    response=client.post('/v1/backups/plans',headers=as_('bob'),json=body)
    assert response.status_code==201,response.text
    plan=response.json();assert plan['layer_id']==layers.NON_PROD
    assert 'backups.plans.run' in plan['capabilities']
    assert 'backups.plans.destination.change' not in plan['capabilities']
    assert client.put('/v1/backups/plans/'+plan['id'],headers=as_('bob'),json={**body,'name':'Renamed'}).status_code==200
    assert client.put('/v1/backups/plans/'+plan['id'],headers=as_('bob'),json={**body,'prefix':'redirected'}).status_code==403
    assert client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=as_('bob')).status_code==202
    assert len(client.get('/v1/backups/plans/'+plan['id']+'/runs',headers=as_('bob')).json())==1
    assert client.post('/v1/backups/plans',headers=as_('bob'),json={'name':'Metadata','destination_id':dest['id']}).status_code==403


def test_retention_revocation_preserves_verified_success(client,recovery_keys,monkeypatch):
    from fastapi import HTTPException
    from app import rbac
    dest=destination(client,recovery_keys)
    plan=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Daily','destination_id':dest['id']}).json()
    run=client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=ADMIN).json()
    with SessionLocal() as db:
        row=db.get(PlatformBackup,run['id']);row.status='succeeded'
        monkeypatch.setattr(rbac,'resolve_user',lambda *args:(_ for _ in ()).throw(HTTPException(403,'revoked')))
        plans.finish(db,row,deps.get_secret_store());db.commit()
        assert db.get(PlatformBackup,row.id).status=='succeeded'


def test_legacy_admission_does_not_block_independent_plan(client,recovery_keys):
    from app import backups
    from .test_recovery import config
    dest=destination(client,recovery_keys)
    plan=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Daily','destination_id':dest['id']}).json()
    client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=ADMIN).raise_for_status()
    client.put('/v1/backups/configuration',headers=ADMIN,json=config(recovery_keys[1])).raise_for_status()
    with SessionLocal() as db:
        assert backups.queue(db,'admin').id
        db.commit()


def test_plan_status_preview_and_prerequisites_change_nothing(client,recovery_keys):
    """A plan says whether it is paused, scheduled, running or failed; the schedule preview and the prerequisite
    check explain without enabling, writing or scheduling anything."""
    dest=destination(client,recovery_keys)
    assert client.get('/v1/backups/plans',headers=ADMIN).json()==[]   # not configured: nothing invented
    body={'name':'Nightly','destination_id':dest['id'],'schedule':'30 1 * * 0','timezone':'Europe/Amsterdam','prefix':'weekly'}
    checks={c['key']:c for c in client.post('/v1/backups/plans/check',headers=ADMIN,json=body).json()['checks']}
    assert {k:c['state'] for k,c in checks.items()}=={'source':'ready','recovery_key':'ready','destination':'ready'}
    assert client.get('/v1/backups/plans',headers=ADMIN).json()==[]   # the check created no plan
    runs=client.get('/v1/backups/schedule-preview',headers=ADMIN,params={'schedule':'30 1 * * 0','timezone':'Europe/Amsterdam'}).json()['runs']
    assert len(runs)==5 and all(datetime.fromisoformat(r).weekday() in (5,6) for r in runs)   # Sunday 01:30 local is Saturday/Sunday UTC
    assert client.get('/v1/backups/schedule-preview',headers=ADMIN,params={'schedule':'61 * * * *'}).status_code==422
    plan=client.post('/v1/backups/plans',headers=ADMIN,json=body).json()
    assert plan['enabled'] is False and plan['status']['state']=='paused' and plan['status']['last_success_at'] is None
    assert plan['max_bytes']==10737418240 and plan['max_objects']==100000   # the limits are part of the plan
    client.post('/v1/backups/plans/'+plan['id']+'/runs',headers=ADMIN).raise_for_status()
    assert client.get('/v1/backups/plans/'+plan['id'],headers=ADMIN).json()['status']['state']=='running'
    # A vault reference without a usable key is reported, not repaired.
    deps.get_secret_store().put('backups/keys/broken',{'public_key':''})
    with SessionLocal() as db:
        row=db.scalar(select(PlatformSetting).where(PlatformSetting.key==plans.DESTINATION+dest['id']))
        row.value={**row.value,'recovery_key_ref':'backups/keys/broken'};db.commit()
    checks={c['key']:c for c in client.post('/v1/backups/plans/check',headers=ADMIN,json=body).json()['checks']}
    assert checks['recovery_key']['state']=='missing' and 'public_key' in checks['recovery_key']['detail']
    assert client.post('/v1/backups/plans/check',headers=as_('ana'),json=body).status_code==403

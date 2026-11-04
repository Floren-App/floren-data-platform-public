"""Stable permission targets for physical data objects; registration grants no data access."""
import hashlib
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import permissions, rbac, object_access
from ..db import get_db
from ..deps import get_actor
from ..models import Resource, DataSource
from ..storage_access import prefix_path
from .common import audit

router = APIRouter(prefix='/v1/data-objects', tags=['object permissions'])


class ObjectIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parent_id: str = Field(min_length=1, max_length=12)
    kind: Literal['table','column','file','directory']
    name: str = Field(min_length=1, max_length=256)


def output(row):
    return {'id':row.id, 'parent_id':row.parent_id, 'kind':row.kind, 'name':row.name,
            'source_id':row.object_id, 'config':row.config}


def get(db, ident):
    row = db.get(Resource, ident)
    if not row or row.kind not in ('table','column','file','directory'):
        raise HTTPException(404, 'data object not found')
    return row


def manage(db, user, parent, action):
    if not permissions.allowed(db, user, action, parent.id):
        rbac.require(db, user, 'ddl', parent)


@router.post('', status_code=201)
def register(body: ObjectIn, db=Depends(get_db), user=Depends(get_actor)):
    parent = db.get(Resource, body.parent_id)
    if not parent:
        raise HTTPException(404, 'parent not found')
    if body.kind == 'column' and parent.kind != 'table':
        raise HTTPException(422, 'columns belong to a registered table')
    if body.kind == 'table' and parent.kind != 'schema':
        raise HTTPException(422, 'tables belong to a registered schema')
    if body.kind in ('file','directory') and parent.kind not in ('schema','directory'):
        raise HTTPException(422, 'files and directories belong to a storage namespace or directory')
    schema = next((r for r in rbac.ancestors(db, parent) if r.kind == 'schema'), None)
    source = db.get(DataSource, schema.object_id) if schema else None
    if not source or (source.kind == 's3') != (body.kind in ('file','directory')):
        raise HTTPException(422, 'object kind does not match its storage integration')
    if '\x00' in body.name:
        raise HTTPException(422, 'object name contains a null byte')
    manage(db, user, parent, 'storage.objects.create')
    config = {'schema_id':schema.id}
    if body.kind in ('file','directory'):
        if '/' in body.name or body.name in ('.','..'):
            raise HTTPException(422, 'use one path component per registered file or directory')
        path = prefix_path(parent.config.get('path', '') if parent.kind == 'directory' else '', body.name)
        if not path:
            raise HTTPException(422, 'a file or directory must have a relative path')
        config['path'] = path
    ident = hashlib.sha256((parent.id+'\0'+body.kind+'\0'+body.name).encode()).hexdigest()[:12]
    existing = db.get(Resource, ident)
    if existing:
        if (existing.parent_id, existing.kind, existing.name) != (parent.id, body.kind, body.name):
            raise HTTPException(409, 'object identity collision')
        return output(existing)
    row = Resource(id=ident, parent_id=parent.id, kind=body.kind, name=body.name,
                   object_id=source.id, config=config)
    db.add(row); db.flush()
    # Registration is metadata only. Never grant an owner data rights just for registering a table.
    audit(db, user.username, 'data_object.register', row.id, parent=parent.id, kind=body.kind)
    db.commit()
    return output(row)


@router.get('/{ident}')
def read(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    row = get(db, ident)
    if not (object_access.metadata_visible(db, user, row) or rbac.allowed(db, user, 'read', row)):
        raise HTTPException(404, 'data object not found')
    return output(row)


@router.delete('/{ident}', status_code=204)
def unregister(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    row = get(db, ident)
    manage(db, user, row, 'storage.objects.drop')
    if db.scalar(select(Resource.id).where(Resource.parent_id == row.id)):
        raise HTTPException(409, 'remove child permission targets first')
    rbac.delete_resource_assignments(db, row.id)
    audit(db, user.username, 'data_object.unregister', row.id)
    db.delete(row); db.commit()  # Never delete the physical table/file.


class SessionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['read','write','read_write','delete','list'] = 'read'


@router.post('/{ident}/session')
def file_session(ident: str, body: SessionIn, db=Depends(get_db), user=Depends(get_actor)):
    from ..deps import get_secret_store
    from ..storage_types import lake_buckets
    from ..storage_access import credentials
    row = get(db, ident)
    from ..environments import permits
    if not permits(db,user,row):
        raise HTTPException(403,'cross-layer file access is forbidden')
    if row.kind not in ('file','directory'):
        raise HTTPException(422, 'SQL objects use the governed SQL endpoint, never native credentials')
    schema = next(r for r in rbac.ancestors(db,row) if r.kind == 'schema')
    source = db.get(DataSource,schema.object_id)
    if source.config['bucket'] in lake_buckets(db):
        raise HTTPException(409, 'Iceberg data is accessible only through governed SQL')
    if schema.config.get('state') != 'ready':
        raise HTTPException(409, 'set up namespace access before issuing object sessions')
    rules = object_access.schema_rules(db,user,schema)
    # A running job uses its admitted authority, as for the governed SQL snapshot.
    if user.execution_context.startswith('run:'):
        from ..models import Run
        run = db.get(Run,user.execution_context[4:])
        if not run or run.status not in ('pending','running') or run.principal_id != user.principal_id:
            raise HTTPException(401, 'run admission is no longer active')
        rules = (run.named_access_snapshot or {}).get(source.id,{}).get(schema.name,{})
    held = object_access.file_actions(rules,row.config['path'])
    legacy = ((run.access_snapshot or {}).get(source.id,{}).get(schema.name)
        if user.execution_context.startswith('run:') else rbac.data_scope(db,user,source).get(schema.name))
    if legacy:
        roles = {'Reader':{'DataReader'}, 'Contributor':{'DataContributor'}, 'Admin':{'Admin'}, 'Owner':{'Owner'}}.get(legacy,set())
        held.update(key for key,value in permissions.REGISTRY.items() if key.startswith('data.files.') and roles.intersection(value['roles']))
    policy, path = object_access.file_session_policy(source,schema,row,held,body.mode)
    cred, _ = credentials(source,{schema.name:schema},{schema.name:'Reader'},get_secret_store(),db=db,policy_override=policy)
    audit(db,user.username,'data_object.session',row.id,mode=body.mode)
    db.commit()
    return {'name':source.name,'kind':'s3','object_kind':row.kind,
        'config':{**source.config,'prefix':path}, 'credentials':cred,
        'access':{'resource_id':row.id,'mode':body.mode,'namespaces':{}}}

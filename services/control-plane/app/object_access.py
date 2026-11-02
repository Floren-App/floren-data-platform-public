"""Named table/column/file authority, compiled into immutable query/run snapshots.

These grants add authority. A broad legacy source/schema grant is not narrowed by a
weaker child assignment; remove that broad grant when switching to object grants.
"""
from __future__ import annotations

from sqlalchemy import select
from . import permissions, rbac
from .authorization_cache import cached
from .models import Resource, DataSource

PREFIXES = ('data.queries.', 'data.routines.', 'data.sql.', 'data.files.', 'data.iceberg.', 'storage.objects.', 'storage.schemas.')
KINDS = ('source','schema','table','column','file','directory')


@cached
def source_record(db, ident):
    return db.get(DataSource, ident)


@cached
def snapshot(db, user):
    user = rbac.resolve_user(db, user)
    from .environments import permits
    assignments = rbac.role_grants(db, user)
    resources = {r.id:r for r in db.scalars(select(Resource).where(Resource.kind.in_(KINDS)))}
    grants = [(g, permissions.grant_actions(db, g)) for g in permissions.grants(db, user)]
    from types import SimpleNamespace
    from .app_profiles import interactive
    for grant in assignments:
        target = resources.get(grant.resource_id)
        if not target or not (target.kind in rbac.DATA_OBJECT_KINDS or
            (target.kind in ('source', 'schema') and grant.role in ('Viewer','Manager','Contributor'))):
            continue
        if interactive(user) and getattr(grant, 'app_group_id', None) and grant.app_group_id != user.active_group_id:
            continue
        actions = {key for key,value in permissions.REGISTRY.items() if key.startswith(PREFIXES) and grant.role in value['roles']}
        grants.append((SimpleNamespace(target_id=target.id, layer_id=permissions.resource_layer(db,target.id), legacy_reviewed=True), actions))
    grants = [(g,{a for a in actions if a.startswith(PREFIXES)}) for g,actions in grants]
    result = {}
    for resource in resources.values():
        if resource.kind == 'source' or not permits(db, user, resource):
            continue
        chain = rbac.ancestors(db, resource)
        ids = permissions.targets(db,resource.id)
        layer = permissions.resource_layer(db, resource.id)
        if user.environment_id and layer != user.environment_id:
            continue
        held = set()
        for grant, actions in grants:
            if grant.target_id not in ids or not permissions.scope_matches(grant.layer_id, layer):
                continue
            if not getattr(grant, 'legacy_reviewed', False) and any(a.startswith(('data.sql.','data.files.','data.iceberg.')) for a in actions):
                from .compliance import role_requirement
                role = 'Executor' if resource.kind in rbac.DATA_OBJECT_KINDS else 'Reader'
                req = role_requirement(db, resource, role, getattr(grant, 'app_group_id', None))
                if req['policy'] is not None:
                    # Mandatory reviews cannot be bypassed with a parallel named grant.
                    actions = {a for a in actions if not a.startswith(('data.sql.','data.files.','data.iceberg.'))}
            held.update(actions)
        if not held:
            continue
        schema = next((r for r in chain if r.kind == 'schema'), None)
        if schema is None:
            continue
        item = result.setdefault(schema.object_id, {}).setdefault(schema.name,
            {'actions':[], 'tables':{}, 'files':{}})
        if resource.kind == 'schema':
            item['actions'] = sorted(held)
        elif resource.kind in ('table','column'):
            table = next((r for r in chain if r.kind == 'table'), None)
            if not table:
                continue
            entry = item['tables'].setdefault(table.name, {'actions':[], 'columns':{}})
            if resource.kind == 'table':
                entry['actions'] = sorted(held)
            else:
                entry['columns'][resource.name] = sorted(held)
        elif resource.kind in ('file','directory'):
            item['files'][resource.config['path']] = {'directory':resource.kind == 'directory', 'actions':sorted(held)}
    return result


def schema_rules(db, user, schema):
    return snapshot(db, user).get(schema.object_id, {}).get(schema.name, {})


def visible(rules, table='', column=''):
    if rules.get('actions'):
        return True
    item = rules.get('tables', {}).get(table, {})
    if column:
        return bool(item.get('actions') or item.get('columns', {}).get(column))
    return bool(item.get('actions') or item.get('columns')) if table else bool(rules.get('tables') or rules.get('files'))


def actions_for(rules, table):
    return set(rules.get('actions', [])) | set(rules.get('tables', {}).get(table, {}).get('actions', []))


def policy_allow(rules, operation, table='', columns=None):
    """OPA object operations. Missing/unknown shapes never imply broad authority."""
    actions = actions_for(rules, table)
    entry = rules.get('tables', {}).get(table, {})
    column_actions = entry.get('columns', {})
    if operation in ('FilterSchemas','ShowTables','ShowFunctions','ShowCreateSchema'):
        return visible(rules)
    if operation in ('FilterTables','ShowColumns','ShowCreateTable'):
        return bool(table) and visible(rules, table)
    if operation == 'FilterColumns':
        return columns is not None and all(visible(rules, table, c) for c in columns)
    if operation == 'SelectFromColumns':
        if '$' in table:
            return 'data.iceberg.metadata.read' in actions_for(rules, table.split('$',1)[0])
        needed={'data.sql.select','data.queries.results.read'}
        if needed.issubset(actions):
            return True
        if columns is None:
            return False
        if not columns:
            return any(needed.issubset(actions | set(a)) for a in column_actions.values())
        return all(needed.issubset(actions | set(column_actions.get(c, []))) for c in columns)
    if operation == 'UpdateTableColumns':
        if 'data.sql.update' in actions:
            return True
        return bool(columns) and all('data.sql.update' in set(column_actions.get(c, [])) for c in columns)
    mapping = {'InsertIntoTable':'data.sql.insert', 'DeleteFromTable':'data.sql.delete',
        'TruncateTable':'data.sql.truncate', 'DropTable':'storage.objects.drop',
        'CreateSchema':'storage.schemas.create_native', 'DropSchema':'storage.schemas.drop_native',
        'CreateView':'storage.objects.create', 'CreateMaterializedView':'storage.objects.create',
        'DropView':'storage.objects.drop', 'DropMaterializedView':'storage.objects.drop',
        'RenameView':'storage.objects.alter', 'RenameMaterializedView':'storage.objects.alter',
        'CreateTable':'storage.objects.create', 'RenameTable':'storage.objects.alter',
        'AddColumn':'storage.objects.alter', 'AlterColumn':'storage.objects.alter',
        'DropColumn':'storage.objects.alter', 'RenameColumn':'storage.objects.alter',
        'SetTableProperties':'storage.objects.alter', 'SetTableComment':'storage.objects.metadata.edit',
        'SetColumnComment':'storage.objects.metadata.edit'}
    action = mapping.get(operation)
    return bool(action and (action in actions or (operation != 'TruncateTable' and 'data.sql.ddl' in actions)))


def file_actions(rules, path):
    from .storage_access import prefix_path
    path = prefix_path('', path)
    held = set(rules.get('actions', []))
    for key, entry in rules.get('files', {}).items():
        if key == path or (entry['directory'] and path.startswith(key+'/')):
            held.update(entry['actions'])
    return held


def has_schema(db, user, schema):
    return visible(schema_rules(db, user, schema))


def metadata_visible(db, user, resource):
    if resource.kind not in KINDS:
        return False
    data = snapshot(db, user)
    if resource.kind == 'source':
        return bool(data.get(resource.object_id))
    chain = rbac.ancestors(db, resource)
    schema = next((r for r in chain if r.kind == 'schema'), None)
    if not schema:
        return False
    rules = data.get(schema.object_id, {}).get(schema.name, {})
    table = next((r for r in chain if r.kind == 'table'), None)
    if resource.kind in ('file','directory'):
        return bool(file_actions(rules, resource.config['path']))
    return visible(rules, table.name if table else '', resource.name if resource.kind == 'column' else '')


def asset_visible(db, user, asset, resource):
    if resource.kind != 'schema':
        return True
    source = source_record(db, resource.object_id)
    if source is None:
        return rbac.allowed(db, user, 'read', resource)
    if rbac.data_scope(db, user, source).get(resource.name):
        return True
    rules = schema_rules(db, user, resource)
    path = asset.attributes.get('object_path', '')
    if path and asset.kind in ('file','folder'):
        return bool(file_actions(rules, path)) or any(key.startswith(path.rstrip('/')+'/') for key in rules.get('files', {}))
    if not path and asset.kind == 'schema':
        return visible(rules)
    table = path or asset.attributes.get('table') or (asset.name if asset.kind in ('table','view','materialized_view') else '')
    return bool(table and visible(rules, table))


def read_only(rules):
    allowed = {'data.sql.select', 'data.sql.preview', 'storage.objects.list', 'storage.objects.describe'}
    return {**rules, 'actions':sorted(set(rules.get('actions', [])) & allowed),
        'tables':{name:{'actions':sorted(set(item.get('actions', [])) & allowed),
            'columns':{column:sorted(set(actions) & allowed) for column,actions in item.get('columns', {}).items()}}
            for name,item in rules.get('tables', {}).items()}, 'files':{}}


def ensure_target(db, parent, kind, name, create=True):
    import hashlib
    from .storage_access import prefix_path
    if kind in ('file','directory'):
        parts = prefix_path('', name).split('/')
        for part in parts[:-1]:
            parent = ensure_target(db, parent, 'directory', part, create)
            if parent is None:
                return None
        name = parts[-1]
    ident = hashlib.sha256((parent.id+'\0'+kind+'\0'+name).encode()).hexdigest()[:12]
    row = db.get(Resource, ident)
    if row or not create:
        return row
    schema = next(r for r in rbac.ancestors(db, parent) if r.kind == 'schema')
    config = {'schema_id':schema.id}
    if kind in ('file','directory'):
        config['path'] = prefix_path(parent.config.get('path', '') if parent.kind == 'directory' else '', name)
    if kind == 'column':
        config['table'] = parent.name
    row = Resource(id=ident, parent_id=parent.id, kind=kind, name=name, object_id=schema.object_id, config=config)
    db.add(row); db.flush()
    return row


def target_for_asset(db, asset, schema, create=False):
    kind = 'table' if asset.kind in ('table','view','materialized_view') else 'directory' if asset.kind == 'folder' else 'file' if asset.kind == 'file' else ''
    name = asset.attributes.get('object_path') or asset.attributes.get('table') or asset.name
    if not kind or not name:
        return None
    row = ensure_target(db, schema, kind, name, create)
    if create and row and row.config.get('catalog_asset_id') != asset.id:
        row.config = {**row.config, 'catalog_asset_id':asset.id}
        db.flush()
    return row


def file_session_policy(source, schema, resource, held, mode):
    from fastapi import HTTPException
    from .storage_access import prefix_path
    required = {'read':{'data.files.read'}, 'write':{'data.files.create','data.files.overwrite'},
                'read_write':{'data.files.read','data.files.create','data.files.overwrite'},
                'delete':{'data.files.delete'}, 'list':{'data.files.list'}}[mode]
    if not required.issubset(held):
        raise HTTPException(403, 'requested file session exceeds the object permissions')
    path = prefix_path(source.config.get('prefix',''), schema.config.get('prefix',schema.name))
    path = prefix_path(path, resource.config['path'])
    bucket = source.config['bucket']
    if any(c in bucket for c in ('/', '*', '?', '$', '{', '}')):
        raise HTTPException(422, 'configure a literal bucket')
    directory = resource.kind == 'directory'
    if mode == 'list' and not directory:
        raise HTTPException(422, 'individual file metadata is available through FDP; only directories issue listing sessions')
    prefix = path+'/' if directory else path
    actions = []
    if mode in ('read','read_write'):
        actions += ['s3:GetObject','s3:GetObjectVersion']
    if mode in ('write','read_write'):
        actions += ['s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts']
    if mode == 'delete':
        actions += ['s3:DeleteObject']
    statements = [{'Effect':'Allow','Action':['s3:GetBucketLocation'],'Resource':['arn:aws:s3:::'+bucket]}]
    if actions:
        statements.append({'Effect':'Allow','Action':actions,
            'Resource':['arn:aws:s3:::'+bucket+'/'+prefix+('*' if directory else '')]})
    if directory and 'data.files.list' in held:
        statements.append({'Effect':'Allow','Action':['s3:ListBucket'],'Resource':['arn:aws:s3:::'+bucket],
            'Condition':{'StringLike':{'s3:prefix':[prefix, prefix+'*']}}})
    return {'Version':'2012-10-17','Statement':statements}, path


def directory_list_allowed(rules, path):
    if 'data.files.list' in rules.get('actions', []):
        return True
    return any(entry['directory'] and 'data.files.list' in entry['actions'] and
        (path == key or path.startswith(key+'/')) for key,entry in rules.get('files', {}).items())


def require_action(db, user, schema, action, table=''):
    """Separate API functionality from the payload authority Trino needs internally."""
    from fastapi import HTTPException
    source = source_record(db, schema.object_id)
    legacy = rbac.data_scope(db,user,source).get(schema.name) if source else None
    roles = {'Reader':{'Executor'}, 'Contributor':{'Executor','Contributor'}, 'Admin':{'Admin'}, 'Owner':{'Owner'}}.get(legacy,set())
    if roles.intersection(permissions.REGISTRY[action]['roles']):
        return
    rules = schema_rules(db,user,schema)
    actions = actions_for(rules,table)
    if action in actions:
        return
    if action in ('storage.objects.list','storage.objects.describe'):
        entries = [rules.get('tables',{}).get(table,{})] if table else list(rules.get('tables',{}).values())
        if any(action in item.get('actions',[]) or any(action in a for a in item.get('columns',{}).values()) for item in entries):
            return
    if action == 'data.sql.preview' and any(action in a for a in rules.get('tables',{}).get(table,{}).get('columns',{}).values()):
        return
    raise HTTPException(403, action+' permission required')

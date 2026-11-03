"""One inventory of data, platform and access-administration grants.

This is a grant inventory, not an impersonation API: execution still evaluates
active groups, page visibility, reviewed access and object/data scope at admission.
"""
from sqlalchemy import select
from . import permissions, rbac
from .models import PermissionGrant, Resource, ComplianceRole, AppProfile, JustifiedGrant, JustificationDeadline, DataSource

CATEGORIES=('data','platform','privileged')


def category(action):
    if action=='roles.custom.read':return 'platform'
    if action.startswith(('delegation.query.','delegation.files.')):return 'data'
    if ('.roles.' in action or action.startswith(('roles.','delegation.')) or
        action in ('groups.members.add','groups.members.remove','groups.owners.change','groups.idp.bind','groups.profile.assign',
                   'workloads.settings.identity.change','backups.destinations.create','backups.destinations.update','backups.plans.source.change','backups.plans.destination.change','backups.recovery.resume','secrets.use','secrets.binding.change','apis.object.credentials.change','apis.object.identity.change','workloads.settings.execution_mode.change')):
        return 'privileged'
    if action in ('data.sql.ddl','data.files.namespace.create','data.files.namespace.delete'):return 'platform'
    if (action.startswith(('data.sql.','data.files.','data.iceberg.','kafka.data.')) or
        action in ('data.queries.results.read','packages.download','packages.files.download','backups.sources.export','apis.calls.invoke','artifacts.read','artifacts.download','artifacts.open','artifacts.delete','workspaces.files.read','workspaces.files.download','workspaces.files.write','workspaces.files.delete','logs.read','logs.export')):
        return 'data'
    return 'platform'


def namespaces(resource, db=None):
    kind=resource.kind
    if kind in ('file','directory'):return ('data.files.','storage.objects.')
    if kind in rbac.DATA_OBJECT_KINDS or kind in ('source','schema'):
        source = db.get(DataSource,resource.object_id) if db is not None and resource.object_id else None
        if source and source.kind=='s3':return ('data.files.','storage.')
        if source and source.kind!='iceberg':return ('data.sql.','data.queries.','data.routines.','storage.')
        return ('data.','storage.')
    if kind=='topic':return ('kafka.topics.','kafka.data.')
    if kind=='kafka':return ('kafka.',)
    if kind in ('api-group','api-operation'):return ('apis.operations.','apis.calls.')
    if kind=='api':return ('apis.object.','apis.operations.','apis.calls.')
    if kind in rbac.API_KINDS:return ('apis.',)
    if kind in rbac.PACKAGE_KINDS:return ('packages.',)
    if kind in rbac.PRODUCT_KINDS:return ('products.',)
    if kind in ('setting','settings-section','settings-root'):
        from .settings_access import feature_namespaces
        # Resolved to leaf settings targets by the caller.
        return feature_namespaces().get(resource.id,(resource.id+'.',))
    if kind in ('backup-plan','backup-dest'):return ('backups.',)
    if kind=='service' and resource.config.get('service_account'):return ('identities.','secrets.')
    return ('workloads.','runs.','artifacts.','logs.')


def legacy_actions(resource,role,db=None):
    if resource.id=='set-backups' or resource.kind in ('backup-plan','backup-dest'):
        return []  # Backup APIs intentionally require named function grants.
    aliases={'Reader':{'Executor'},'Consumer':{'Executor'},'Publisher':{'Contributor','Executor','Manager'},
             'Maintainer':{'Contributor','Executor','Manager'}}
    roles=aliases.get(role,{role})
    prefixes=namespaces(resource,db)
    result=[]
    for key,definition in permissions.REGISTRY.items():
        if not key.startswith(prefixes):continue
        if roles & set(definition['roles']):
            result.append(key)
    return result


def inventory(db, group, assigned):
    from .ownership import GLOBAL_ADMIN
    from .routes.rbac import compliance_rows
    from .compliance import REVIEW_PERMISSIONS
    sections={key:[] for key in CATEGORIES}
    resources={r.id:r for r in db.scalars(select(Resource))}
    def append(action,target,role,origin,layer=None,conditions=None,grant_id=None):
        definition=permissions.REGISTRY.get(action,{})
        item=resources.get(target)
        sections[category(action)].append({'permission':action,'description':definition.get('description',action),
            'target_id':target,'target_name':item.name if item else target,'role':role,'origin':origin,
            'layer_id':layer,'layer_name':resources[layer].name if layer in resources else None,'conditions':conditions or [],'grant_id':grant_id})
    if group.id==GLOBAL_ADMIN:
        for action in permissions.REGISTRY:
            append(action,'*','Owner','Protected Global Admin', '*', ['Global Admin must be the active group. Execution layer boundaries still apply.'])
        return sections
    profile=db.get(AppProfile,group.profile_id) if group.profile_id else db.scalar(select(AppProfile).where(AppProfile.default.is_(True)))
    profile_note=['App profile: '+profile.name] if profile else []
    for grant in db.scalars(select(PermissionGrant).where(PermissionGrant.app_group_id==group.id)):
        for action in sorted(permissions.grant_actions(db,grant)):
            append(action,grant.target_id,grant.role or ('Custom role' if grant.custom_role_id else 'Explicit permission'),
                   'Named assignment',grant.layer_id,profile_note+[permissions.REGISTRY[action].get('constraint','')],grant.id)
    # Include product-derived object grants, not just direct role assignments.
    expanded=list(assigned)+list(rbac.product_grants(db,assigned))
    seen=set()
    for grant in expanded:
        target=resources.get(grant.resource_id)
        if not target:continue
        extra=[]
        justified=db.scalar(select(JustifiedGrant).where(JustifiedGrant.resource_id==target.id,JustifiedGrant.app_group_id==group.id))
        deadline=db.scalar(select(JustificationDeadline).where(JustificationDeadline.resource_id==target.id,JustificationDeadline.app_group_id==group.id))
        if justified and justified.until:extra.append('Approved until '+justified.until.isoformat())
        if deadline:extra.append('Approved form required by '+deadline.due_at.isoformat())
        origin='Data product' if isinstance(grant,rbac.ProductGrant) else 'Object role'
        # Administrative scopes inherit to every child page; show concrete features.
        leaves=[r for r in resources.values() if r.kind=='setting' and target.id in {a.id for a in rbac.ancestors(db,r)}] if target.kind in ('settings-root','settings-section') else [target]
        for leaf in leaves:
            for action in legacy_actions(leaf,grant.role,db):
                marker=(action,leaf.id,grant.role,origin)
                if marker in seen:continue
                seen.add(marker)
                append(action,leaf.id,grant.role,origin,permissions.resource_layer(db,leaf.id),profile_note+extra+[permissions.REGISTRY[action].get('constraint','')]+
                       ['Inherited by child objects; data policies, approvals and page permissions are evaluated separately.'])
            if leaf.kind in ('setting','settings-section','settings-root'):
                append('pages.view',leaf.id,grant.role,origin,None,profile_note)
    # A review assignment is a scoped platform permission, with its normal admission conditions.
    for duty in compliance_rows(db,[group.id]):
        scope=duty['scope']
        sections['platform'].append({'permission':REVIEW_PERMISSIONS.get(duty['role'],duty['role']),
            'description':duty['label'],'target_id':'cases','target_name':'Access requests',
            'role':'Admin' if duty['role']=='compliance_admin' else 'Executor','origin':'Scoped platform assignment','layer_id':None,
            'conditions':['Scope: '+__import__('json').dumps(scope,sort_keys=True),
                'Requires the matching review function permission; self-review and assigned-step checks still apply.'], 'grant_id':None})
    for values in sections.values():values.sort(key=lambda r:(r['target_name'].casefold(),r['permission'],r['role']))
    return sections

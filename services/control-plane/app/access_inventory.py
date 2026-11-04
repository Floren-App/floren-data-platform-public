"""Roles, the permissions they carry, and what a group holds.

Permissions belong to roles; roles are given to groups on objects (or on Administration pages); people and service
identities are members of groups. Each role falls in one category: data access (reading or changing actual data,
files and messages), platform access (pages, features and configuration) or privileged access (administering,
deleting and delegating access). A role's permissions can span categories; the role's own category is what it is
for, and each permission keeps its own category too.

The group inventory is a grant inventory, not an impersonation API: execution still evaluates active groups,
page visibility, reviewed access and object/data scope at admission.
"""
from sqlalchemy import select
from . import permissions, rbac
from .models import (PermissionGrant, Resource, ComplianceRole, CustomPermissionRole, JustifiedGrant,
                     JustificationDeadline, DataSource, AppGroup, CatalogProduct)

CATEGORIES=('data','platform','privileged')
# Administering, deleting and delegating: privileged wherever they are given. Data Reader and Data Contributor are
# data access; Viewer, Reader, Contributor and Executor are platform access.
PRIVILEGED_ROLES={'Admin','Owner','AccessAdministrator'}
DATA_ROLES={'DataReader','DataContributor'}
DUTY_ROLES={'compliance_reviewer':'Forms reviewer','membership_approver':'Membership approver',
            'compliance_admin':'Compliance administrator'}


def category(action):
    if action=='roles.custom.read':return 'platform'
    if action.startswith(('delegation.query','delegation.files.')):return 'data'
    if ('.roles.' in action or action.startswith(('roles.','delegation.','reviews.')) or
        action in ('groups.members.add','groups.members.remove','groups.owners.change','groups.idp.bind','groups.profile.assign',
                   'workloads.settings.identity.change','backups.destinations.create','backups.destinations.update','backups.plans.source.change','backups.plans.destination.change','backups.recovery.resume','secrets.use','secrets.binding.change','apis.object.credentials.change','apis.object.identity.change','workloads.settings.execution_mode.change')):
        return 'privileged'
    # Structure and listings are about the object, not what it holds.
    if action in ('data.sql.ddl','data.files.namespace.create','data.files.namespace.delete','data.files.list','data.iceberg.metadata.read'):
        return 'platform'
    if (action.startswith(('data.sql.','data.files.','data.iceberg.','kafka.data.')) or
        action in ('data.queries.results.read','packages.download','packages.files.download','backups.sources.export','apis.calls.read','apis.calls.write','artifacts.read','artifacts.download','artifacts.open','artifacts.delete','workspaces.files.read','workspaces.files.download','workspaces.files.write','workspaces.files.delete','logs.read','logs.export')):
        return 'data'
    return 'platform'


def family(kind):
    if kind in ('kafka','topic'):return 'kafka'
    if kind in rbac.DATA_OBJECT_KINDS:return 'data_object'
    if kind in ('source','schema'):return 'data'
    if kind in rbac.API_KINDS:return 'api'
    if kind in rbac.PACKAGE_KINDS:return 'package'
    if kind in rbac.PRODUCT_KINDS:return 'product'
    if kind in ('setting','settings-section','settings-root'):return 'settings'
    return 'program'


def role_category(role):
    return 'privileged' if role in PRIVILEGED_ROLES else 'data' if role in DATA_ROLES else 'platform'


def actions_category(actions):
    """For permissions given without a built-in object role: the most powerful category among them."""
    found={category(a) for a in actions}
    return 'privileged' if 'privileged' in found else 'data' if 'data' in found else 'platform'


def _data_namespaces(source_kind):
    if source_kind=='s3':return ('data.files.','storage.')
    if source_kind and source_kind!='iceberg':return ('data.sql.','data.queries.','data.routines.','storage.')
    return ('data.','storage.')


def namespaces(resource, db=None):
    kind=resource.kind
    if kind in ('file','directory'):return ('data.files.','storage.objects.')
    if kind in rbac.DATA_OBJECT_KINDS or kind in ('source','schema'):
        source = db.get(DataSource,resource.object_id) if db is not None and resource.object_id else None
        return _data_namespaces(source.kind if source else None)
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
    if kind=='service' and (resource.config or {}).get('service_account'):return ('identities.','secrets.')
    return ('workloads.','runs.','artifacts.','logs.')


def _bundle(prefixes, role):
    return [key for key,definition in permissions.REGISTRY.items() if key.startswith(prefixes) and role in definition['roles']]


def legacy_actions(resource,role,db=None):
    if resource.id=='set-backups' or resource.kind in ('backup-plan','backup-dest'):
        return []  # Backup APIs intentionally require named function grants.
    return _bundle(namespaces(resource,db),role)


def _settings_actions(target_id, role):
    """A role on a page, a section or all of Administration: what it carries on each page below, plus seeing them."""
    from .settings_access import PAGES, ROOT, feature_namespaces
    pages=[p for p,(_,section) in PAGES.items() if target_id in (p,section,ROOT)]
    found={'pages.view'}
    for page in pages:
        if page!='set-backups':found.update(_bundle(feature_namespaces().get(page,(page+'.',)),role))
    return sorted(found)


def permission_rows(actions):
    return [{'key':a,'description':permissions.REGISTRY.get(a,{}).get('description',a),'category':category(a),
             'constraint':permissions.REGISTRY.get(a,{}).get('constraint','')} for a in sorted(actions)]


def _scope_text(db, scope):
    def names(ids):
        return [getattr(db.get(Resource,i) or db.get(AppGroup,i),'name',i) for i in ids]
    parts=[]
    if scope.get('layers'):parts.append('Layers: '+', '.join(names(scope['layers'])))
    if scope.get('classifications'):parts.append('Classifications: '+', '.join(scope['classifications']))
    if scope.get('resource_ids'):parts.append('Objects: '+', '.join(names(scope['resource_ids'])))
    if scope.get('group_ids'):parts.append('Groups: '+', '.join(names(scope['group_ids'])))
    return parts or ['Everything']


def assignments(db, group, assigned):
    """Every role the group holds, once, with the permissions it carries. Object roles (direct and through data
    products), roles on Administration pages and features, custom roles and selected permissions, and review duties."""
    from .ownership import GLOBAL_ADMIN
    from .compliance import REVIEW_PERMISSIONS, ROLES as DUTY_LABELS
    out=[]
    def add(role,cat,target_id,target_name,target_kind,origin,actions,layer=None,conditions=None,ident=None,**extra):
        layer_row=db.get(Resource,layer) if layer and layer!='*' else None
        out.append({'id':ident or f'{origin}:{target_id}:{role}:{len(out)}','role':role,'category':cat,'family':family(target_kind) if target_kind else None,
            'target_id':target_id,'target_name':target_name,'target_kind':target_kind,'origin':origin,
            'layer_id':layer,'layer_name':layer_row.name if layer_row else None,
            'conditions':conditions or [],'permissions':permission_rows(actions),**extra})
    if group.id==GLOBAL_ADMIN:
        add('Owner','privileged','*','Everything',None,'system',permissions.REGISTRY,'*',
            ['Global Admin must be the active group. Execution layer boundaries still apply.'])
        return out
    for grant in db.scalars(select(PermissionGrant).where(PermissionGrant.app_group_id==group.id)):
        actions=sorted(permissions.grant_actions(db,grant))
        target=db.get(Resource,grant.target_id)
        custom=db.get(CustomPermissionRole,grant.custom_role_id) if grant.custom_role_id else None
        role=grant.role or (custom.name if custom else 'Selected permissions')
        cat=role_category(grant.role) if grant.role else actions_category(actions)
        add(role,cat,grant.target_id,target.name if target else grant.target_id,target.kind if target else None,
            'custom' if custom else 'named',actions,grant.layer_id,['Feature: '+grant.namespace],grant.id,
            grant_id=grant.id,custom_role_id=grant.custom_role_id)
    # Product-derived object grants as well as direct role assignments.
    for grant in list(assigned)+list(rbac.product_grants(db,assigned)):
        target=db.get(Resource,grant.resource_id)
        if not target:continue
        conditions=[]
        justified=db.scalar(select(JustifiedGrant).where(JustifiedGrant.resource_id==target.id,JustifiedGrant.app_group_id==group.id))
        deadline=db.scalar(select(JustificationDeadline).where(JustificationDeadline.resource_id==target.id,JustificationDeadline.app_group_id==group.id))
        if justified and justified.until:conditions.append('Approved until '+justified.until.date().isoformat())
        if deadline:conditions.append('Needs an approved form by '+deadline.due_at.date().isoformat())
        via_product=isinstance(grant,rbac.ProductGrant)
        product=None
        if via_product:
            row=db.scalar(select(CatalogProduct).where(CatalogProduct.access_resource_id==grant.product_resource_id))
            product={'id':row.id,'title':row.title} if row else None
        actions=_settings_actions(target.id,grant.role) if target.kind in ('setting','settings-section','settings-root') else legacy_actions(target,grant.role,db)
        add(grant.role,role_category(grant.role),target.id,target.name,target.kind,
            'product' if via_product else 'object',actions,permissions.resource_layer(db,target.id),conditions,product=product)
    # A review duty is access administration: deciding who gets in. Its scope stays part of the duty.
    for duty in db.scalars(select(ComplianceRole).where(ComplianceRole.app_group_id==group.id)):
        add(DUTY_ROLES.get(duty.role,duty.role),'privileged','cases','Access requests',None,'duty',
            [REVIEW_PERMISSIONS.get(duty.role,duty.role)],None,_scope_text(db,duty.scope or {}),'duty:'+duty.id,
            duty_id=duty.id,description=DUTY_LABELS.get(duty.role,duty.role))
    out.sort(key=lambda r:(CATEGORIES.index(r['category']),r['target_name'].casefold(),r['role']))
    return out


# What the Roles page lists: each kind of object, the roles it offers and what each carries.
CATALOG_TYPES=(
    ('data-sql','PostgreSQL source or schema','source',_data_namespaces('postgres')),
    ('data-iceberg','Iceberg source or schema','source',_data_namespaces('iceberg')),
    ('data-s3','S3 source or namespace','source',_data_namespaces('s3')),
    ('table','Table or column','table',_data_namespaces(None)),
    ('file','File or directory','file',('data.files.','storage.objects.')),
    ('kafka','Kafka cluster','kafka',('kafka.',)),
    ('topic','Kafka topic','topic',('kafka.topics.','kafka.data.')),
    ('workloads','Workload folder, program or app','folder',('workloads.','runs.','artifacts.','logs.')),
    ('service-account','Service account','service-account',('identities.','secrets.')),
    ('api-root','All APIs','api-root',('apis.',)),
    ('api','API','api',('apis.object.','apis.operations.','apis.calls.')),
    ('api-operation','Endpoint group or operation','api-operation',('apis.operations.','apis.calls.')),
    ('package','Package repository or package','package',('packages.',)),
    ('product','Data product','product',('products.',)),
)


def _roles_entry(names, actions_of):
    return [{'name':name,'category':role_category(name),'permissions':actions_of(name)} for name in names]


def role_catalog(db, user):
    from .settings_access import ROLES as SETTINGS_ROLES, SECTIONS, PAGES, ROOT
    from .compliance import REVIEW_PERMISSIONS, ROLES as DUTY_LABELS
    types=[]
    for key,label,kind,prefixes in CATALOG_TYPES:
        fam=family('service' if kind=='service-account' else kind)
        types.append({'key':key,'label':label,'family':fam,
            'roles':_roles_entry([r for r in rbac.roles_for(Resource(kind='folder' if kind=='service-account' else kind)) if _bundle(prefixes,r)],lambda r,p=prefixes:_bundle(p,r))})
    for ident,name in [(ROOT,'All of Administration'),*[(i,n+' pages') for i,n in SECTIONS.items()]]:
        types.append({'key':'settings:'+ident,'label':name,'family':'settings','target_id':ident,
            'pages':[n for p,(n,s) in PAGES.items() if ident in (ROOT,s)],
            'roles':_roles_entry(SETTINGS_ROLES,lambda r,i=ident:_settings_actions(i,r))})
    custom=[]
    if permissions.allowed(db,user,'roles.custom.read','roles') or getattr(rbac.resolve_user(db,user),'is_admin',False):
        for row in db.scalars(select(CustomPermissionRole).order_by(CustomPermissionRole.name)):
            custom.append({'id':row.id,'name':row.name,'category':actions_category(row.actions),'permissions':sorted(row.actions)})
    duties=[{'key':k,'name':DUTY_ROLES[k],'description':DUTY_LABELS.get(k,k),'category':'privileged','permissions':[REVIEW_PERMISSIONS[k]]}
            for k in DUTY_ROLES]
    used={a for t in types for r in t['roles'] for a in r['permissions']}|{a for r in custom for a in r['permissions']}|{d['permissions'][0] for d in duties}
    return {'types':types,'custom_roles':custom,'review_duties':duties,
            'permissions':{row['key']:{k:v for k,v in row.items() if k!='key'} for row in permission_rows(used)}}

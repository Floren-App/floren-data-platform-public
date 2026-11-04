"""005_role_model: move every grant to the role model of November 2026, keeping exactly the access it gave.

Reader and Contributor became platform roles (see an object and its configuration; change its settings), Data Reader
and Data Contributor interact with data, Executor only triggers on-demand work, Viewer only discovers data products.
Manager, PlatformContributor, Consumer, Publisher and Maintainer are retired.

Each grant gets the new roles that carry what it was for (a folder's Executor becomes Executor and Data Reader). Where
those roles together carry less than the old role did (write-only data Contributor, for one), the rest is kept as an
explicit permission grant on the same target, so nobody loses access. Some gain: Contributor now also carries what
Manager and Platform Contributor did, as agreed. The audit event of the migration lists every change, with what was kept
explicitly and what was gained.
"""
import json
from collections import defaultdict
from pathlib import Path
from sqlalchemy import select, text

OLD_BUNDLES = json.loads(Path(__file__).with_name('role_model_2026_11_bundles.json').read_text())
# The old role names that resolved to another role's bundle.
OLD_ALIASES = {'Reader': {'Executor'}, 'Consumer': {'Executor'}, 'Publisher': {'Contributor', 'Executor', 'Manager'},
               'Maintainer': {'Contributor', 'Executor', 'Manager'}}
# What each old role becomes, per kind of object. Roles not listed keep their name.
_DATA = {'Viewer': ['Reader'], 'Reader': ['DataReader'], 'Executor': ['DataReader'], 'Contributor': ['Reader'],
         'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']}
RENAMES = {
    'data': _DATA, 'data_object': _DATA, 'kafka': _DATA,
    'program': {'Viewer': ['Reader'], 'Executor': ['Executor', 'DataReader'], 'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']},
    'service_account': {'Viewer': ['Reader'], 'Executor': ['Executor'], 'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']},
    'api': {'Viewer': ['Reader'], 'Consumer': ['DataContributor'], 'Executor': ['DataContributor'], 'Manager': ['Contributor'],
            'PlatformContributor': ['Contributor']},
    'package': {'Viewer': ['Reader'], 'Executor': ['DataReader'], 'Contributor': ['DataContributor'], 'Manager': ['DataContributor', 'Contributor'],
                'Publisher': ['DataContributor'], 'Maintainer': ['DataContributor', 'Contributor'], 'PlatformContributor': ['Contributor']},
    'product': {'Consumer': ['DataReader'], 'Executor': ['DataReader'], 'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']},
    'settings': {'Viewer': ['Reader'], 'Executor': ['DataReader', 'Executor'], 'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']},
    'reviews': {'Viewer': ['Reader'], 'Executor': [], 'Manager': ['Contributor'], 'PlatformContributor': ['Contributor']},
}
NAMESPACE_FAMILY = {'data': 'data', 'storage': 'data', 'kafka': 'kafka', 'workloads': 'program', 'runs': 'program', 'artifacts': 'program',
                    'logs': 'program', 'workspaces': 'program', 'identities': 'service_account', 'secrets': 'service_account', 'apis': 'api',
                    'packages': 'package', 'products': 'product', 'reviews': 'reviews'}
# One old permission became two.
SPLIT = {'apis.calls.invoke': ['apis.calls.read', 'apis.calls.write']}


def _expand(actions):
    return {new for a in actions for new in SPLIT.get(a, [a])}


def old_bundle(prefixes, role):
    roles = OLD_ALIASES.get(role, {role})
    return _expand(k for k, held in OLD_BUNDLES.items() if k.startswith(prefixes) and roles & set(held))


def new_bundle(prefixes, roles):
    from .permissions import REGISTRY
    return {k for k, d in REGISTRY.items() if k.startswith(prefixes) and set(roles) & set(d['roles'])}


def _family(resource):
    from . import access_inventory
    if resource.kind == 'service' and (resource.config or {}).get('service_account'):
        return 'service_account'
    return access_inventory.family(resource.kind)


def _prefixes(db, resource):
    from . import access_inventory
    if resource.kind in ('setting', 'settings-section', 'settings-root'):
        from .settings_access import PAGES, ROOT, feature_namespaces
        pages = [p for p, (_, section) in PAGES.items() if resource.id in (p, section, ROOT) and p != 'set-backups']
        return tuple({prefix for p in pages for prefix in feature_namespaces().get(p, (p + '.',))}) + ('pages.view',)
    return access_inventory.namespaces(resource, db)


def _explicit(db, gap, target, layer, **recipient):
    """Kept permissions, one grant per feature namespace."""
    from .models import PermissionGrant
    by_namespace = defaultdict(list)
    for action in sorted(gap):
        by_namespace['.'.join(action.split('.')[:2])].append(action)
    for namespace, actions in by_namespace.items():
        db.add(PermissionGrant(target_id=target, namespace=namespace, layer_id=layer, actions=actions, **recipient))


def migrate_assignments(db, report):
    from .models import Resource, RoleAssignment, ServiceRoleAssignment
    from .permissions import resource_layer
    from . import rbac
    for model, key in ((RoleAssignment, 'app_group_id'), (ServiceRoleAssignment, 'principal_id')):
        for row in list(db.scalars(select(model))):
            resource = db.get(Resource, row.resource_id)
            if resource is None:
                continue
            renames = RENAMES.get(_family(resource), {})
            # A role that keeps its name can still mean less now (a workspace Contributor no longer edits files).
            new = [r for r in renames.get(row.role, [row.role]) if r in rbac.roles_for(resource)]
            prefixes = _prefixes(db, resource)
            old, now = old_bundle(prefixes, row.role), new_bundle(prefixes, new)
            if new == [row.role] and not old - now:
                continue
            recipient = {key: getattr(row, key)}
            held = set(db.scalars(select(model.role).where(model.resource_id == row.resource_id, getattr(model, key) == recipient[key],
                                                           model.id != row.id)))
            report.append({'target': row.resource_id, **recipient, 'from': row.role, 'to': new,
                           'kept_permissions': sorted(old - now), 'gained': sorted(now - old)})
            # The row itself becomes the first new role, so its ID stays the same.
            keep = row.role in new
            for role in new:
                if role == row.role or role in held:
                    continue
                if not keep:
                    row.role, keep = role, True
                else:
                    db.add(model(resource_id=row.resource_id, role=role, **recipient))
                held.add(role)
            if old - now:
                _explicit(db, old - now, row.resource_id, resource_layer(db, row.resource_id), **recipient)
            if not keep:
                db.delete(row)
            db.flush()


def migrate_named(db, report):
    """Named permission grants with a built-in role, and explicit action sets that name a split permission."""
    from .models import CustomPermissionRole, PermissionGrant
    for role in db.scalars(select(CustomPermissionRole)):
        if set(role.actions) & SPLIT.keys():
            role.actions = sorted(_expand(role.actions))
    for grant in list(db.scalars(select(PermissionGrant))):
        if grant.actions and set(grant.actions) & SPLIT.keys():
            grant.actions = sorted(_expand(grant.actions))
        if not grant.role:
            continue
        renames = RENAMES.get(NAMESPACE_FAMILY.get(grant.namespace.split('.')[0], 'settings'), {})
        prefix = (grant.namespace + '.',)
        new = [r for r in renames.get(grant.role, [grant.role]) if new_bundle(prefix, [r])]
        old, now = old_bundle(prefix, grant.role), new_bundle(prefix, new)
        if new == [grant.role] and not old - now:
            continue
        recipient = {'app_group_id': grant.app_group_id, 'principal_id': grant.principal_id}
        report.append({'grant': grant.id, 'namespace': grant.namespace, 'from': grant.role, 'to': new,
                       'kept_permissions': sorted(old - now), 'gained': sorted(now - old)})
        # The grant itself becomes the first new role (or the kept permissions), so its ID stays the same.
        keep = grant.role in new
        for role in new:
            if role == grant.role:
                continue
            if not keep:
                grant.role, keep = role, True
            else:
                db.add(PermissionGrant(target_id=grant.target_id, namespace=grant.namespace, layer_id=grant.layer_id, role=role, actions=[], **recipient))
        if old - now:
            if not keep:
                grant.role, grant.actions, keep = None, sorted(old - now), True
            else:
                db.add(PermissionGrant(target_id=grant.target_id, namespace=grant.namespace, layer_id=grant.layer_id, actions=sorted(old - now), **recipient))
        if not keep:
            db.delete(grant)
        db.flush()


def migrate_records(db):
    """Approvals, deadlines, cases and requests name the role they are about."""
    from .models import AccessCase, AccessRequest, JustificationDeadline, JustifiedGrant, Resource
    for model in (JustifiedGrant, JustificationDeadline, AccessCase):
        for row in db.scalars(select(model)):
            resource = db.get(Resource, row.resource_id) if row.resource_id else None
            if resource is None or not row.role:
                continue
            renames = RENAMES.get(_family(resource), {})
            for column in ('role', 'previous_role'):
                value = getattr(row, column, None)
                if value in renames and renames[value]:
                    # The data role is what an approval was about.
                    setattr(row, column, next((r for r in renames[value] if r.startswith('Data')), renames[value][0]))
    for request in db.scalars(select(AccessRequest).where(AccessRequest.role == 'Consumer')):
        request.role = 'DataReader'


def upgrade_role_model(session_factory=None):
    from .db import SessionLocal
    from .models import AuditEvent
    with (session_factory or SessionLocal)() as db:
        db.execute(text("CREATE TABLE IF NOT EXISTS fdp_migrations (revision VARCHAR(64) PRIMARY KEY)"))
        if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='005_role_model'")).first():
            return []
        report = []
        migrate_assignments(db, report)
        migrate_named(db, report)
        migrate_records(db)
        db.add(AuditEvent(actor="system", action="migration.role_model", target="005_role_model", detail={"changes": report}))
        db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('005_role_model')"))
        db.commit()
        return report

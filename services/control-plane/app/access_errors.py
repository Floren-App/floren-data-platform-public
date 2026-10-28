"""Readable data-access failures with navigation scoped to the viewing user."""
from sqlalchemy import select

from . import rbac
from .models import AppGroup, DataSource, DirectoryPrincipal, Resource


def identity_label(db, actor):
    principal = db.get(DirectoryPrincipal, actor.principal_id) if actor.principal_id else None
    if actor.username.startswith("service:"):
        principal = principal or db.get(DirectoryPrincipal, actor.username[8:])
        return f'Service identity for "{principal.name}"' if principal else "This service identity"
    if actor.username.startswith("sa:"):
        return f'Service account "{actor.username[3:]}"'
    return f'User "{principal.name if principal else actor.username}"'


def data_access_issue(db, actor, missing, viewer=None):
    """Resource links are omitted when their target is not visible to the viewer."""
    label = identity_label(db, actor)
    principal_id = actor.principal_id or (actor.username[8:] if actor.username.startswith("service:") else None)
    names = list(dict.fromkeys(missing))
    targets = []
    for name in names:
        source = db.scalar(select(DataSource).where(DataSource.name == name))
        resource = rbac.source_resource(db, source) if source else None
        visible = resource is not None and viewer is not None and rbac.allowed(db, viewer, "read", resource)
        schemas = []
        if resource and viewer and not visible:
            schemas = [{"id": schema.id, "name": schema.name,
                        "can_assign": rbac.allowed(db, viewer, "assign", schema)}
                       for schema in db.scalars(select(Resource).where(Resource.kind == "schema", Resource.parent_id == resource.id))
                       if rbac.allowed(db, viewer, "read", schema)]
        targets.append({"name": name, "resource_id": resource.id if visible else None, "schemas": schemas,
                        "can_assign": bool(visible and rbac.allowed(db, viewer, "assign", resource))})
    groups_manageable = viewer is not None and (viewer.is_admin or any(
        viewer.principal_id in g.owners for g in db.scalars(select(AppGroup))))
    source_names = ", ".join(f'"{name}"' for name in names)
    service = actor.username.startswith(("service:", "sa:"))
    can_assign = any(t["can_assign"] or any(s["can_assign"] for s in t["schemas"]) for t in targets)
    guidance = ("Assign the required data role directly to this service identity on the required schema "
                "(or on the data source for all schemas). App-group grants can also be used. " if service else
                "Assign the required data role to an app security group on the required schema "
                "(or on the data source for all schemas), then bind this identity to that group. ")
    return {"code": "data_access_denied",
            "message": f'{label} has no data access to {source_names}.',
            "guidance": guidance +
                        "Reader allows reads; Contributor allows writes; Admin allows table creation and alteration. "
                        "After updating access, retry the run or redeploy the workload.",
            "identity": {"name": label, "kind": "service" if service else "user",
                         "principal_id": principal_id if groups_manageable or (service and can_assign) else None},
            "can_manage_groups": bool(groups_manageable), "sources": targets}

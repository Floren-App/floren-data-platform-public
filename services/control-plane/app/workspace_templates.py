"""Group defaults for workspaces: each member of a template's group gets their own private workspace from it.

Workspaces are always one person's. A template's workspaces are created stopped (their volume only appears at
first start), also for people who join the group later. Leaving the group removes that person's workspace
(files stay in its volume); a workspace the person deleted is not created again.
"""
import logging
from typing import Dict, Set

from sqlalchemy import select

from .models import (DirectoryMembership, DirectoryPrincipal, IdentityBinding, Workspace, WorkspaceTemplate,
                     WorkspaceTemplateDismissal)

log = logging.getLogger(__name__)


def members(db, group_id: str) -> Dict[str, str]:
    """Active people in an app group, directly or through (nested) directory groups: {principal id: name}."""
    out: Dict[str, str] = {}
    pending = list(db.scalars(select(IdentityBinding.principal_id).where(IdentityBinding.app_group_id == group_id)))
    seen: Set[str] = set()
    while pending:
        ident = pending.pop()
        if ident in seen:
            continue
        seen.add(ident)
        principal = db.get(DirectoryPrincipal, ident)
        if principal is None or not principal.active:
            continue
        if principal.kind == "user":
            out[principal.id] = principal.name
        elif principal.kind == "group":
            pending.extend(db.scalars(select(DirectoryMembership.member_id).where(DirectoryMembership.group_id == ident)))
    return out


def reconcile(db, orch=None) -> dict:
    """Bring template workspaces in line with group memberships; returns {"created": n, "removed": n}."""
    created = removed = 0
    templates = {t.id: t for t in db.scalars(select(WorkspaceTemplate))}
    wanted: Dict[str, Dict[str, str]] = {t.id: members(db, t.group_id) for t in templates.values()}
    dismissed = {(d.template_id, d.principal_id) for d in db.scalars(select(WorkspaceTemplateDismissal))}
    existing = list(db.scalars(select(Workspace).where(Workspace.template_id.is_not(None))))
    have = {(w.template_id, w.principal_id) for w in existing}
    for ws in existing:  # template gone, or no longer a member
        if ws.template_id not in templates or ws.principal_id not in wanted.get(ws.template_id, {}):
            if ws.execution_ref and orch is not None:
                orch.stop(ws.execution_ref)
            db.delete(ws)
            removed += 1
        elif ws.status == "stopped":  # follow template changes while it is not in use
            template = templates[ws.template_id]
            ws.name, ws.editor, ws.environment_id = template.name, template.editor, template.layer_id
    for template in templates.values():
        for principal_id, name in wanted[template.id].items():
            if (template.id, principal_id) in have or (template.id, principal_id) in dismissed:
                continue
            ws = Workspace(owner=name, principal_id=principal_id, environment_id=template.layer_id,
                           owner_group_id=template.group_id, name=template.name, editor=template.editor,
                           template_id=template.id, status="stopped")
            db.add(ws)
            db.flush()
            ws.volume_owner = f"ws-{ws.id}"  # its own home, like every workspace
            created += 1
    db.flush()
    if created or removed:
        log.info("workspace templates: %d created, %d removed", created, removed)
    return {"created": created, "removed": removed}

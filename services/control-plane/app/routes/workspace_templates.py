"""Admin API for group workspace templates (see app/workspace_templates.py)."""
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import User, require_admin
from .. import settings_access
from ..db import get_db
from ..deps import get_orchestrator
from ..models import AppGroup, GitRepository, Resource, Workspace, WorkspaceTemplate
from ..orchestrator import Orchestrator
from .common import audit

router = APIRouter(prefix="/v1/admin/workspace-templates", tags=["workspaces"])


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    group_id: str = Field(min_length=1, max_length=12)
    editor: Literal["vscode", "rstudio", "sql"]
    layer_id: str = Field(min_length=1, max_length=12)
    description: str = Field(default="", max_length=500)
    repositories: List[str] = Field(default_factory=list, description="Repository IDs cloned at first start")


def out(db, t: WorkspaceTemplate) -> dict:
    group = db.get(AppGroup, t.group_id)
    layer = db.get(Resource, t.layer_id)
    count = len(list(db.scalars(select(Workspace.id).where(Workspace.template_id == t.id))))
    return {"id": t.id, "name": t.name, "group_id": t.group_id, "group_name": group.name if group else None,
            "editor": t.editor, "layer_id": t.layer_id, "layer_name": layer.name if layer else None,
            "description": t.description, "repositories": list(t.repositories or []), "workspaces": count}


def _validate(db, body: TemplateIn, template_id: str = "") -> None:
    if db.get(AppGroup, body.group_id) is None:
        raise HTTPException(422, "choose an existing group")
    if body.editor == "rstudio":
        from ..runtimes import require_kind
        require_kind("r")
    from ..environments import workspace_environment
    workspace_environment(db, None, body.layer_id)  # the layer must exist and allow workspaces
    missing = [r for r in body.repositories if db.get(GitRepository, r) is None]
    if missing:
        raise HTTPException(422, f"unknown repositories: {', '.join(missing)}")
    clash = db.scalar(select(WorkspaceTemplate.id).where(WorkspaceTemplate.group_id == body.group_id,
                                                         WorkspaceTemplate.name == body.name.strip()))
    if clash and clash != template_id:
        raise HTTPException(409, "this group already has a template with that name")


def _apply(db, orch) -> None:
    from ..workspace_templates import reconcile
    reconcile(db, orch)


@router.get("")
def list_templates(db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-template", "workspace_templates.list"))):
    return [out(db, t) for t in db.scalars(select(WorkspaceTemplate).order_by(WorkspaceTemplate.name))]


@router.post("", status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-template", "workspace_templates.create")),
                    orch: Orchestrator = Depends(get_orchestrator)):
    _validate(db, body)
    template = WorkspaceTemplate(**{**body.model_dump(), "name": body.name.strip()})
    db.add(template)
    db.flush()
    audit(db, admin.username, "workspace_template.create", template.id, group=body.group_id, editor=body.editor)
    _apply(db, orch)
    db.commit()
    return out(db, template)


@router.get("/{template_id}")
def get_template(template_id: str, db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-template", "workspace_templates.read"))):
    template = db.get(WorkspaceTemplate, template_id)
    if template is None:
        raise HTTPException(404, "workspace template not found")
    return out(db, template)


@router.put("/{template_id}")
def update_template(template_id: str, body: TemplateIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-template", "workspace_templates.apply")),
                    orch: Orchestrator = Depends(get_orchestrator)):
    template = db.get(WorkspaceTemplate, template_id)
    if template is None:
        raise HTTPException(404, "workspace template not found")
    _validate(db, body, template_id)
    for field, value in {**body.model_dump(), "name": body.name.strip()}.items():
        setattr(template, field, value)
    audit(db, admin.username, "workspace_template.update", template.id, group=body.group_id, editor=body.editor)
    _apply(db, orch)  # stopped workspaces follow the change; running ones keep theirs until stopped
    db.commit()
    return out(db, template)


@router.delete("/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-template", "workspace_templates.delete")),
                    orch: Orchestrator = Depends(get_orchestrator)):
    template = db.get(WorkspaceTemplate, template_id)
    if template is None:
        raise HTTPException(404, "workspace template not found")
    db.delete(template)
    db.flush()
    audit(db, admin.username, "workspace_template.delete", template_id)
    _apply(db, orch)  # its workspaces go (files stay in their volumes)
    db.commit()

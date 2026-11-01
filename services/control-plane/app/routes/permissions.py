"""Terraform/UI administration of explicit scoped permission assignments."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .. import permissions as policy, rbac
from ..app_profiles import acting_as_admin
from ..db import get_db
from ..deps import get_actor
from ..models import AppGroup, CustomPermissionRole, DirectoryPrincipal, PermissionGrant, Resource
from .common import audit

router = APIRouter(prefix="/v1/permissions", tags=["permissions"])


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=128)
    layer_id: Optional[str] = Field(default=None, max_length=12)
    app_group_id: Optional[str] = None
    principal_id: Optional[str] = None
    role: Optional[str] = None
    actions: List[str] = Field(default_factory=list)
    custom_role_id: Optional[str] = None

    @model_validator(mode="after")
    def selection(self):
        if bool(self.app_group_id) == bool(self.principal_id):
            raise ValueError("choose exactly one group or principal")
        if sum((bool(self.role), bool(self.actions), bool(self.custom_role_id))) != 1:
            raise ValueError("choose exactly one built-in role, action set or custom role")
        return self


def output(row):
    return {name: getattr(row, name) for name in ("id", "target_id", "namespace", "layer_id", "app_group_id",
            "principal_id", "role", "actions", "custom_role_id")}


def authorize_grant(db, user, body):
    if body.layer_id not in (None, "*"):
        layer = db.get(Resource, body.layer_id)
        if not layer or layer.kind != "layer":
            raise HTTPException(422, "unknown layer")
    actual = policy.resource_layer(db, body.target_id)
    if actual and actual != body.layer_id:
        raise HTTPException(422, "grant must match the target's actual layer")
    if body.app_group_id and not db.get(AppGroup, body.app_group_id):
        raise HTTPException(422, "unknown app group")
    if body.principal_id:
        principal = db.get(DirectoryPrincipal, body.principal_id)
        if not principal or not principal.active:
            raise HTTPException(422, "unknown or inactive principal")
    if body.custom_role_id:
        custom = db.get(CustomPermissionRole, body.custom_role_id)
        if not custom:
            raise HTTPException(422, "unknown custom role")
        actions = policy.validate_actions(custom.actions)
    elif body.role:
        actions = policy.role_actions(body.namespace, body.role)
    else:
        actions = policy.validate_actions(body.actions)
    if any(not action.startswith(body.namespace + ".") for action in actions):
        raise HTTPException(422, "every action must belong to the declared namespace")
    if not policy.can_delegate(db, user, body.namespace, body.target_id, body.layer_id, actions):
        raise HTTPException(403, "Owner of this capability scope required; cannot delegate permissions you do not hold")
    return actions


@router.get("/registry")
def registry(user=Depends(get_actor)):
    return {"roles": list(policy.ROLES), "permissions": policy.REGISTRY}


@router.get("/capabilities")
def capabilities(target_id: str, layer_id: Optional[str] = None, db=Depends(get_db), user=Depends(get_actor)):
    return {"actions": policy.capabilities(db, user, target_id, layer_id)}


@router.post("/assignments", status_code=201)
def create(body: GrantIn, db=Depends(get_db), user=Depends(get_actor)):
    authorize_grant(db, user, body)
    row = PermissionGrant(**body.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.username, "permission.grant", row.id, **output(row))
    db.commit()
    return output(row)


def accessible(db, user, ident):
    row = db.get(PermissionGrant, ident)
    if not row:
        raise HTTPException(404, "assignment not found")
    if not policy.can_delegate(db, user, row.namespace, row.target_id, row.layer_id, policy.grant_actions(db, row)):
        raise HTTPException(403, "Owner of this capability scope required")
    return row


@router.get("/assignments/{ident}")
def read(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    return output(accessible(db, user, ident))


@router.put("/assignments/{ident}")
def update(ident: str, body: GrantIn, db=Depends(get_db), user=Depends(get_actor)):
    row = accessible(db, user, ident)
    authorize_grant(db, user, body)
    before = output(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    audit(db, user.username, "permission.update", row.id, before=before, after=output(row))
    db.commit()
    return output(row)


@router.delete("/assignments/{ident}", status_code=204)
def remove(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    row = accessible(db, user, ident)
    audit(db, user.username, "permission.revoke", row.id, before=output(row))
    db.delete(row)
    db.commit()


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    actions: List[str] = Field(min_length=1)


def role_output(row):
    return {"id": row.id, "name": row.name, "actions": row.actions}


def role_admin(db, user):
    # Definitions affect every grantee. Per-scope Owners delegate explicit actions instead.
    if not acting_as_admin(rbac.resolve_user(db, user)):
        raise HTTPException(403, "Global Admin must be active to change shared role definitions")


@router.post("/roles", status_code=201)
def create_role(body: RoleIn, db=Depends(get_db), user=Depends(get_actor)):
    role_admin(db, user)
    if body.name in policy.ROLES or db.scalar(select(CustomPermissionRole).where(CustomPermissionRole.name == body.name)):
        raise HTTPException(409, "role name is reserved or already exists")
    row = CustomPermissionRole(name=body.name, actions=policy.validate_actions(body.actions))
    db.add(row)
    db.flush()
    audit(db, user.username, "permission.role.create", row.id, **role_output(row))
    db.commit()
    return role_output(row)


@router.get("/roles/{ident}")
def read_role(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    role_admin(db, user)
    row = db.get(CustomPermissionRole, ident)
    if not row:
        raise HTTPException(404, "role not found")
    return role_output(row)


@router.put("/roles/{ident}")
def update_role(ident: str, body: RoleIn, db=Depends(get_db), user=Depends(get_actor)):
    role_admin(db, user)
    row = db.get(CustomPermissionRole, ident)
    if not row:
        raise HTTPException(404, "role not found")
    if body.name in policy.ROLES or db.scalar(select(CustomPermissionRole.id).where(
            CustomPermissionRole.name == body.name, CustomPermissionRole.id != ident)):
        raise HTTPException(409, "role name is reserved or already exists")
    before = role_output(row)
    actions = policy.validate_actions(body.actions)
    for assignment in db.scalars(select(PermissionGrant).where(PermissionGrant.custom_role_id == ident)):
        if any(not action.startswith(assignment.namespace + ".") for action in actions):
            raise HTTPException(422, "role changes must stay within every assignment's declared namespace")
    row.name, row.actions = body.name, actions
    audit(db, user.username, "permission.role.update", row.id, before=before, after=role_output(row))
    db.commit()
    return role_output(row)


@router.delete("/roles/{ident}", status_code=204)
def remove_role(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    role_admin(db, user)
    row = db.get(CustomPermissionRole, ident)
    if not row:
        raise HTTPException(404, "role not found")
    if db.scalar(select(PermissionGrant.id).where(PermissionGrant.custom_role_id == ident)):
        raise HTTPException(409, "role is still assigned")
    audit(db, user.username, "permission.role.delete", row.id, before=role_output(row))
    db.delete(row)
    db.commit()

"""Compliance: forms, the policy grid, compliance roles, cases (justifications and their review) and memberships."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import classification, compliance as cp, layers, notify, rbac, settings_access
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import (AccessCase, AccessCaseFile, AccessForm, AccessFormVersion, AccessPolicy, AppGroup, CatalogProduct,
                      ComplianceRole, DirectoryPrincipal, ProductOption, Resource, utcnow)
from .common import audit

router = APIRouter(prefix="/v1/compliance", tags=["compliance"])


def require_admin_role(db, user):
    if not cp.is_compliance_admin(db, user):
        raise HTTPException(403, "compliance administrator required")


# --- forms ----------------------------------------------------------------------------------------------

class FormIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    definition: Dict[str, Any] = Field(description="{pages: [{id, title, intro, show_if, fields: [{id, label, help, type, required, options, show_if}]}]}")


def form_out(db, form: AccessForm, versions: bool = False) -> dict:
    all_versions = list(db.scalars(select(AccessFormVersion).where(AccessFormVersion.form_id == form.id).order_by(AccessFormVersion.version)))
    published = [v for v in all_versions if v.published]
    draft = next((v for v in reversed(all_versions) if not v.published), None)
    out = {"id": form.id, "name": form.name, "description": form.description,
           "published_version": published[-1].version if published else None, "has_draft": draft is not None,
           "used_by": len(list(db.scalars(select(AccessPolicy.id).where(AccessPolicy.form_id == form.id))))}
    if versions:
        out["versions"] = [{"id": v.id, "version": v.version, "published": v.published, "published_at": v.published_at,
                            "published_by": v.published_by, "definition": v.definition} for v in all_versions]
    return out


@router.get("/forms")
def list_forms(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-forms", "compliance.forms.list")
    return [form_out(db, f) for f in db.scalars(select(AccessForm).order_by(AccessForm.name))]


@router.get("/forms/{form_id}")
def get_form(form_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-forms", "compliance.forms.read")
    form = db.get(AccessForm, form_id)
    if form is None:
        raise HTTPException(404, "form not found")
    return form_out(db, form, versions=True)


@router.put("/forms/{name}")
def put_form(name: str, body: FormIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Save a form's draft (created when missing). Publishing makes it the version new cases use."""
    existing = db.scalar(select(AccessForm).where(AccessForm.name == name))
    settings_access.require(db, user, "set-forms", "compliance.forms.draft.edit" if existing else "compliance.forms.create")
    definition = cp.validate_definition(body.definition)
    form = db.scalar(select(AccessForm).where(AccessForm.name == name))
    if body.name != name and db.scalar(select(AccessForm).where(AccessForm.name == body.name)):
        raise HTTPException(409, f"a form named {body.name} already exists")
    if form is None:
        form = AccessForm(name=body.name)
        db.add(form)
        db.flush()
    form.name, form.description = body.name, body.description
    latest = db.scalar(select(AccessFormVersion).where(AccessFormVersion.form_id == form.id).order_by(AccessFormVersion.version.desc()))
    if latest is None or latest.published:
        latest = AccessFormVersion(form_id=form.id, version=(latest.version + 1) if latest else 1)
        db.add(latest)
    latest.definition = definition
    audit(db, user.username, "compliance.form_draft", form.id, name=form.name, version=latest.version)
    db.commit()
    return form_out(db, form, versions=True)


@router.post("/forms/{form_id}/publish")
def publish_form(form_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-forms", "compliance.forms.publish")
    draft = db.scalar(select(AccessFormVersion).where(AccessFormVersion.form_id == form_id, AccessFormVersion.published.is_(False))
                      .order_by(AccessFormVersion.version.desc()))
    if draft is None:
        raise HTTPException(409, "nothing to publish: the form has no draft")
    draft.published, draft.published_at, draft.published_by = True, utcnow(), rbac.resolve_user(db, user).username
    audit(db, user.username, "compliance.form_published", form_id, version=draft.version)
    db.commit()
    return form_out(db, db.get(AccessForm, form_id), versions=True)


@router.delete("/forms/{form_id}", status_code=204)
def delete_form(form_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-forms", "compliance.forms.delete")
    form = db.get(AccessForm, form_id)
    if form is None:
        raise HTTPException(404, "form not found")
    if db.scalar(select(AccessPolicy.id).where(AccessPolicy.form_id == form_id)):
        raise HTTPException(409, "a policy uses this form")
    version_ids = list(db.scalars(select(AccessFormVersion.id).where(AccessFormVersion.form_id == form_id)))
    if version_ids and db.scalar(select(AccessCase.id).where(AccessCase.form_version_id.in_(version_ids))):
        raise HTTPException(409, "cases were filled in with this form; it stays for their record")
    for v in db.scalars(select(AccessFormVersion).where(AccessFormVersion.form_id == form_id)):
        db.delete(v)
    audit(db, user.username, "compliance.form_deleted", form_id, name=form.name)
    db.delete(form)
    db.commit()


# --- the policy grid ------------------------------------------------------------------------------------

class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["data_owner", "compliance_reviewer"]
    approvals: int = Field(default=1, ge=1, le=5)


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form_id: str = Field(min_length=1, max_length=12)
    steps: List[StepIn] = Field(min_length=1, max_length=5)
    max_days: int = Field(default=365, ge=1, le=3650, description="Longest time approved access lasts")
    review_days: int = Field(default=10, ge=1, le=365, description="Reviews are due within this many days")
    renew_days: int = Field(default=30, ge=1, le=365, description="A renewal opens this many days before access ends")
    grace_days: int = Field(default=30, ge=1, le=365, description="Access that existed before (or was classified higher since) "
                                                                    "is kept this many days for a form to be approved")


def policy_out(db, p: AccessPolicy) -> dict:
    form = db.get(AccessForm, p.form_id)
    return {"id": p.id, "layer_id": p.layer_id, "classification": p.classification, "form_id": p.form_id,
            "form": form.name if form else None, "steps": p.steps, "max_days": p.max_days, "review_days": p.review_days,
            "renew_days": p.renew_days or 30, "grace_days": p.grace_days or 30}


@router.get("/policies")
def list_policies(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-policies", "view")
    """The grid: layers × classifications, with the policy of each filled cell."""
    visible = {l.id for l in layers.all_layers(db) if
               settings_access.allowed(db, user, "set-policies", "compliance.policies.read") or
               settings_access.allowed(db, user, "set-policies", "compliance.policies.read", l.id)}
    return {"layers": [{"id": l.id, "name": l.name} for l in layers.all_layers(db) if l.id in visible],
            "classifications": classification.levels(db),
            "policies": [policy_out(db, p) for p in db.scalars(select(AccessPolicy)) if p.layer_id in visible]}


@router.put("/policies/{layer_id}/{level}")
def put_policy(layer_id: str, level: str, body: PolicyIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-policies", "compliance.policies.apply", layer_id=layer_id)
    layer = db.get(Resource, layer_id)
    if layer is None or layer.kind != "layer":
        raise HTTPException(404, "layer not found")
    classification.require_level(db, level)
    if level == classification.UNCLASSIFIED:
        raise HTTPException(422, "unclassified data cannot need a form; classify it first")
    if cp.current_version(db, body.form_id) is None:
        raise HTTPException(422, "publish the form first")
    policy = db.scalar(select(AccessPolicy).where(AccessPolicy.layer_id == layer_id, AccessPolicy.classification == level))
    if policy is not None and (policy.form_id != body.form_id or
            policy.steps != [s.model_dump() for s in body.steps] or
            body.max_days > policy.max_days or body.review_days > policy.review_days or
            body.grace_days > (policy.grace_days or 30)):
        settings_access.require(db, user, "set-policies", "compliance.policies.weaken", layer_id=layer_id)
    before = policy_out(db, policy) if policy else None
    if policy is None:
        policy = AccessPolicy(layer_id=layer_id, classification=level)
        db.add(policy)
    policy.form_id, policy.steps = body.form_id, [s.model_dump() for s in body.steps]
    policy.max_days, policy.review_days = body.max_days, body.review_days
    policy.renew_days, policy.grace_days = body.renew_days, body.grace_days
    db.flush()
    audit(db, user.username, "compliance.policy", layer_id, classification=level, before=before, after=policy_out(db, policy))
    db.commit()
    return policy_out(db, policy)


@router.delete("/policies/{layer_id}/{level}", status_code=204)
def delete_policy(layer_id: str, level: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-policies", "compliance.policies.remove", layer_id=layer_id)
    policy = db.scalar(select(AccessPolicy).where(AccessPolicy.layer_id == layer_id, AccessPolicy.classification == level))
    if policy is not None:
        audit(db, user.username, "compliance.policy_removed", layer_id, classification=level)
        db.delete(policy)
        db.commit()


# --- compliance roles ----------------------------------------------------------------------------------

class ScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_ids: List[str] = Field(default_factory=list, max_length=200, description="Objects (and what is under them); empty: all")
    layers: List[str] = Field(default_factory=list, max_length=50, description="Layer IDs; empty: all")
    classifications: List[str] = Field(default_factory=list, max_length=20, description="Empty: all")
    group_ids: List[str] = Field(default_factory=list, max_length=200, description="For membership approvers: groups; empty: all")


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["compliance_reviewer", "membership_approver", "compliance_admin"]
    app_group_id: str = Field(min_length=1, max_length=12)
    scope: ScopeIn = Field(default_factory=ScopeIn)


def role_out(db, r: ComplianceRole) -> dict:
    group = db.get(AppGroup, r.app_group_id)
    return {"id": r.id, "role": r.role, "label": cp.ROLES[r.role], "app_group_id": r.app_group_id,
            "group": group.name if group else r.app_group_id, "scope": r.scope or {}}


@router.get("/roles")
def list_roles(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-duties", "compliance.duties.read")
    return {"roles": [role_out(db, r) for r in db.scalars(select(ComplianceRole))],
            "kinds": [{"key": k, "label": v} for k, v in cp.ROLES.items()]}


@router.post("/roles", status_code=201)
def add_role(body: RoleIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Give a group a compliance role within a scope (platform administrators; compliance administrators for the
    reviewer and membership roles)."""
    resolved = rbac.resolve_user(db, user)
    if body.role == "compliance_admin" and not resolved.is_admin:
        raise HTTPException(403, "platform administrator required")
    settings_access.require(db, user, "set-duties", "compliance.duties.appoint")
    if not db.get(AppGroup, body.app_group_id):
        raise HTTPException(422, "choose an existing app group")
    for level in body.scope.classifications:
        classification.require_level(db, level)
    role = ComplianceRole(role=body.role, app_group_id=body.app_group_id, scope=body.scope.model_dump())
    db.add(role)
    db.flush()
    audit(db, resolved.username, "compliance.role_added", body.app_group_id, role=body.role, scope=role.scope)
    db.commit()
    return role_out(db, role)


@router.delete("/roles/{role_id}", status_code=204)
def remove_role(role_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    role = db.get(ComplianceRole, role_id)
    if role is None:
        raise HTTPException(404, "role not found")
    resolved = rbac.resolve_user(db, user)
    if role.role == "compliance_admin" and not resolved.is_admin:
        raise HTTPException(403, "platform administrator required")
    settings_access.require(db, user, "set-duties", "compliance.duties.revoke")
    audit(db, resolved.username, "compliance.role_removed", role.app_group_id, role=role.role)
    db.delete(role)
    db.commit()


# --- requirements and cases ------------------------------------------------------------------------------

class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["product", "role"]
    app_group_id: str = Field(min_length=1, max_length=12)
    product_id: Optional[str] = None
    option_id: Optional[str] = None
    resource_id: Optional[str] = None
    role: Optional[str] = None


def requirement_for(db, target: TargetIn) -> dict:
    if target.kind == "product":
        from .. import product_options
        product = db.get(CatalogProduct, target.product_id or "")
        if product is None:
            raise HTTPException(404, "data product not found")
        option = db.get(ProductOption, target.option_id) if target.option_id else product_options.default_option(db, product.id)
        if option is None or option.product_id != product.id:
            raise HTTPException(422, "choose one of the product's access options")
        return cp.product_requirement(db, product, option)
    resource = db.get(Resource, target.resource_id or "")
    if resource is None:
        raise HTTPException(404, "object not found")
    if target.role not in rbac.roles_for(resource):
        raise HTTPException(422, "choose a valid role for this object")
    return cp.role_requirement(db, resource, target.role, target.app_group_id)


@router.post("/requirement")
def requirement(body: TargetIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Whether giving this access needs a compliance form, and which."""
    return cp.requirement_out(db, requirement_for(db, body))


class CaseIn(TargetIn):
    answers: Dict[str, Any] = Field(default_factory=dict)
    until: Optional[datetime] = Field(default=None, description="Access needed until (at most the policy's maximum)")
    submit: bool = True


def new_case(db, user, target: TargetIn, answers: dict, until: Optional[datetime], submit: bool, request_id: Optional[str] = None) -> AccessCase:
    """A case for this access under the policy that applies; answers checked against the form's current version."""
    req = requirement_for(db, target)
    policy = req["policy"]
    if policy is None:
        raise HTTPException(409, "this access needs no compliance form")
    version = cp.current_version(db, policy.form_id)
    if version is None:
        raise HTTPException(409, "the policy's form has no published version")
    user = rbac.resolve_user(db, user)
    steps = list(policy.steps or [])
    if target.kind == "product" and not any(s["role"] == "data_owner" for s in steps):
        steps = [{"role": "data_owner", "approvals": 1}] + steps  # product owners always accept their consumers
    case = AccessCase(kind=target.kind, requester=user.username, requester_principal_id=user.principal_id or "",
                      app_group_id=target.app_group_id, product_id=target.product_id, option_id=target.option_id,
                      resource_id=target.resource_id, role=target.role or "", layer_id=req["layer_id"], classification=req["classification"],
                      policy_id=policy.id, form_version_id=version.id, steps=steps, until=until, request_id=request_id,
                      answers=cp.validate_answers(version.definition, answers, complete=False))
    if target.kind == "product" and not target.option_id:
        from .. import product_options
        case.option_id = product_options.default_option(db, target.product_id).id
    db.add(case)
    db.flush()
    if submit:
        case.answers = cp.validate_answers(version.definition, answers, complete=True)
        cp.submit(db, case, policy)
    return case


def case_out(db, case: AccessCase, user, full: bool = True) -> dict:
    version = db.get(AccessFormVersion, case.form_version_id) if case.form_version_id else None
    form = db.get(AccessForm, version.form_id) if version else None
    group = db.get(AppGroup, case.app_group_id)
    resource = db.get(Resource, case.resource_id) if case.resource_id else None
    product = db.get(CatalogProduct, case.product_id) if case.product_id else None
    option = db.get(ProductOption, case.option_id) if case.option_id else None
    principal = db.get(DirectoryPrincipal, case.principal_id) if case.principal_id else None
    out = {"id": case.id, "kind": case.kind, "status": case.status, "requester": case.requester,
           "app_group_id": case.app_group_id, "group": group.name if group else case.app_group_id,
           "resource_id": case.resource_id, "resource": {"kind": resource.kind, "name": resource.name} if resource else None,
           "role": case.role, "product_id": case.product_id, "product": product.title if product else None,
           "option": option.name if option else None, "principal": principal.name if principal else None,
           "layer_id": case.layer_id, "layer": (db.get(Resource, case.layer_id).name if case.layer_id and db.get(Resource, case.layer_id) else None),
           "classification": case.classification, "form": form.name if form else None, "form_version": version.version if version else None,
           "steps": case.steps, "step": case.step, "decisions": case.decisions or [], "until": case.until,
           "approved_until": case.approved_until, "due_at": case.due_at, "created_at": case.created_at,
           "submitted_at": case.submitted_at, "decided_at": case.decided_at, "can_review": cp.may_review(db, user, case),
           "renewal_of": case.renewal_of,
           "renewal": db.scalar(select(AccessCase.id).where(AccessCase.renewal_of == case.id,
                                                            AccessCase.status.in_(("draft", "in_review", "changes_requested", "approved")))),
           "is_requester": rbac.resolve_user(db, user).username == case.requester}
    if full:
        out.update({"answers": case.answers or {}, "definition": version.definition if version else None, "comments": case.comments or [],
                    "files": [{"id": f.id, "field_id": f.field_id, "name": f.name, "content_type": f.content_type}
                              for f in db.scalars(select(AccessCaseFile).where(AccessCaseFile.case_id == case.id))],
                    "members": [p.name for p in db.scalars(select(DirectoryPrincipal).where(
                        DirectoryPrincipal.id.in_(cp.members_of(db, case.app_group_id))))] if case.kind != "membership" else []})
    return out


def may_see(db, user, case: AccessCase) -> bool:
    resolved = rbac.resolve_user(db, user)
    if resolved.is_admin or resolved.username == case.requester or cp.may_review(db, user, case) or cp.is_compliance_admin(db, user):
        return True
    target = cp.case_target(db, case)
    resource = db.get(Resource, target) if target else None
    if resource is not None and rbac.allowed(db, user, "assign", resource):
        return True  # the data's owners
    group = db.get(AppGroup, case.app_group_id)
    if group is not None and resolved.principal_id in (group.owners or []):
        return True  # the group's owners
    return cp.has_role(db, user, "compliance_reviewer", resource_id=target, layer_id=case.layer_id, level=case.classification)


def _case(db, case_id, user) -> AccessCase:
    case = db.get(AccessCase, case_id)
    if case is None or not may_see(db, user, case):
        raise HTTPException(404, "case not found")
    return case


@router.post("/cases", status_code=201)
def create_case(body: CaseIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Justify access: for a role on an object (its owners), or a group's use of a product (its members)."""
    resolved = rbac.resolve_user(db, user)
    if body.kind == "role":
        resource = db.get(Resource, body.resource_id or "")
        if resource is None or not rbac.allowed(db, user, "assign", resource):
            raise HTTPException(403, "only the object's owners give roles on it")
    elif body.app_group_id not in rbac.app_groups(db, resolved) and not resolved.is_admin:
        product = db.get(CatalogProduct, body.product_id or "")
        if product is None or not rbac.allowed(db, user, "assign", db.get(Resource, product.access_resource_id)):
            raise HTTPException(422, "ask for one of your own groups")
    if not db.get(AppGroup, body.app_group_id):
        raise HTTPException(422, "choose an existing app group")
    case = new_case(db, user, body, body.answers, body.until, body.submit)
    audit(db, resolved.username, "compliance.case", case.resource_id or case.product_id, case=case.id, kind=case.kind, status=case.status)
    db.commit()
    return case_out(db, case, user)


@router.get("/cases")
def list_cases(box: Literal["mine", "review", "all"] = "mine", status: str = "",
               db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """mine: cases you started; review: cases waiting for your decision; all: every case you may see."""
    resolved = rbac.resolve_user(db, user)
    stmt = select(AccessCase).order_by(AccessCase.created_at.desc())
    if status:
        stmt = stmt.where(AccessCase.status == status)
    out = []
    for case in db.scalars(stmt.limit(1000)):
        if box == "mine" and case.requester != resolved.username:
            continue
        if box == "review" and not cp.may_review(db, user, case):
            continue
        if box == "all" and not may_see(db, user, case):
            continue
        out.append(case_out(db, case, user, full=False))
    return out


@router.get("/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    return case_out(db, _case(db, case_id, user), user)


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: Optional[Dict[str, Any]] = None
    until: Optional[datetime] = None


@router.patch("/cases/{case_id}")
def update_case(case_id: str, body: CasePatch, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The requester changes answers of a draft, or after the reviewer asked for changes."""
    case = _case(db, case_id, user)
    if rbac.resolve_user(db, user).username != case.requester or case.status not in ("draft", "changes_requested"):
        raise HTTPException(409, "answers can be changed in a draft, or when changes are asked for")
    version = db.get(AccessFormVersion, case.form_version_id)
    files = [f.field_id for f in db.scalars(select(AccessCaseFile).where(AccessCaseFile.case_id == case.id))]
    if body.answers is not None:
        case.answers = cp.validate_answers(version.definition, body.answers, files, complete=False)
    if body.until is not None:
        case.until = body.until
    db.commit()
    return case_out(db, case, user)


@router.post("/cases/{case_id}/submit")
def submit_case(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    if rbac.resolve_user(db, user).username != case.requester or case.status not in ("draft", "changes_requested"):
        raise HTTPException(409, "only a draft, or a case with changes asked for, can be sent")
    version = db.get(AccessFormVersion, case.form_version_id)
    files = [f.field_id for f in db.scalars(select(AccessCaseFile).where(AccessCaseFile.case_id == case.id))]
    case.answers = cp.validate_answers(version.definition, case.answers or {}, files, complete=True)
    if case.status == "changes_requested":
        case.status = "in_review"  # back to the step that asked
        notify.case_event(db, case, "review")
    else:
        cp.submit(db, case, db.get(AccessPolicy, case.policy_id) if case.policy_id else None)
    audit(db, case.requester, "compliance.case_submitted", case.resource_id or case.product_id, case=case.id)
    db.commit()
    return case_out(db, case, user)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "changes", "decline"]
    note: str = Field(default="", max_length=2000)
    until: Optional[datetime] = Field(default=None, description="Approve for a shorter time")


@router.post("/cases/{case_id}/decision")
def decide_case(case_id: str, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    if body.decision in ("changes", "decline") and not body.note.strip():
        raise HTTPException(422, "say why, so the requester can act on it")
    cp.decide(db, case, user, body.decision, body.note.strip(), body.until)
    audit(db, rbac.resolve_user(db, user).username, "compliance.decision", case.resource_id or case.product_id, case=case.id,
          decision=body.decision, step=case.step, status=case.status)
    db.commit()
    return case_out(db, case, user)


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


@router.post("/cases/{case_id}/comments")
def comment(case_id: str, body: CommentIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    by = rbac.resolve_user(db, user).username
    case.comments = [*(case.comments or []), {"by": by, "text": body.text.strip(), "at": utcnow().isoformat()}]
    notify.case_event(db, case, "comment", by=by)
    db.commit()
    return case_out(db, case, user)


@router.post("/cases/{case_id}/withdraw")
def withdraw_case(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    if rbac.resolve_user(db, user).username != case.requester or case.status not in ("draft", "in_review", "changes_requested"):
        raise HTTPException(409, "only your own open case can be withdrawn")
    case.status, case.decided_at = "withdrawn", utcnow()
    db.commit()
    return case_out(db, case, user)


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    data: str = Field(description="Base64; at most 5 MB")


@router.post("/cases/{case_id}/files", status_code=201)
def add_file(case_id: str, body: FileIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    if rbac.resolve_user(db, user).username != case.requester or case.status not in ("draft", "changes_requested"):
        raise HTTPException(409, "files are added to a draft, or when changes are asked for")
    version = db.get(AccessFormVersion, case.form_version_id)
    field = next((f for p in version.definition["pages"] for f in p["fields"] if f["id"] == body.field_id), None)
    if field is None or field["type"] != "file":
        raise HTTPException(422, "that question does not take a file")
    try:
        data = base64.b64decode(body.data, validate=True)
    except ValueError:
        raise HTTPException(422, "data must be base64") from None
    if len(data) > cp.MAX_FILE:
        raise HTTPException(413, "files are at most 5 MB")
    for old in db.scalars(select(AccessCaseFile).where(AccessCaseFile.case_id == case.id, AccessCaseFile.field_id == body.field_id)):
        db.delete(old)
    item = AccessCaseFile(case_id=case.id, field_id=body.field_id, name=body.name, content_type=body.content_type, data=data,
                          uploaded_by=case.requester)
    db.add(item)
    db.commit()
    return {"id": item.id, "field_id": item.field_id, "name": item.name}


@router.get("/cases/{case_id}/files/{file_id}")
def get_file(case_id: str, file_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    case = _case(db, case_id, user)
    item = db.get(AccessCaseFile, file_id)
    if item is None or item.case_id != case.id:
        raise HTTPException(404, "file not found")
    return Response(item.data, media_type=item.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{item.name}"', "Cache-Control": "no-store"})


# --- memberships ------------------------------------------------------------------------------------------

@router.get("/memberships")
def pending_memberships(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """People in groups with justified access whose membership waits for your approval."""
    out = []
    declined = {(c.app_group_id, c.principal_id) for c in db.scalars(select(AccessCase).where(
        AccessCase.kind == "membership", AccessCase.status == "declined"))}
    for item in cp.pending_members(db):
        if (item["app_group_id"], item["principal_id"]) in declined:
            continue
        if not cp.has_role(db, user, "membership_approver", group_id=item["app_group_id"]):
            continue
        group, principal = db.get(AppGroup, item["app_group_id"]), db.get(DirectoryPrincipal, item["principal_id"])
        out.append({**item, "group": group.name if group else item["app_group_id"], "principal": principal.name if principal else item["principal_id"]})
    return out


class MembershipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field(default="", max_length=2000)


@router.post("/memberships/{group_id}/{principal_id}")
def decide_membership(group_id: str, principal_id: str, body: MembershipDecision,
                      db: Session = Depends(get_db), user: User = Depends(get_actor)):
    resolved = rbac.resolve_user(db, user)
    if not cp.has_role(db, user, "membership_approver", group_id=group_id):
        raise HTTPException(403, "membership approver required for this group")
    if principal_id == resolved.principal_id:
        raise HTTPException(403, "nobody approves their own membership")
    if principal_id not in cp.members_of(db, group_id):
        raise HTTPException(404, "that person is not in the group")
    case = AccessCase(kind="membership", requester="platform", app_group_id=group_id, principal_id=principal_id,
                      status="approved" if body.approve else "declined", decided_at=utcnow(),
                      decisions=[{"step": 0, "role": "membership_approver", "by": resolved.username,
                                  "decision": "approve" if body.approve else "decline", "note": body.note, "at": utcnow().isoformat()}])
    db.add(case)
    db.flush()
    if body.approve:
        cp._approve_member(db, group_id, principal_id, resolved.username, case.id)
    audit(db, resolved.username, "compliance.membership_" + ("approved" if body.approve else "declined"), group_id, principal=principal_id)
    db.commit()
    return {"app_group_id": group_id, "principal_id": principal_id, "status": case.status}


# --- the access register ----------------------------------------------------------------------------------

REGISTER_COLUMNS = ["group", "access", "kind", "classification", "layer", "form", "requester", "approved_by", "approved_at",
                    "until", "members", "case_id"]


def register_rows(db, user) -> List[dict]:
    """Access that was given because a compliance form was approved, with who approved it and until when."""
    from ..models import JustifiedGrant
    admin = cp.is_compliance_admin(db, user) or settings_access.allowed(db, user, "set-register", "view")
    rows = []
    for grant in db.scalars(select(JustifiedGrant).order_by(JustifiedGrant.created_at.desc())):
        case = db.get(AccessCase, grant.case_id)
        if case is None:
            continue
        target = cp.case_target(db, case) if case.kind == "product" else case.resource_id
        if not admin and not cp.has_role(db, user, "compliance_reviewer", resource_id=target, layer_id=case.layer_id, level=case.classification):
            continue
        out = case_out(db, case, user, full=False)
        access = (f"{out['product']} ({out['option']})" if out["option"] else out["product"]) if case.kind == "product" \
            else f"{grant.role} on {out['resource']['name'] if out['resource'] else grant.resource_id}"
        rows.append({"group": out["group"], "app_group_id": case.app_group_id, "access": access, "kind": case.kind,
                     "classification": case.classification, "layer": out["layer"] or "",
                     "form": f"{out['form']} v{out['form_version']}" if out["form"] else "", "requester": case.requester,
                     "approved_by": ", ".join(sorted({d["by"] for d in case.decisions or [] if d["decision"] == "approve"})),
                     "approved_at": case.decided_at, "until": grant.until, "members": len(cp.members_of(db, case.app_group_id)),
                     "case_id": case.id})
    return rows


@router.get("/register")
def access_register(format: Literal["json", "csv"] = "json", db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The access register: compliance administrators see all of it, reviewers what is in their scope."""
    import csv
    import io
    held = db.scalar(select(ComplianceRole.id).where(ComplianceRole.role == "compliance_reviewer",
                                                     ComplianceRole.app_group_id.in_(list(rbac.app_groups(db, rbac.resolve_user(db, user))))))
    if not (cp.is_compliance_admin(db, user) or held or settings_access.allowed(db, user, "set-register", "view")):
        raise HTTPException(403, "Viewer on Access register, or a review duty, required")
    rows = register_rows(db, user)
    audit(db, rbac.resolve_user(db, user).username, "compliance.register_read", "compliance-register", rows=len(rows), format=format)
    db.commit()
    if format == "json":
        return rows
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REGISTER_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        text = str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text  # no formulas in spreadsheets
    for row in rows:
        writer.writerow({k: cell(v) for k, v in row.items()})
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="access-register.csv"', "Cache-Control": "no-store"})



# --- renewal and deadlines ---------------------------------------------------------------------------------

def may_act_for(db, user, case: AccessCase) -> bool:
    """The requester, or someone who can give the access: the object's or product's owners."""
    resolved = rbac.resolve_user(db, user)
    if resolved.username == case.requester or resolved.is_admin:
        return True
    target = cp.case_target(db, case)
    resource = db.get(Resource, target) if target else None
    return resource is not None and rbac.allowed(db, user, "assign", resource)


@router.post("/cases/{case_id}/renew", status_code=201)
def renew_case(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Start the renewal of approved access now: a draft on the current form, prefilled with the answers."""
    from .. import expiry
    case = _case(db, case_id, user)
    if case.status not in ("approved", "expired") or case.kind == "membership":
        raise HTTPException(409, "only approved or ended access can be renewed")
    if not may_act_for(db, user, case):
        raise HTTPException(403, "the requester or the data's owners renew access")
    renewal = expiry.renew(db, case, by=rbac.resolve_user(db, user).username)
    if renewal is None:
        raise HTTPException(409, "this access needs no compliance form any more")
    audit(db, rbac.resolve_user(db, user).username, "compliance.renewal", case.resource_id or case.product_id, case=renewal.id, renews=case.id)
    db.commit()
    return case_out(db, renewal, user)


@router.get("/deadlines")
def list_deadlines(resource_id: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Access that needs a compliance form and has none yet: kept until its deadline, then removed."""
    from ..models import JustificationDeadline
    admin = cp.is_compliance_admin(db, user)
    stmt = select(JustificationDeadline).order_by(JustificationDeadline.due_at)
    if resource_id:
        stmt = stmt.where(JustificationDeadline.resource_id == resource_id)
    out = []
    for item in db.scalars(stmt):
        resource = db.get(Resource, item.resource_id)
        if resource is None:
            continue
        policy = db.get(AccessPolicy, item.policy_id)
        if not (admin or rbac.allowed(db, user, "assign", resource) or cp.has_role(
                db, user, "compliance_reviewer", resource_id=resource.id, layer_id=policy.layer_id if policy else None, level=item.classification)):
            continue
        group = db.get(AppGroup, item.app_group_id)
        product = db.scalar(select(CatalogProduct).where(CatalogProduct.access_resource_id == resource.id))
        out.append({"resource_id": resource.id, "resource": product.title if product else resource.name, "kind": "product" if product else resource.kind,
                    "product_id": product.id if product else None, "app_group_id": item.app_group_id, "group": group.name if group else item.app_group_id,
                    "role": item.role, "classification": item.classification, "due_at": item.due_at})
    return out

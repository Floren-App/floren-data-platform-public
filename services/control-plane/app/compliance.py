"""Access policies: some access needs a justification (a compliance form) that is reviewed before it is given.

Which access: a policy grid of layers × classifications (AccessPolicy). What counts is what the group would
really see: for a product, the level of its access option (masked columns count lower); for a role on an
object, the highest level of everything under it. Every way of giving access is checked here: product
requests and consumers, roles on the Permissions tab, the API and Terraform (which name an approved case).

A case goes through the policy's review steps (data owners, compliance reviewers in scope); nobody reviews
their own case. Approval gives the access (JustifiedGrant), until a date. People who join the group later get
that access only after their membership is approved (MembershipApproval) by membership approvers."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import select

from . import classification, layers, rbac
from .models import (AccessCase, AccessForm, AccessFormVersion, AccessPolicy, AppGroup, CatalogAsset, CatalogProduct,
                     ComplianceRole, JustifiedGrant, MembershipApproval, ProductAccess, ProductOption, Resource, utcnow)

ROLES = {"compliance_reviewer": "Review compliance forms in scope",
         "membership_approver": "Approve people joining groups with justified access",
         "compliance_admin": "Set up forms, policies, classifications and compliance roles"}
STEP_ROLES = ("data_owner", "compliance_reviewer")
FIELD_TYPES = ("short_text", "long_text", "choice", "multi_choice", "date", "number", "statement", "person", "file")
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_FILE = 5 * 1024 * 1024


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- forms ----------------------------------------------------------------------------------------------

def validate_definition(definition: dict) -> dict:
    """Pages with fields; ids unique; conditions refer to earlier fields."""
    pages = definition.get("pages") or []
    if not pages:
        raise HTTPException(422, "a form needs at least one page")
    seen: Dict[str, dict] = {}
    out_pages = []
    for p, page in enumerate(pages):
        page_id = page.get("id") or f"page_{p + 1}"
        if not _ID.fullmatch(page_id):
            raise HTTPException(422, f"page id {page_id!r}: lower-case letters, digits and _")
        fields = []
        condition = _condition(page.get("show_if"), seen, f"page {page.get('title') or page_id}")
        for field in page.get("fields") or []:
            field_id = field.get("id") or ""
            if not _ID.fullmatch(field_id) or field_id in seen:
                raise HTTPException(422, f"field id {field_id!r} must be unique: lower-case letters, digits and _")
            kind = field.get("type")
            if kind not in FIELD_TYPES:
                raise HTTPException(422, f"{field_id}: type is one of " + ", ".join(FIELD_TYPES))
            options = [str(o) for o in field.get("options") or []]
            if kind in ("choice", "multi_choice") and not options:
                raise HTTPException(422, f"{field_id}: a choice needs options")
            clean = {"id": field_id, "label": (field.get("label") or field_id)[:300], "help": (field.get("help") or "")[:1000],
                     "type": kind, "required": bool(field.get("required")), "options": options,
                     "show_if": _condition(field.get("show_if"), seen, field_id)}
            seen[field_id] = clean
            fields.append(clean)
        out_pages.append({"id": page_id, "title": (page.get("title") or f"Page {p + 1}")[:200],
                          "intro": (page.get("intro") or "")[:2000], "show_if": condition, "fields": fields})
    return {"pages": out_pages}


def _condition(condition, earlier: Dict[str, dict], where: str):
    if not condition:
        return None
    field = condition.get("field")
    if field not in earlier:
        raise HTTPException(422, f"{where}: a condition must refer to an earlier field")
    if "equals" not in condition and "contains" not in condition:
        raise HTTPException(422, f"{where}: a condition says equals or contains")
    return {"field": field, **({"equals": condition["equals"]} if "equals" in condition else {"contains": condition["contains"]})}


def _shown(condition, answers) -> bool:
    if not condition:
        return True
    value = answers.get(condition["field"])
    if "equals" in condition:
        return value == condition["equals"]
    return isinstance(value, list) and condition["contains"] in value


def visible_fields(definition: dict, answers: dict) -> List[dict]:
    out = []
    for page in definition.get("pages") or []:
        if not _shown(page.get("show_if"), answers):
            continue
        out.extend(f for f in page["fields"] if _shown(f.get("show_if"), answers))
    return out


def validate_answers(definition: dict, answers: dict, files: Iterable[str] = (), complete: bool = True) -> dict:
    """Only answers to visible fields are kept; complete=True also requires every required visible field."""
    files = set(files)
    clean = {}
    for field in visible_fields(definition, answers):
        value, fid, kind = answers.get(field["id"]), field["id"], field["type"]
        if kind == "file":
            if fid in files:
                clean[fid] = True
            elif complete and field["required"]:
                raise HTTPException(422, f"{field['label']}: attach a file")
            continue
        if value in (None, "", []):
            if complete and field["required"]:
                raise HTTPException(422, f"{field['label']}: answer this question")
            continue
        if kind in ("short_text", "long_text", "person"):
            value = str(value)[:20000 if kind == "long_text" else 500]
        elif kind == "choice":
            if value not in field["options"]:
                raise HTTPException(422, f"{field['label']}: choose one of the options")
        elif kind == "multi_choice":
            if not isinstance(value, list) or any(v not in field["options"] for v in value):
                raise HTTPException(422, f"{field['label']}: choose from the options")
        elif kind == "date":
            try:
                datetime.fromisoformat(str(value)[:10])
            except ValueError:
                raise HTTPException(422, f"{field['label']}: a date (YYYY-MM-DD)") from None
            value = str(value)[:10]
        elif kind == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{field['label']}: a number") from None
        elif kind == "statement":
            if value is not True:
                if complete and field["required"]:
                    raise HTTPException(422, f"{field['label']}: confirm the statement")
                continue
        clean[fid] = value
    return clean


def current_version(db, form_id: str) -> Optional[AccessFormVersion]:
    return db.scalar(select(AccessFormVersion).where(AccessFormVersion.form_id == form_id, AccessFormVersion.published.is_(True))
                     .order_by(AccessFormVersion.version.desc()))


# --- which policy applies ---------------------------------------------------------------------------------

def policy_for(db, layer_id: Optional[str], level: str) -> Optional[AccessPolicy]:
    if not layer_id or level == classification.UNCLASSIFIED:
        return None
    return db.scalar(select(AccessPolicy).where(AccessPolicy.layer_id == layer_id, AccessPolicy.classification == level))


def _subtree(db, resource: Resource) -> List[Resource]:
    out, frontier = [resource], [resource.id]
    while frontier:
        children = list(db.scalars(select(Resource).where(Resource.parent_id.in_(frontier))))
        out.extend(children)
        frontier = [c.id for c in children]
    return out


def role_level(db, resource: Resource, group_id: Optional[str] = None) -> str:
    """The highest level a role on this object gives: everything under it; table masks that apply to the group count."""
    from .models import DataPolicy
    ids = [r.id for r in _subtree(db, resource)]
    masked: Dict[str, set] = {}
    if group_id:
        for policy in db.scalars(select(DataPolicy).where(DataPolicy.schema_id.in_(ids), DataPolicy.kind == "column_mask")):
            if group_id not in (policy.exempt_groups or []):
                masked.setdefault((policy.schema_id, policy.table_name.lower()), set()).add(policy.column_name.lower())
    levels = []
    for asset in db.scalars(select(CatalogAsset).where(CatalogAsset.resource_id.in_(ids))):
        table = ((asset.attributes or {}).get("object_path") or asset.name).lower()
        levels.append(classification.effective(db, asset, masked.get((asset.resource_id, table), ())))
    return classification.highest(db, levels)


def product_requirement(db, product: CatalogProduct, option: Optional[ProductOption]) -> dict:
    from . import product_options
    from .routes.products import members
    level = product_options.level(db, members(db, product), option)
    return {"layer_id": product.layer_id, "classification": level, "policy": policy_for(db, product.layer_id, level)}


def role_requirement(db, resource: Resource, role: str, group_id: Optional[str]) -> dict:
    if "read" not in rbac.roles_for(resource).get(role, set()):
        return {"layer_id": None, "classification": classification.UNCLASSIFIED, "policy": None}
    layer_id = layers.info(db, resource)["layer_id"]
    level = role_level(db, resource, group_id)
    return {"layer_id": layer_id, "classification": level, "policy": policy_for(db, layer_id, level)}


def requirement_out(db, req: dict) -> dict:
    policy = req["policy"]
    if policy is None:
        return {"required": False, "layer_id": req["layer_id"], "classification": req["classification"]}
    form = db.get(AccessForm, policy.form_id)
    version = current_version(db, policy.form_id)
    return {"required": True, "layer_id": req["layer_id"], "classification": req["classification"], "policy_id": policy.id,
            "form": {"id": form.id, "name": form.name, "version_id": version.id if version else None,
                     "version": version.version if version else None, "definition": version.definition if version else None},
            "steps": policy.steps, "max_days": policy.max_days, "review_days": policy.review_days}


# --- compliance roles ------------------------------------------------------------------------------------

def has_role(db, user, role: str, *, resource_id: Optional[str] = None, layer_id: Optional[str] = None,
             level: Optional[str] = None, group_id: Optional[str] = None) -> bool:
    """Whether the user holds the compliance role for this scope (an empty scope list means all)."""
    user = rbac.resolve_user(db, user)
    if user.is_admin and not user.execution_context:
        return True
    groups = rbac.app_groups(db, user)
    if not groups:
        return False
    return bool(role_groups(db, role, resource_id=resource_id, layer_id=layer_id, level=level, group_id=group_id, among=groups))


def role_groups(db, role: str, *, resource_id: Optional[str] = None, layer_id: Optional[str] = None,
                level: Optional[str] = None, group_id: Optional[str] = None, among=None) -> List[str]:
    """The app groups holding the compliance role for this scope (an empty scope list means all)."""
    ancestors = set()
    if resource_id:
        resource = db.get(Resource, resource_id)
        ancestors = {a.id for a in rbac.ancestors(db, resource)} | {resource_id} if resource else {resource_id}
    stmt = select(ComplianceRole).where(ComplianceRole.role == role)
    if among is not None:
        stmt = stmt.where(ComplianceRole.app_group_id.in_(list(among)))
    out = []
    for item in db.scalars(stmt):
        scope = item.scope or {}
        if scope.get("resource_ids") and not (set(scope["resource_ids"]) & ancestors):
            continue
        if scope.get("layers") and layer_id not in scope["layers"]:
            continue
        if scope.get("classifications") and level not in scope["classifications"]:
            continue
        if scope.get("group_ids") and group_id not in scope["group_ids"]:
            continue
        out.append(item.app_group_id)
    return sorted(set(out))


def is_compliance_admin(db, user) -> bool:
    return has_role(db, user, "compliance_admin")


# --- the review flow ------------------------------------------------------------------------------------

def case_target(db, case: AccessCase) -> Optional[str]:
    """The resource the case's owners own: the object, or the product's access resource."""
    if case.kind == "product":
        product = db.get(CatalogProduct, case.product_id)
        return product.access_resource_id if product else None
    return case.resource_id


def may_review(db, user, case: AccessCase) -> bool:
    """Whether the user may decide the case's current step."""
    user = rbac.resolve_user(db, user)
    if case.status != "in_review" or case.step >= len(case.steps or []):
        return False
    if user.username == case.requester:
        return False  # nobody reviews their own case
    if any(d["step"] == case.step and d["by"] == user.username and d["decision"] == "approve" for d in case.decisions or []):
        return False  # one approval per person per step
    role = case.steps[case.step]["role"]
    if case.kind == "membership":
        return has_role(db, user, "membership_approver", group_id=case.app_group_id, layer_id=case.layer_id, level=case.classification)
    if role == "data_owner":
        target = db.get(Resource, case_target(db, case)) if case_target(db, case) else None
        return bool(target) and rbac.allowed(db, user, "assign", target)
    return has_role(db, user, "compliance_reviewer", resource_id=case_target(db, case) if case.kind == "product" else case.resource_id,
                    layer_id=case.layer_id, level=case.classification)


def submit(db, case: AccessCase, policy: Optional[AccessPolicy]) -> None:
    from . import notify
    case.status, case.step, case.submitted_at = "in_review", 0, utcnow()
    if policy is not None:
        case.due_at = utcnow() + timedelta(days=policy.review_days)
    notify.case_event(db, case, "review")


def decide(db, case: AccessCase, user, decision: str, note: str = "", until: Optional[datetime] = None) -> None:
    """approve | changes | decline the current step; approving the last step gives the access."""
    if not may_review(db, user, case):
        raise HTTPException(403, "you cannot decide this case at its current step")
    user = rbac.resolve_user(db, user)
    step = case.steps[case.step]
    case.decisions = [*(case.decisions or []), {"step": case.step, "role": step["role"], "by": user.username, "decision": decision,
                                                "note": note, "at": utcnow().isoformat()}]
    from . import notify
    if decision == "decline":
        case.status, case.decided_at = "declined", utcnow()
        notify.case_event(db, case, "declined", by=user.username, note=note)
        return
    if decision == "changes":
        case.status = "changes_requested"
        notify.case_event(db, case, "changes", by=user.username, note=note)
        return
    approvals = sum(1 for d in case.decisions if d["step"] == case.step and d["decision"] == "approve")
    if approvals < int(step.get("approvals") or 1):
        return
    if until is not None:
        case.until = min(_aware(case.until), _aware(until)) if case.until else until
    case.step += 1
    if case.step >= len(case.steps):
        approve(db, case, user)
        notify.case_event(db, case, "approved")
    else:
        notify.case_event(db, case, "review")


def approve(db, case: AccessCase, user) -> None:
    """Give the access the case asked for, until the earliest of what was asked and the policy's maximum."""
    from .routes.common import audit
    policy = db.get(AccessPolicy, case.policy_id) if case.policy_id else None
    limit = utcnow() + timedelta(days=policy.max_days) if policy else None
    until = min([d for d in (_aware(case.until), limit) if d], default=None)
    case.status, case.decided_at, case.approved_until = "approved", utcnow(), until
    if case.kind == "membership":
        _approve_member(db, case.app_group_id, case.principal_id, user.username, case.id)
        return
    if case.kind == "product":
        from . import product_options
        from .models import RoleAssignment
        from .routes.products import members
        product = db.get(CatalogProduct, case.product_id)
        if product is None:
            raise HTTPException(409, "the product was unpublished")
        resource_id, role, previous = product.access_resource_id, "Consumer", None
        if db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource_id, RoleAssignment.app_group_id == case.app_group_id)) is None:
            db.add(RoleAssignment(resource_id=resource_id, app_group_id=case.app_group_id, role=role))
        option = db.get(ProductOption, case.option_id) if case.option_id else product_options.default_option(db, product.id)
        product_options.set_option(db, product.id, case.app_group_id, option.id)
        db.flush()
        access = db.scalar(select(ProductAccess).where(ProductAccess.product_id == product.id, ProductAccess.app_group_id == case.app_group_id))
        access.approved_assets = [a.id for a, _ in members(db, product)]
        if case.request_id:
            from .models import AccessRequest
            request = db.get(AccessRequest, case.request_id)
            if request is not None:
                request.status, request.decided_by, request.decided_at = "approved", user.username, utcnow()
    else:
        resource_id, role = case.resource_id, case.role
        previous = apply_role(db, resource_id, case.app_group_id, role)
    grant = db.scalar(select(JustifiedGrant).where(JustifiedGrant.resource_id == resource_id, JustifiedGrant.app_group_id == case.app_group_id))
    if grant is None:
        # A grant that is renewed keeps what the group had before the first approval.
        grant = JustifiedGrant(resource_id=resource_id, app_group_id=case.app_group_id, role=role, case_id=case.id, previous_role=previous)
        db.add(grant)
    grant.role, grant.case_id, grant.until = role, case.id, until
    from .models import JustificationDeadline
    db.query(JustificationDeadline).filter(JustificationDeadline.resource_id == resource_id,
                                           JustificationDeadline.app_group_id == case.app_group_id).delete()
    # The group's members when it was approved were part of what the reviewers saw.
    for principal_id in members_of(db, case.app_group_id):
        _approve_member(db, case.app_group_id, principal_id, "approval of " + case.id, case.id)
    audit(db, user.username, "compliance.approved", resource_id, case=case.id, group=case.app_group_id, role=role,
          until=until.isoformat() if until else None)


def apply_role(db, resource_id: str, group_id: str, role: str) -> Optional[str]:
    """Give the role (never lowering one the group has); returns the lower role it replaced, if any."""
    from .models import RoleAssignment
    resource = db.get(Resource, resource_id)
    roles = list(rbac.roles_for(resource))
    current = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource_id, RoleAssignment.app_group_id == group_id))
    if current is None:
        db.add(RoleAssignment(resource_id=resource_id, app_group_id=group_id, role=role))
        return None
    if current.role in roles and role in roles and roles.index(current.role) < roles.index(role):
        previous, current.role = current.role, role
        return previous  # the lower role the group goes back to when this ends
    return None


def _approve_member(db, group_id: str, principal_id: Optional[str], by: str, case_id: Optional[str]) -> None:
    if not principal_id:
        return
    if db.scalar(select(MembershipApproval).where(MembershipApproval.app_group_id == group_id, MembershipApproval.principal_id == principal_id)) is None:
        db.add(MembershipApproval(app_group_id=group_id, principal_id=principal_id, approved_by=by, case_id=case_id))


def members_of(db, group_id: str) -> List[str]:
    """Principal IDs of the people in an app group (through directory groups too)."""
    from .models import DirectoryMembership, DirectoryPrincipal, IdentityBinding
    bound = set(db.scalars(select(IdentityBinding.principal_id).where(IdentityBinding.app_group_id == group_id)))
    people, frontier = set(), set(bound)
    seen = set()
    while frontier:
        seen |= frontier
        for principal in db.scalars(select(DirectoryPrincipal).where(DirectoryPrincipal.id.in_(frontier))):
            if principal.kind in ("user", "service") and principal.active:
                people.add(principal.id)
        frontier = set(db.scalars(select(DirectoryMembership.member_id).where(DirectoryMembership.group_id.in_(frontier)))) - seen
    return sorted(people)


# --- what members may use -------------------------------------------------------------------------------

def unapproved_justified(db, user, groups) -> set:
    """(resource_id, group_id) of justified grants the user may not use yet: their membership is not approved."""
    if not groups or not getattr(user, "principal_id", None):
        return set()
    justified = list(db.scalars(select(JustifiedGrant).where(JustifiedGrant.app_group_id.in_(list(groups)))))
    if not justified:
        return set()
    approved = set(db.scalars(select(MembershipApproval.app_group_id).where(MembershipApproval.principal_id == user.principal_id,
                                                                          MembershipApproval.app_group_id.in_(list(groups)))))
    return {(g.resource_id, g.app_group_id) for g in justified if g.app_group_id not in approved}


def pending_members(db) -> List[dict]:
    """People in groups with justified access whose membership is not approved yet."""
    out = []
    for group_id in sorted(set(db.scalars(select(JustifiedGrant.app_group_id)))):
        approved = set(db.scalars(select(MembershipApproval.principal_id).where(MembershipApproval.app_group_id == group_id)))
        for principal_id in members_of(db, group_id):
            if principal_id not in approved:
                out.append({"app_group_id": group_id, "principal_id": principal_id})
    return out

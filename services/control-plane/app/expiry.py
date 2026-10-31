"""Justified access ends: at its end date, unless renewed; and access that needs a form but has none (it existed
before the policy, or its data was classified higher since) is kept until a deadline, then removed. Renewals open
before the end, prefilled; reminders and escalations go to the notification webhooks (see notify)."""
import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool

from . import classification, notify, rbac
from .models import (AccessCase, AccessPolicy, AppGroup, CatalogProduct, JustificationDeadline, JustifiedGrant, ProductAccess,
                     ProductOption, Resource, RoleAssignment, utcnow)
from .ownership import GLOBAL_ADMIN

log = logging.getLogger(__name__)
RENEW_DAYS, GRACE_DAYS = 30, 30
REMINDERS = (14, 3)          # days before the end, when no renewal is under review
OPEN = ("draft", "in_review", "changes_requested")
DATA_KINDS = ("source", "schema", "kafka", "topic")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    from .compliance import _aware as aware
    return aware(value)


def _audit(db, action: str, target: str, **detail) -> None:
    from .routes.common import audit
    audit(db, "platform", action, target, **detail)


def _product_of(db, resource_id: str) -> Optional[CatalogProduct]:
    return db.scalar(select(CatalogProduct).where(CatalogProduct.access_resource_id == resource_id))


def remove_access(db, resource_id: str, group_id: str, previous_role: Optional[str] = None) -> None:
    """Take the justified access away: a product's consumer stops being one; a role goes back to what it was."""
    product = _product_of(db, resource_id)
    if product is not None:
        db.execute(delete(RoleAssignment).where(RoleAssignment.resource_id == resource_id, RoleAssignment.app_group_id == group_id,
                                                RoleAssignment.role == "Consumer"))
        db.execute(delete(ProductAccess).where(ProductAccess.product_id == product.id, ProductAccess.app_group_id == group_id))
        return
    current = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource_id, RoleAssignment.app_group_id == group_id))
    if current is None:
        return
    if previous_role:
        current.role = previous_role
    else:
        db.delete(current)


def _tell_owners(db, event: str, resource_id: str, group_id: str, subject: str, text: str, key: Optional[str] = None,
                 case: Optional[AccessCase] = None) -> None:
    """The object's owners and the group's owners (and the requester of the case)."""
    group = db.get(AppGroup, group_id)
    people = notify.people(db, notify.owner_groups(db, resource_id)) | set((group.owners or []) if group else [])
    if case is not None:
        people |= notify.requester(db, case)
    notify.queue(db, event, people, subject, text, case, key=key,
                 link_to=None if case is not None else notify.get_settings().public_url.rstrip("/"))


def _what(db, resource_id: str, group_id: str, role: str) -> str:
    group = db.get(AppGroup, group_id)
    product = _product_of(db, resource_id)
    resource = db.get(Resource, resource_id)
    name = product.title if product else (resource.name if resource else resource_id)
    return f"{'the data product ' + name if product else role + ' on ' + name} for {group.name if group else group_id}"


# --- the end of justified access ------------------------------------------------------------------------

def expire(db, now: datetime) -> int:
    ended = 0
    for grant in list(db.scalars(select(JustifiedGrant).where(JustifiedGrant.until.is_not(None)))):
        if _aware(grant.until) > now:
            continue
        case = db.get(AccessCase, grant.case_id)
        what = _what(db, grant.resource_id, grant.app_group_id, grant.role)
        remove_access(db, grant.resource_id, grant.app_group_id, grant.previous_role)
        if case is not None and case.status == "approved":
            case.status = "expired"
        _audit(db, "compliance.expired", grant.resource_id, group=grant.app_group_id, role=grant.role, case=grant.case_id)
        _tell_owners(db, "access.ended", grant.resource_id, grant.app_group_id, f"Access ended: {what}",
                     f"The approved access to {what} ended on {grant.until:%Y-%m-%d}. A new request needs a new compliance form.",
                     key=f"ended:{grant.case_id}", case=case)
        db.delete(grant)
        ended += 1
    return ended


def renew(db, case: AccessCase, by: Optional[str] = None) -> Optional[AccessCase]:
    """A draft that renews the case: the current form version, prefilled with the answers that still apply."""
    from . import compliance as cp
    from .routes.compliance import TargetIn, requirement_for
    existing = db.scalar(select(AccessCase).where(AccessCase.renewal_of == case.id, AccessCase.status.in_(OPEN + ("approved",))))
    if existing is not None:
        return existing
    target = TargetIn(kind=case.kind, app_group_id=case.app_group_id, product_id=case.product_id, option_id=case.option_id,
                      resource_id=case.resource_id, role=case.role or None)
    req = requirement_for(db, target)
    policy = req["policy"]
    version = cp.current_version(db, policy.form_id) if policy else None
    if version is None:
        return None   # no form needed any more
    known = {f["id"] for p in version.definition["pages"] for f in p["fields"]}
    answers = cp.validate_answers(version.definition, {k: v for k, v in (case.answers or {}).items() if k in known}, complete=False)
    steps = list(policy.steps or [])
    if case.kind == "product" and not any(s["role"] == "data_owner" for s in steps):
        steps = [{"role": "data_owner", "approvals": 1}] + steps
    renewal = AccessCase(kind=case.kind, requester=by or case.requester, requester_principal_id="" if by else case.requester_principal_id,
                         app_group_id=case.app_group_id, product_id=case.product_id, option_id=case.option_id, resource_id=case.resource_id,
                         role=case.role, layer_id=req["layer_id"], classification=req["classification"], policy_id=policy.id,
                         form_version_id=version.id, steps=steps, answers=answers, renewal_of=case.id)
    if by:
        renewal.requester_principal_id = notify.principal_of(db, by) or ""
    db.add(renewal)
    db.flush()
    return renewal


def renewals(db, now: datetime) -> None:
    """Before the end: open a prefilled renewal and tell the requester; remind them while nothing is under review."""
    for grant in db.scalars(select(JustifiedGrant).where(JustifiedGrant.until.is_not(None))):
        case = db.get(AccessCase, grant.case_id)
        if case is None:
            continue
        policy = db.get(AccessPolicy, case.policy_id) if case.policy_id else None
        left = _aware(grant.until) - now
        if left > timedelta(days=(policy.renew_days if policy and policy.renew_days else RENEW_DAYS)):
            continue
        renewal = renew(db, case)
        if renewal is None:
            continue
        what = notify.describe(db, case)
        notify.queue(db, "access.renewal_ready", notify.requester(db, renewal), f"Renew access: {what}",
                     f"Access to {what} ends on {grant.until:%Y-%m-%d}. A renewal is ready, filled in with your earlier answers: "
                     "check them and send it for review.", renewal, key=f"renew:{case.id}")
        due = [d for d in REMINDERS if left <= timedelta(days=d)]
        if renewal.status in ("draft", "changes_requested") and due:
            days = min(due)   # the tightest reminder that applies; each is sent once
            _tell_owners(db, "access.ending", grant.resource_id, grant.app_group_id, f"Access ends in {max(math.ceil(left.total_seconds() / 86400), 0)} days: {what}",
                         f"Access to {what} ends on {grant.until:%Y-%m-%d} and the renewal was not sent for review yet.",
                         key=f"remind:{case.id}:{days}", case=renewal)


def escalate(db, now: datetime) -> None:
    """Reviews due within a day remind the reviewers; overdue ones go to the compliance administrators too."""
    from . import compliance as cp
    for case in db.scalars(select(AccessCase).where(AccessCase.status == "in_review", AccessCase.due_at.is_not(None))):
        due, what = _aware(case.due_at), notify.describe(db, case)
        if now >= due - timedelta(days=1):
            notify.queue(db, "review.due", notify.reviewers(db, case), f"Review due {due:%Y-%m-%d}: {what}",
                         f"The review of {what} is due on {due:%Y-%m-%d}.", case, key=f"due:{case.id}:{case.step}")
        if now >= due:
            notify.queue(db, "review.overdue", notify.people(db, cp.role_groups(db, "compliance_admin")), f"Review overdue: {what}",
                         f"The review of {what} was due on {due:%Y-%m-%d} and waits for {case.steps[case.step]['role'].replace('_', ' ')}s.",
                         case, key=f"overdue:{case.id}:{case.step}")


# --- access that needs a form but has none ------------------------------------------------------------

def _owning_groups(db, resource: Resource) -> set:
    """Groups that own the object (or what it is in): owners do not justify access to their own data."""
    return {(r.config or {}).get("owner_group_id") for r in rbac.ancestors(db, resource)} - {None}


def uncovered(db) -> dict:
    """(resource_id, group_id) → (role, policy, level) for group access under a policy without a matching approved case."""
    from . import compliance as cp
    order = classification.ranks(db)
    out = {}
    for assignment in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id.is_not(None))):
        group = assignment.app_group_id
        resource = db.get(Resource, assignment.resource_id)
        if group == GLOBAL_ADMIN or resource is None:
            continue
        if "assign" in rbac.roles_for(resource).get(assignment.role, set()):
            continue  # the data's owners: they decide who gets it
        if resource.kind == "product":
            product = _product_of(db, resource.id)
            if product is None or assignment.role != "Consumer":
                continue
            access = db.scalar(select(ProductAccess).where(ProductAccess.product_id == product.id, ProductAccess.app_group_id == group))
            option = db.get(ProductOption, access.option_id) if access and access.option_id else None
            req = cp.product_requirement(db, product, option)
        elif resource.kind in DATA_KINDS:
            req = cp.role_requirement(db, resource, assignment.role, group)
        else:
            continue
        policy = req["policy"]
        if policy is None or group in _owning_groups(db, resource):
            continue
        grant = db.scalar(select(JustifiedGrant).where(JustifiedGrant.resource_id == resource.id, JustifiedGrant.app_group_id == group))
        case = db.get(AccessCase, grant.case_id) if grant else None
        if case is not None and case.layer_id == req["layer_id"] and order.get(case.classification, -1) >= order.get(req["classification"], -1):
            continue  # approved for data at least this sensitive
        out[(resource.id, group)] = (assignment.role, policy, req["classification"])
    return out


def deadlines(db, now: datetime) -> None:
    """Give access that needs a form a deadline; remove it when the deadline passes without an approved case."""
    needed = uncovered(db)
    for item in list(db.scalars(select(JustificationDeadline))):
        key = (item.resource_id, item.app_group_id)
        if key not in needed:
            db.delete(item)   # justified meanwhile, or no form needed any more
            continue
        if _aware(item.due_at) <= now:
            what = _what(db, item.resource_id, item.app_group_id, item.role)
            remove_access(db, item.resource_id, item.app_group_id)
            _audit(db, "compliance.unjustified_removed", item.resource_id, group=item.app_group_id, role=item.role,
                   due_at=item.due_at.isoformat())
            _tell_owners(db, "access.removed", item.resource_id, item.app_group_id, f"Access removed: {what}",
                         f"Access to {what} needed an approved compliance form by {item.due_at:%Y-%m-%d} and had none, so it was removed.",
                         key=f"removed:{item.resource_id}:{item.app_group_id}:{item.due_at:%Y%m%d}")
            db.delete(item)
        needed.pop(key)
    for (resource_id, group_id), (role, policy, level) in needed.items():
        due = now + timedelta(days=policy.grace_days or GRACE_DAYS)
        db.add(JustificationDeadline(resource_id=resource_id, app_group_id=group_id, role=role, policy_id=policy.id,
                                     classification=level, due_at=due))
        what = _what(db, resource_id, group_id, role)
        _tell_owners(db, "access.deadline", resource_id, group_id, f"Justify access by {due:%Y-%m-%d}: {what}",
                     f"Access to {what} now needs an approved compliance form ({level} data). It stays until {due:%Y-%m-%d}; "
                     "without an approved form by then it is removed. Open the object's permissions and choose Justify.",
                     key=f"deadline:{resource_id}:{group_id}:{policy.id}")
    db.flush()


def tick(factory, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    with factory() as db:
        for step in (expire, deadlines, renewals, escalate):
            try:
                step(db, now)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("compliance %s failed; retrying next interval", step.__name__)


async def compliance_loop(factory, interval: int) -> None:
    while True:
        try:
            await run_in_threadpool(tick, factory)
        except Exception:
            log.exception("compliance upkeep failed; retrying next interval")
        await asyncio.sleep(max(60, interval))

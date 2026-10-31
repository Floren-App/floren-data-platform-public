"""Notifications: the platform posts events (a review waits, access ends, ...) to the webhooks that are configured,
for example a chat channel or automation that sends email. A message says what happened, who should act and where;
nothing is decided outside the app, and answers to forms are never sent. Without webhooks, the app alone shows
what waits."""
import asyncio
import hashlib
import hmac
import json
import logging
from typing import Iterable, List, Optional

import httpx
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .models import (AccessCase, AccessForm, AccessFormVersion, AppGroup, CatalogProduct, DirectoryPrincipal, Notification,
                     NotificationWebhook, ProductOption, Resource, RoleAssignment, utcnow)

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
EVENTS = {
    "review.waiting": "A compliance form waits for a review step",
    "review.due": "A review is due within a day",
    "review.overdue": "A review is overdue",
    "case.approved": "A compliance form was approved",
    "case.declined": "A compliance form was declined",
    "case.changes_requested": "Reviewers asked for changes",
    "case.comment": "A comment on a compliance form",
    "membership.waiting": "Someone joined a group with justified access",
    "access.renewal_ready": "A renewal is ready before access ends",
    "access.ending": "Access ends soon and the renewal was not sent",
    "access.ended": "Justified access ended",
    "access.deadline": "Existing access needs a compliance form by a deadline",
    "access.removed": "Access without a form was removed at its deadline",
}


def secret_path(webhook_id: str) -> str:
    return f"webhooks/{webhook_id}"


def link(case_id: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/#case={case_id}"


def email_of(db, principal_id: str) -> str:
    """The address of a person: from SCIM (primary email first), the sign-in token, or a name that is one."""
    p = db.get(DirectoryPrincipal, principal_id)
    if p is None or p.kind != "user" or not p.active:
        return ""
    attrs = p.attributes or {}
    emails = [e for e in attrs.get("emails") or [] if isinstance(e, dict) and e.get("value")]
    emails.sort(key=lambda e: not e.get("primary"))
    if emails:
        return str(emails[0]["value"])
    if isinstance(attrs.get("email"), str) and attrs["email"]:
        return attrs["email"]
    return p.name if "@" in p.name else ""


def principal_of(db, username: str) -> Optional[str]:
    return db.scalar(select(DirectoryPrincipal.id).where(DirectoryPrincipal.kind == "user", DirectoryPrincipal.name == username))


def queue(db, event: str, principal_ids: Iterable[str], subject: str, text: str, case: Optional[AccessCase] = None,
          key: Optional[str] = None, link_to: Optional[str] = None) -> int:
    """Put the event in the outbox of every enabled webhook that wants it. With a key, each webhook gets it once."""
    hooks = [h for h in db.scalars(select(NotificationWebhook).where(NotificationWebhook.enabled.is_(True)))
             if not h.events or event in h.events]
    if not hooks:
        return 0
    people = []
    for principal_id in sorted(set(principal_ids)):
        p = db.get(DirectoryPrincipal, principal_id)
        if p is not None and p.kind == "user" and p.active:
            people.append({"username": p.name, "email": email_of(db, principal_id) or None})
    people.sort(key=lambda r: (r["email"] or "", r["username"]))
    payload = {"event": event, "subject": subject[:300], "text": text, "link": link_to or (link(case.id) if case else None),
               "recipients": people, "case": case_summary(db, case) if case else None}
    queued = 0
    for hook in hooks:
        item_key = f"{key}:{hook.id}"[:300] if key else None
        if item_key and db.scalar(select(Notification.id).where(Notification.key == item_key)) is not None:
            continue
        db.add(Notification(key=item_key, webhook_id=hook.id, event=event, payload=payload, case_id=case.id if case else None))
        queued += 1
    db.flush()
    return queued


def case_summary(db, case: AccessCase) -> dict:
    group = db.get(AppGroup, case.app_group_id)
    layer = db.get(Resource, case.layer_id) if case.layer_id else None
    when = lambda v: v.isoformat() if v else None  # noqa: E731
    return {"id": case.id, "kind": case.kind, "status": case.status, "what": describe(db, case), "group": group.name if group else case.app_group_id,
            "classification": case.classification, "layer": layer.name if layer else None, "requester": case.requester,
            "due_at": when(case.due_at), "approved_until": when(case.approved_until)}


# --- who to tell ---------------------------------------------------------------------------------------

def owner_groups(db, resource_id: Optional[str]) -> List[str]:
    """App groups that may give access to the object: a role with "assign" on it or above it."""
    from . import rbac
    resource = db.get(Resource, resource_id) if resource_id else None
    if resource is None:
        return []
    roles = {name for name, actions in rbac.roles_for(resource).items() if "assign" in actions}
    chain = [r.id for r in rbac.ancestors(db, resource)]
    return sorted({a.app_group_id for a in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id.in_(chain)))
                   if a.app_group_id and a.role in roles})


def people(db, groups: Iterable[str]) -> set:
    from .compliance import members_of
    return {p for g in groups for p in members_of(db, g)}


def reviewers(db, case: AccessCase) -> set:
    """The people who may decide the case's current step (never the requester)."""
    from . import compliance as cp
    if case.status != "in_review" or case.step >= len(case.steps or []):
        return set()
    role = case.steps[case.step]["role"]
    if case.kind == "membership":
        groups = cp.role_groups(db, "membership_approver", group_id=case.app_group_id, layer_id=case.layer_id, level=case.classification)
    elif role == "data_owner":
        groups = owner_groups(db, cp.case_target(db, case))
    else:
        target = cp.case_target(db, case) if case.kind == "product" else case.resource_id
        groups = cp.role_groups(db, "compliance_reviewer", resource_id=target, layer_id=case.layer_id, level=case.classification)
    if not groups and role != "data_owner":
        groups = cp.role_groups(db, "compliance_admin")  # nobody in scope: the compliance administrators
    approved = {d["by"] for d in case.decisions or [] if d["step"] == case.step and d["decision"] == "approve"}
    return {p for p in people(db, groups)
            if p != case.requester_principal_id and db.get(DirectoryPrincipal, p).name not in approved}


def requester(db, case: AccessCase) -> set:
    p = case.requester_principal_id or principal_of(db, case.requester)
    return {p} if p else set()


# --- what to say -----------------------------------------------------------------------------------------

def describe(db, case: AccessCase) -> str:
    group = db.get(AppGroup, case.app_group_id)
    name = group.name if group else case.app_group_id
    if case.kind == "product":
        product = db.get(CatalogProduct, case.product_id) if case.product_id else None
        option = db.get(ProductOption, case.option_id) if case.option_id else None
        return f"the data product {product.title if product else case.product_id}" + (f" ({option.name})" if option else "") + f" for {name}"
    if case.kind == "membership":
        person = db.get(DirectoryPrincipal, case.principal_id) if case.principal_id else None
        return f"{person.name if person else 'someone'} joining {name}"
    resource = db.get(Resource, case.resource_id) if case.resource_id else None
    return f"{case.role} on {resource.name if resource else case.resource_id} for {name}"


def _facts(db, case: AccessCase) -> str:
    layer = db.get(Resource, case.layer_id) if case.layer_id else None
    version = db.get(AccessFormVersion, case.form_version_id) if case.form_version_id else None
    form = db.get(AccessForm, version.form_id) if version else None
    lines = [f"Classification: {case.classification}" + (f" in {layer.name}" if layer else "")]
    if form:
        lines.append(f"Form: {form.name} (version {version.version})")
    if case.due_at:
        lines.append(f"Review due: {case.due_at:%Y-%m-%d}")
    return "\n".join(lines)


def _body(db, case: AccessCase, lead: str) -> str:
    return f"{lead}\n\n{_facts(db, case)}\n\nOpen the case: {link(case.id)}"


def case_event(db, case: AccessCase, event: str, by: str = "", note: str = "") -> None:
    """Tell the people who act next. Events: review (waits for a step), approved, declined, changes, comment."""
    what = describe(db, case)
    if event == "review":
        queue(db, "review.waiting", reviewers(db, case), f"Review needed: {what}",
              _body(db, case, f"{case.requester} asks for {what}. It waits for your review."), case,
              key=f"review:{case.id}:{case.step}:{len(case.decisions or [])}")
    elif event == "approved":
        until = f" until {case.approved_until:%Y-%m-%d}" if case.approved_until else ""
        queue(db, "case.approved", requester(db, case), f"Approved: {what}", _body(db, case, f"Your request for {what} is approved{until}."), case)
    elif event == "declined":
        queue(db, "case.declined", requester(db, case), f"Declined: {what}", _body(db, case, f"{by} declined your request for {what}: {note}"), case)
    elif event == "changes":
        queue(db, "case.changes_requested", requester(db, case), f"Changes asked: {what}",
              _body(db, case, f"{by} asks you to change your request for {what}: {note}"), case)
    elif event == "comment":
        to = reviewers(db, case) if by == case.requester else requester(db, case)
        queue(db, "case.comment", to - {principal_of(db, by)}, f"New comment: {what}", _body(db, case, f"{by} commented on the request for {what}."), case)


def membership_notices(db) -> int:
    """Tell membership approvers once about each person waiting to use a group's justified access."""
    from . import compliance as cp
    sent = 0
    for item in cp.pending_members(db):
        group = db.get(AppGroup, item["app_group_id"])
        person = db.get(DirectoryPrincipal, item["principal_id"])
        groups = cp.role_groups(db, "membership_approver", group_id=item["app_group_id"]) or cp.role_groups(db, "compliance_admin")
        name = group.name if group else item["app_group_id"]
        text = (f"{person.name if person else 'Someone'} joined {name}. The group has access that was given through a compliance "
                f"form; they get it once a membership approver approves them.")
        sent += queue(db, "membership.waiting", people(db, groups) - {item["principal_id"]}, f"Membership to approve: {name}", text,
                      key=f"member:{item['app_group_id']}:{item['principal_id']}",
                      link_to=f"{get_settings().public_url.rstrip('/')}/#memberships")
    return sent


# --- sending -----------------------------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: dict) -> int:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        return client.post(url, content=body, headers=headers).status_code


def deliver(db, secrets=None, limit: int = 50) -> int:
    """Post what waits in the outbox; failures are retried a few times, then kept with their error.
    With a signing secret, X-FDP-Signature is sha256=<HMAC-SHA256 of the body>."""
    waiting = list(db.scalars(select(Notification).where(Notification.sent_at.is_(None), Notification.attempts < MAX_ATTEMPTS)
                              .order_by(Notification.id).limit(limit)))
    sent = 0
    for item in waiting:
        hook = db.get(NotificationWebhook, item.webhook_id)
        if hook is not None and hook.enabled:
            sent += deliver_one(db, item, hook, secrets)
    return sent


def deliver_one(db, item: Notification, hook: NotificationWebhook, secrets=None) -> int:
    body = json.dumps({"id": item.id, "at": item.created_at.isoformat(), **item.payload}, default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "fdp-notifications", "X-FDP-Event": item.event,
               "X-FDP-Delivery": str(item.id)}
    if hook.has_secret and secrets is not None:
        secret = (secrets.read(secret_path(hook.id)) or {}).get("secret", "")
        if secret:
            headers["X-FDP-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        status = _post(hook.url, body, headers)
        if 200 <= status < 300:
            item.sent_at, item.error = utcnow(), ""
            return 1
        item.error = f"HTTP {status}"
    except httpx.HTTPError as exc:
        item.error = f"{type(exc).__name__}: {exc}"[:500]
    item.attempts += 1
    return 0


def tick(factory) -> None:
    from .deps import get_secret_store
    with factory() as db:
        membership_notices(db)
        db.commit()
        deliver(db, get_secret_store())
        db.commit()


async def notification_loop(factory, interval: int) -> None:
    while True:
        try:
            await run_in_threadpool(tick, factory)
        except Exception:
            log.exception("sending notifications failed; retrying next interval")
        await asyncio.sleep(max(5, interval))

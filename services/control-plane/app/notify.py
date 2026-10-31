"""Email notifications for compliance reviews. Emails only say what waits and link to the app; nothing is
decided by email, and answers to forms never leave the platform in an email."""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, List, Optional

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .models import (AccessCase, AccessForm, AccessFormVersion, AppGroup, CatalogProduct, DirectoryPrincipal, Notification,
                     ProductOption, Resource, RoleAssignment, utcnow)

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def enabled() -> bool:
    return bool(get_settings().smtp_host)


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


def queue(db, principal_ids: Iterable[str], subject: str, body: str, case_id: Optional[str] = None, key: Optional[str] = None) -> int:
    """Put emails in the outbox (sent by the notification loop). With a key, each person gets it once."""
    if not enabled():
        return 0
    sent = 0
    for address in sorted({email_of(db, p) for p in principal_ids} - {""}):
        item_key = f"{key}:{address}"[:300] if key else None
        if item_key and db.scalar(select(Notification.id).where(Notification.key == item_key)) is not None:
            continue
        db.add(Notification(key=item_key, recipient=address, subject=subject[:300], body=body, case_id=case_id))
        sent += 1
    db.flush()
    return sent


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
    return (f"{lead}\n\n{_facts(db, case)}\n\nOpen the case: {link(case.id)}\n\n"
            "This email only lets you know. Decisions are made in the app.")


def case_event(db, case: AccessCase, event: str, by: str = "", note: str = "") -> None:
    """Tell the people who act next. Events: review (waits for a step), approved, declined, changes, comment."""
    what = describe(db, case)
    if event == "review":
        queue(db, reviewers(db, case), f"Review needed: {what}",
              _body(db, case, f"{case.requester} asks for {what}. It waits for your review."), case.id,
              key=f"review:{case.id}:{case.step}:{len(case.decisions or [])}")
    elif event == "approved":
        until = f" until {case.approved_until:%Y-%m-%d}" if case.approved_until else ""
        queue(db, requester(db, case), f"Approved: {what}", _body(db, case, f"Your request for {what} is approved{until}."), case.id)
    elif event == "declined":
        queue(db, requester(db, case), f"Declined: {what}", _body(db, case, f"{by} declined your request for {what}: {note}"), case.id)
    elif event == "changes":
        queue(db, requester(db, case), f"Changes asked: {what}",
              _body(db, case, f"{by} asks you to change your request for {what}: {note}"), case.id)
    elif event == "comment":
        to = reviewers(db, case) if by == case.requester else requester(db, case)
        queue(db, to - {principal_of(db, by)}, f"New comment: {what}", _body(db, case, f"{by} commented on the request for {what}."), case.id)


def membership_notices(db) -> int:
    """Tell membership approvers once about each person waiting to use a group's justified access."""
    from . import compliance as cp
    sent = 0
    for item in cp.pending_members(db):
        group = db.get(AppGroup, item["app_group_id"])
        person = db.get(DirectoryPrincipal, item["principal_id"])
        groups = cp.role_groups(db, "membership_approver", group_id=item["app_group_id"]) or cp.role_groups(db, "compliance_admin")
        name = group.name if group else item["app_group_id"]
        body = (f"{person.name if person else 'Someone'} joined {name}. The group has access that was given through a compliance "
                f"form; they get it once a membership approver approves them.\n\n"
                f"Open memberships: {get_settings().public_url.rstrip('/')}/#memberships\n\n"
                "This email only lets you know. Decisions are made in the app.")
        sent += queue(db, people(db, groups) - {item["principal_id"]}, f"Membership to approve: {name}", body,
                      key=f"member:{item['app_group_id']}:{item['principal_id']}")
    return sent


# --- sending -----------------------------------------------------------------------------------------------

def _connect(s):
    if s.smtp_security == "tls":
        smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        if s.smtp_security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
    if s.smtp_username:
        smtp.login(s.smtp_username, s.smtp_password)
    return smtp


def deliver(db, limit: int = 50) -> int:
    """Send what waits in the outbox; failures are retried a few times, then kept with their error."""
    s = get_settings()
    if not s.smtp_host:
        return 0
    waiting = list(db.scalars(select(Notification).where(Notification.sent_at.is_(None), Notification.attempts < MAX_ATTEMPTS)
                              .order_by(Notification.id).limit(limit)))
    if not waiting:
        return 0
    sent = 0
    try:
        smtp = _connect(s)
    except (OSError, smtplib.SMTPException) as exc:
        for item in waiting:
            item.attempts, item.error = item.attempts + 1, f"connect: {exc}"[:500]
        log.warning("SMTP connection failed: %s", exc)
        return 0
    with smtp:
        for item in waiting:
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = s.smtp_from or s.smtp_username, item.recipient, item.subject
            message.set_content(item.body)
            try:
                smtp.send_message(message)
                item.sent_at, item.error = utcnow(), ""
                sent += 1
            except smtplib.SMTPException as exc:
                item.attempts, item.error = item.attempts + 1, str(exc)[:500]
    return sent


def tick(factory) -> None:
    with factory() as db:
        membership_notices(db)
        db.commit()
        deliver(db)
        db.commit()


async def notification_loop(factory, interval: int) -> None:
    while True:
        try:
            await run_in_threadpool(tick, factory)
        except Exception:
            log.exception("sending notifications failed; retrying next interval")
        await asyncio.sleep(max(5, interval))

"""Notification webhooks: where the platform posts what happens (administrators; Terraform: fdp_notification_webhook)."""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import notify
from ..auth import User, require_admin
from ..deps import get_actor
from .. import rbac
from .. import settings_access
from ..db import get_db
from ..deps import get_secret_store
from ..models import AlertRule, Notification, NotificationWebhook, Resource
from ..secrets import SecretStore
from .common import audit

router = APIRouter(tags=["notifications"])


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000, description="http(s) URL the platform posts JSON to")
    events: List[str] = Field(default_factory=list, max_length=50,
                              description="Events this channel always gets, everywhere; empty: only what alert rules send it")
    enabled: bool = True
    secret: Optional[str] = Field(default=None, min_length=16, max_length=512, description="Write-only signing secret; "
                                  "X-FDP-Signature is sha256=<HMAC-SHA256 of the body>. Omit to keep the current one.")
    remove_secret: bool = False


def webhook_out(db, hook: NotificationWebhook) -> dict:
    last = db.scalar(select(Notification).where(Notification.webhook_id == hook.id).order_by(Notification.id.desc()).limit(1))
    failing = db.scalar(select(Notification.id).where(Notification.webhook_id == hook.id, Notification.sent_at.is_(None),
                                                      Notification.attempts >= notify.MAX_ATTEMPTS).limit(1))
    return {"id": hook.id, "name": hook.name, "url": hook.url, "events": hook.events or [], "enabled": hook.enabled,
            "has_secret": hook.has_secret, "created_at": hook.created_at,
            "last": {"event": last.event, "at": last.created_at, "sent_at": last.sent_at, "error": last.error} if last else None,
            "failing": failing is not None}


def _hook(db, name: str) -> NotificationWebhook:
    hook = db.scalar(select(NotificationWebhook).where(NotificationWebhook.name == name))
    if hook is None:
        raise HTTPException(404, "webhook not found")
    return hook


@router.get("/v1/admin/webhooks")
def list_webhooks(db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-alerts", "view"))):
    return {"webhooks": [webhook_out(db, h) for h in db.scalars(select(NotificationWebhook).order_by(NotificationWebhook.name))],
            "events": [{"key": k, "label": v} for k, v in notify.EVENTS.items()],
            "groups": notify.EVENT_GROUPS}


@router.put("/v1/admin/webhooks/{name}")
def put_webhook(name: str, body: WebhookIn, db: Session = Depends(get_db), user: User = Depends(settings_access.needs("set-alerts", "manage")),
                secrets: SecretStore = Depends(get_secret_store)):
    """Create or change a webhook by name (a different body name renames it)."""
    parts = urlsplit(body.url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise HTTPException(422, "the URL must start with https:// (or http:// inside your network)")
    unknown = set(body.events) - set(notify.EVENTS)
    if unknown:
        raise HTTPException(422, "unknown events: " + ", ".join(sorted(unknown)))
    hook = db.scalar(select(NotificationWebhook).where(NotificationWebhook.name == name))
    if body.name != name and db.scalar(select(NotificationWebhook).where(NotificationWebhook.name == body.name)):
        raise HTTPException(409, f"a webhook named {body.name} already exists")
    if hook is None:
        hook = NotificationWebhook(name=body.name, url=body.url)
        db.add(hook)
        db.flush()
    hook.name, hook.url, hook.events, hook.enabled = body.name, body.url, sorted(set(body.events)), body.enabled
    if body.secret:
        secrets.put(notify.secret_path(hook.id), {"secret": body.secret})
        hook.has_secret = True
    elif body.remove_secret and hook.has_secret:
        secrets.delete(notify.secret_path(hook.id))
        hook.has_secret = False
    audit(db, user.username, "webhook.put", hook.id, name=hook.name, url=hook.url, events=hook.events, enabled=hook.enabled,
          secret="changed" if body.secret else "removed" if body.remove_secret else "kept")
    db.commit()
    return webhook_out(db, hook)


@router.delete("/v1/admin/webhooks/{name}", status_code=204)
def delete_webhook(name: str, db: Session = Depends(get_db), user: User = Depends(settings_access.needs("set-alerts", "manage")),
                   secrets: SecretStore = Depends(get_secret_store)):
    hook = _hook(db, name)
    if hook.has_secret:
        secrets.delete(notify.secret_path(hook.id))
    db.execute(delete(Notification).where(Notification.webhook_id == hook.id))
    db.execute(delete(AlertRule).where(AlertRule.channel_id == hook.id))
    audit(db, user.username, "webhook.delete", hook.id, name=hook.name)
    db.delete(hook)
    db.commit()


@router.post("/v1/admin/webhooks/{name}/test")
def test_webhook(name: str, db: Session = Depends(get_db), user: User = Depends(settings_access.needs("set-alerts", "manage")),
                 secrets: SecretStore = Depends(get_secret_store)):
    """Post a test message now and say how it went."""
    hook = _hook(db, name)
    item = Notification(webhook_id=hook.id, event="test", payload={
        "event": "test", "subject": "Test from the Floren Data Platform", "text": f"{user.username} sent a test to {hook.name}.",
        "link": None, "recipients": [], "case": None})
    db.add(item)
    db.flush()
    notify.deliver_one(db, item, hook, secrets)   # also when the webhook is switched off
    db.commit()
    return {"sent": item.sent_at is not None, "error": item.error}


@router.get("/v1/admin/webhooks/{name}/deliveries")
def deliveries(name: str, db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-alerts", "view"))):
    """The latest messages to this webhook and how they went."""
    hook = _hook(db, name)
    return [{"id": n.id, "event": n.event, "subject": (n.payload or {}).get("subject", ""), "created_at": n.created_at,
             "sent_at": n.sent_at, "attempts": n.attempts, "error": n.error}
            for n in db.scalars(select(Notification).where(Notification.webhook_id == hook.id).order_by(Notification.id.desc()).limit(50))]



# --- alert rules: which events go to which channel, for which layers, objects and classifications ---------------

class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    events: List[str] = Field(min_length=1, max_length=50)
    channel_id: str = Field(min_length=1, max_length=12)
    layer_ids: List[str] = Field(default_factory=list, max_length=50, description="Empty: every layer")
    resource_ids: List[str] = Field(default_factory=list, max_length=200, description="Objects, and what is under them; empty: all")
    classifications: List[str] = Field(default_factory=list, max_length=20, description="Empty: all")
    enabled: bool = True


def rule_out(db, rule: AlertRule) -> dict:
    channel = db.get(NotificationWebhook, rule.channel_id)
    objects = [db.get(Resource, r) for r in rule.resource_ids or []]
    return {"id": rule.id, "name": rule.name, "events": rule.events, "channel_id": rule.channel_id, "channel": channel.name if channel else None,
            "layer_ids": rule.layer_ids or [], "resource_ids": rule.resource_ids or [],
            "objects": [{"id": o.id, "name": o.name, "kind": o.kind} for o in objects if o is not None],
            "classifications": rule.classifications or [], "enabled": rule.enabled, "created_by": rule.created_by}


def may_manage(db, user, resource_ids) -> bool:
    """Manager on Alerts manages every rule; owners manage rules on their own objects."""
    if settings_access.allowed(db, user, "set-alerts", "manage"):
        return True
    return bool(resource_ids) and all(rbac.allowed(db, user, "assign", db.get(Resource, r)) for r in resource_ids)


def _check(db, body: RuleIn, user) -> None:
    unknown = set(body.events) - set(notify.EVENTS)
    if unknown:
        raise HTTPException(422, "unknown events: " + ", ".join(sorted(unknown)))
    if db.get(NotificationWebhook, body.channel_id) is None:
        raise HTTPException(422, "choose an existing channel")
    if any(db.get(Resource, r) is None for r in body.resource_ids):
        raise HTTPException(422, "choose existing objects")
    if not may_manage(db, user, body.resource_ids):
        raise HTTPException(403, "Manager on Alerts, or owning every object the rule names, required")


@router.get("/v1/alerts/events")
def alert_events(_: User = Depends(get_actor)):
    return {"events": [{"key": k, "label": v} for k, v in notify.EVENTS.items()], "groups": notify.EVENT_GROUPS}


@router.get("/v1/alerts/channels")
def alert_channels(db: Session = Depends(get_db), _: User = Depends(get_actor)):
    """The channels rules can send to (names only)."""
    return [{"id": h.id, "name": h.name, "enabled": h.enabled} for h in db.scalars(select(NotificationWebhook).order_by(NotificationWebhook.name))]


@router.get("/v1/alerts/rules")
def list_rules(resource_id: str = "", db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Every rule for Alerts viewers; otherwise the rules on objects you own. With resource_id: rules naming it."""
    everything = settings_access.allowed(db, user, "set-alerts", "view")
    out = []
    for rule in db.scalars(select(AlertRule).order_by(AlertRule.name)):
        if resource_id and resource_id not in (rule.resource_ids or []):
            continue
        if everything or may_manage(db, user, rule.resource_ids or []):
            out.append(rule_out(db, rule))
    return out


@router.post("/v1/alerts/rules", status_code=201)
def create_rule(body: RuleIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    _check(db, body, user)
    rule = AlertRule(**body.model_dump(), created_by=rbac.resolve_user(db, user).username)
    db.add(rule)
    db.flush()
    audit(db, rule.created_by, "alert.rule_created", rule.id, name=rule.name, events=rule.events, channel=rule.channel_id)
    db.commit()
    return rule_out(db, rule)


@router.put("/v1/alerts/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    rule = db.get(AlertRule, rule_id)
    if rule is None or not may_manage(db, user, rule.resource_ids or []):
        raise HTTPException(404, "rule not found")
    _check(db, body, user)
    for key, value in body.model_dump().items():
        setattr(rule, key, value)
    audit(db, rbac.resolve_user(db, user).username, "alert.rule_changed", rule.id, name=rule.name, events=rule.events)
    db.commit()
    return rule_out(db, rule)


@router.delete("/v1/alerts/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    rule = db.get(AlertRule, rule_id)
    if rule is None or not may_manage(db, user, rule.resource_ids or []):
        raise HTTPException(404, "rule not found")
    audit(db, rbac.resolve_user(db, user).username, "alert.rule_deleted", rule.id, name=rule.name)
    db.delete(rule)
    db.commit()

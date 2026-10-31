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
from ..db import get_db
from ..deps import get_secret_store
from ..models import Notification, NotificationWebhook
from ..secrets import SecretStore
from .common import audit

router = APIRouter(tags=["notifications"])


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000, description="http(s) URL the platform posts JSON to")
    events: List[str] = Field(default_factory=list, max_length=50, description="Events to send; empty: all")
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
def list_webhooks(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return {"webhooks": [webhook_out(db, h) for h in db.scalars(select(NotificationWebhook).order_by(NotificationWebhook.name))],
            "events": [{"key": k, "label": v} for k, v in notify.EVENTS.items()]}


@router.put("/v1/admin/webhooks/{name}")
def put_webhook(name: str, body: WebhookIn, db: Session = Depends(get_db), user: User = Depends(require_admin),
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
def delete_webhook(name: str, db: Session = Depends(get_db), user: User = Depends(require_admin),
                   secrets: SecretStore = Depends(get_secret_store)):
    hook = _hook(db, name)
    if hook.has_secret:
        secrets.delete(notify.secret_path(hook.id))
    db.execute(delete(Notification).where(Notification.webhook_id == hook.id))
    audit(db, user.username, "webhook.delete", hook.id, name=hook.name)
    db.delete(hook)
    db.commit()


@router.post("/v1/admin/webhooks/{name}/test")
def test_webhook(name: str, db: Session = Depends(get_db), user: User = Depends(require_admin),
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
def deliveries(name: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """The latest messages to this webhook and how they went."""
    hook = _hook(db, name)
    return [{"id": n.id, "event": n.event, "subject": (n.payload or {}).get("subject", ""), "created_at": n.created_at,
             "sent_at": n.sent_at, "attempts": n.attempts, "error": n.error}
            for n in db.scalars(select(Notification).where(Notification.webhook_id == hook.id).order_by(Notification.id.desc()).limit(50))]

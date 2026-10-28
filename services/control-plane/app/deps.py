"""Process-wide singletons, exposed as FastAPI dependencies so tests can override them."""

from __future__ import annotations

from typing import List, Optional

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import User, get_verifier, read_token
from .config import get_settings
from .models import DataSource
from .orchestrator import Orchestrator, make_orchestrator
from .secrets import SecretStore, make_secret_store

_orchestrator: Optional[Orchestrator] = None
_secrets: Optional[SecretStore] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = make_orchestrator(get_settings())
    return _orchestrator


def get_secret_store() -> SecretStore:
    global _secrets
    if _secrets is None:
        _secrets = make_secret_store(get_settings())
    return _secrets


def request_group(user: User, group_id: Optional[str]) -> User:
    """An explicit automation primary group never changes the user's UI preference."""
    if group_id is None:
        return user
    if user.execution_context or user.username.startswith(('service:', 'sa:')):
        raise HTTPException(403, 'execution identities cannot override their primary group')
    from dataclasses import replace
    from .db import SessionLocal
    from .ownership import available_groups
    with SessionLocal() as db:
        if group_id not in {g.id for g in available_groups(db, user)}:
            raise HTTPException(403, 'requested primary group is not available to this identity')
    return replace(user, active_group_id=group_id)


def get_actor(authorization: Optional[str] = Header(default=None),
              x_fdp_active_group: Optional[str] = Header(default=None)) -> User:
    """Current admission identity. Job snapshots only apply to data access for that run."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    if token.startswith("dcs_"):
        return request_group(service_token_user(token), x_fdp_active_group)
    from .db import SessionLocal
    from .rbac import resolve_user
    from .auth import validate_execution
    try:
        p = read_token(token, "execution")
    except HTTPException:
        p = None
    with SessionLocal() as db:
        if p is not None:
            validate_execution(p)
            from .environments import execution_user
            user = resolve_user(db, execution_user(db, p))
        else:
            user = resolve_user(db, get_verifier().verify(token), refresh=True)
        db.commit()
        return request_group(user, x_fdp_active_group)


def service_token_user(token: str) -> User:
    """A service account's API token (external tools, Airflow outside Floren Data Platform, …)."""
    from .db import SessionLocal
    from .identity import user_for_token

    with SessionLocal() as db:
        user = user_for_token(db, token)
        if user is not None:
            from .rbac import resolve_user
            user = resolve_user(db, user)
            db.commit()
    if user is None:
        raise HTTPException(401, "invalid, expired or revoked service token")
    return user


def granted_sources(db: Session, actor) -> List[DataSource]:
    from .rbac import data_scope
    if not isinstance(actor, User):
        raise TypeError("granted_sources requires a current principal, not group-name claims")
    return [ds for ds in db.scalars(select(DataSource).order_by(DataSource.name)) if data_scope(db, actor, ds)]

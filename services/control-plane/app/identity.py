"""Service accounts, service tokens and secrets for programs, apps, dbt and Airflow.

Identities: a person ("ana") or a service account ("sa:finance-etl"). A service account's data
access comes from its groups; the people in its allowed_groups may run things as it.

Secrets live in OpenBao at env/<scope>/<owner>/<name>; programs reference them as
  me/<name>            the running identity's own secret (a person's, or the service account's)
  group:<g>/<name>     a group's shared secret (the identity must be in the group)
  sa:<account>/<name>  a service account's secret (the account itself, or a person allowed to use it)
"""

from __future__ import annotations

import hashlib
import re
import secrets as pysecrets
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import User
from .models import SecretEntry, ServiceAccount, ServiceToken, utcnow
from .programs import SECRET_REF
from .secrets import SecretStore

SA_PREFIX = "sa:"
TOKEN_PREFIX = "dcs_"


class SecretAccessError(Exception):
    pass


def is_sa(name: str) -> bool:
    return name.startswith(SA_PREFIX)


def get_sa(db: Session, name: str) -> Optional[ServiceAccount]:
    return db.scalar(select(ServiceAccount).where(ServiceAccount.name == name))


def may_use(sa: ServiceAccount, user: User) -> bool:
    if sa.disabled:
        return False
    if user.username == f"{SA_PREFIX}{sa.name}":
        return True
    from sqlalchemy.orm import object_session
    from .rbac import allowed, service_account_resource
    db = object_session(sa)
    return bool(db and allowed(db, user, "execute", service_account_resource(db, sa)))


def as_user(sa: ServiceAccount) -> User:
    return User(username=f"{SA_PREFIX}{sa.name}", groups=list(sa.groups))


# --- service tokens -----------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session, sa: ServiceAccount, name: str, by: str, expires_days: Optional[int]) -> Tuple[ServiceToken, str]:
    raw = TOKEN_PREFIX + pysecrets.token_urlsafe(32)
    tok = ServiceToken(service_account_id=sa.id, name=name, prefix=raw[:12], token_hash=_hash(raw), created_by=by,
                       expires_at=utcnow() + timedelta(days=expires_days) if expires_days else None)
    db.add(tok)
    return tok, raw


def user_for_token(db: Session, raw: str) -> Optional[User]:
    """The service account behind a dcs_ token, or None if unknown, revoked, expired or disabled."""
    tok = db.scalar(select(ServiceToken).where(ServiceToken.token_hash == _hash(raw)))
    if tok is None or tok.revoked:
        return None
    now = utcnow()
    if tok.expires_at and tok.expires_at.replace(tzinfo=now.tzinfo) < now:
        return None
    sa = tok.service_account
    if sa.disabled:
        return None
    if tok.last_used_at is None or now - tok.last_used_at.replace(tzinfo=now.tzinfo) > timedelta(minutes=5):
        tok.last_used_at = now
        db.commit()
    return as_user(sa)


# --- secrets ------------------------------------------------------------------------------------

def secret_path(scope: str, owner: str, name: str) -> str:
    return f"env/{scope}/{owner}/{name}"


def parse_ref(ref: str, identity: User) -> Tuple[str, str, str]:
    """(scope, owner, name) for a reference, from the point of view of the identity using it."""
    if not SECRET_REF.match(ref):
        raise SecretAccessError(f"invalid secret reference {ref!r}")
    where, name = ref.rsplit("/", 1)
    if where == "me":
        if is_sa(identity.username):
            return "sa", identity.username[len(SA_PREFIX):], name
        return "user", identity.username, name
    if where.startswith("group:"):
        return "group", where[len("group:"):], name
    return "sa", where[len(SA_PREFIX):], name


def can_access(db: Session, scope: str, owner: str, identity: User) -> bool:
    if identity.environment_id is not None and scope in ("user", "group"):
        from .layers import canonical, is_production
        # Personal/shared development secrets stay out of production layers, which use their own
        # service account secrets; a workspace cannot borrow them across layers.
        if is_production(db, canonical(db, identity.environment_id)):
            return False
    if scope == "user":
        return identity.username == owner
    if scope == "group":
        return owner in identity.groups or identity.is_admin
    sa = get_sa(db, owner)
    if sa is None or sa.disabled:
        return False
    if identity.username == f"{SA_PREFIX}{sa.name}":
        return True
    from .rbac import allowed, service_account_resource
    return allowed(db, identity, "identity", service_account_resource(db, sa))


def authorize_secrets(db: Session, store: SecretStore, identity: User, refs: Dict[str, str]) -> Dict[str, str]:
    """Environment variable -> secret path, after checking the identity may use each secret and it exists.

    Fails with a clear message (before anything starts) if a secret is missing or not allowed.
    """
    var_paths: Dict[str, str] = {}
    problems = []
    for var, ref in sorted(refs.items()):
        try:
            scope, owner, name = parse_ref(ref, identity)
        except SecretAccessError as exc:
            problems.append(f"{var}: {exc}")
            continue
        if not can_access(db, scope, owner, identity):
            problems.append(f"{var}: {identity.username} may not use secret {ref}")
            continue
        path = secret_path(scope, owner, name)
        stored = store.read(path)
        if not stored.get("certificate") and stored.get("value") is None:
            problems.append(f"{var}: secret {ref} does not exist")
            continue
        var_paths[var] = path
    if problems:
        raise SecretAccessError("; ".join(problems))
    return var_paths


def secret_env_from_paths(store: SecretStore, var_paths: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Read authorized secrets now (queued runs keep only the paths, never values)."""
    env: Dict[str, str] = {}
    for var, path in sorted(var_paths.items()):
        stored = store.read(path)
        if stored.get("certificate"):
            # VAR = certificate, VAR_KEY = private key, VAR_CA = chain (only those present).
            from .certificates import env_for
            env.update(env_for(var, stored))
        elif stored.get("value") is not None:
            env[var] = stored["value"]
        else:
            raise SecretAccessError(f"{var}: the secret was removed after the run was admitted")
    return env, sorted(set(var_paths.values()))


def resolve_secret_env(db: Session, store: SecretStore, identity: User,
                       refs: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Environment variables for a run, and the OpenBao paths used (for console masking)."""
    return secret_env_from_paths(store, authorize_secrets(db, store, identity, refs))


def put_secret(db: Session, store: SecretStore, scope: str, owner: str, name: str, value,
               by: str, description: str = "") -> SecretEntry:
    """value: a string, or a certificate bundle {certificate, private_key?, ca_chain?}."""
    certificate = isinstance(value, dict)
    store.put(secret_path(scope, owner, name), value if certificate else {"value": value})
    entry = db.scalar(select(SecretEntry).where(SecretEntry.scope == scope, SecretEntry.owner == owner,
                                                SecretEntry.name == name)) or SecretEntry(scope=scope, owner=owner, name=name)
    entry.kind = "certificate" if certificate else "string"
    if certificate:
        from .certificates import describe
        entry.meta = describe(value)
    else:
        entry.meta = None
    entry.description = description or entry.description or ""
    entry.updated_by, entry.updated_at = by, utcnow()
    db.add(entry)
    return entry


def ref_of(entry: SecretEntry) -> str:
    """How programs refer to it (owner-relative 'me/' references depend on who runs)."""
    return {"user": "me", "group": f"group:{entry.owner}", "sa": f"sa:{entry.owner}"}[entry.scope] + f"/{entry.name}"


# --- console masking ----------------------------------------------------------------------------

_mask_cache: Dict[str, List[str]] = {}


def mask(text: str, run_id: str, paths: List[str], store: SecretStore) -> str:
    """Replaces secret values given to a run with *** in its console."""
    if not paths or not text:
        return text
    values = _mask_cache.get(run_id)
    if values is None:
        values = []
        for p in paths:
            stored = store.read(p)
            for field in ("value", "certificate", "private_key", "ca_chain"):
                v = stored.get(field)
                if not v or len(v) < 4:
                    continue
                values.append(v)
                if field != "value":
                    # Printed PEM may be re-wrapped or partial: also mask each encoded line.
                    values += [line for line in v.splitlines() if len(line) >= 16 and not line.startswith("-----")]
        _mask_cache[run_id] = sorted(values, key=len, reverse=True)
        values = _mask_cache[run_id]
    for v in values:
        text = text.replace(v, "***")
    return text


SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

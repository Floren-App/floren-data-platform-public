"""Runtime versions, secrets, service accounts and their tokens."""

from __future__ import annotations

from .. import layers

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import User, require_admin
from ..config import get_settings
from ..db import get_db
from ..deps import get_actor, get_secret_store
from ..identity import (SA_PREFIX, can_access, get_sa, issue_token, may_use, put_secret, ref_of, secret_path)
from ..models import RuntimeImage, SecretEntry, ServiceAccount, ServiceToken
from ..programs import SECRET_NAME
from ..runtimes import INSTALLATION, KINDS, LABEL, offered
from ..secrets import SecretStore
from .common import audit

router = APIRouter(prefix="/v1", tags=["identity"])
NAME = r"^[a-z][a-z0-9-]{1,40}$"


# --- runtime catalog ---------------------------------------------------------------------------

class RuntimeOut(BaseModel):
    id: str
    kind: str
    version: str
    image: str
    description: str
    is_default: bool
    enabled: bool
    installation: bool = False   # shipped with the installation: image set in the Helm values


class RuntimeIn(BaseModel):
    image: str = Field(min_length=3, max_length=256)
    description: str = ""
    is_default: bool = False
    enabled: bool = True


def _rt(r: RuntimeImage) -> RuntimeOut:
    return RuntimeOut(id=r.id, kind=r.kind, version=r.version, image=r.image, description=r.description,
                      is_default=r.is_default, enabled=r.enabled, installation=r.managed_by == INSTALLATION)


@router.get("/runtimes", response_model=Dict[str, List[RuntimeOut]])
def runtimes(db: Session = Depends(get_db), _: User = Depends(get_actor)):
    """Versions you can choose in fdp.json, newest first."""
    return {k: [_rt(r) for r in offered(db, k)] for k in KINDS}


@router.get("/admin/runtimes", response_model=List[RuntimeOut], tags=["admin"])
def admin_runtimes(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return [_rt(r) for r in db.scalars(select(RuntimeImage).order_by(RuntimeImage.kind, RuntimeImage.version))]


@router.put("/admin/runtimes/{kind}/{version}", response_model=RuntimeOut, tags=["admin"])
def upsert_runtime(kind: str, version: str, body: RuntimeIn, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    from ..programs import VERSION
    import re

    if kind not in KINDS or not re.match(VERSION, version):
        raise HTTPException(422, f"kind must be one of {', '.join(KINDS)} and version like 3.12")
    from ..runtimes import require_kind
    require_kind(kind)
    row = db.scalar(select(RuntimeImage).where(RuntimeImage.kind == kind, RuntimeImage.version == version))
    if row is None:
        limit = get_settings().max_versions_per_runtime
        if len(offered(db, kind)) >= limit and body.enabled:
            raise HTTPException(409, f"at most {limit} {kind} versions can be offered; disable one first")
        row = RuntimeImage(kind=kind, version=version)
    if row.managed_by == INSTALLATION and body.image != row.image:
        raise HTTPException(409, f"{LABEL.get(kind, kind)} {version} comes with the installation: its image is set in the Helm "
                                 "values (runtime.images) and follows them on every upgrade. Add another version for a different image")
    row.image, row.description, row.enabled = body.image, body.description, body.enabled
    if body.is_default:
        for other in db.scalars(select(RuntimeImage).where(RuntimeImage.kind == kind)):
            other.is_default = False
    row.is_default = body.is_default or row.is_default
    db.add(row)
    audit(db, admin.username, "runtime.upsert", f"{kind} {version}", image=body.image)
    db.commit()
    return _rt(row)


@router.delete("/admin/runtimes/{kind}/{version}", status_code=204, tags=["admin"])
def delete_runtime(kind: str, version: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = db.scalar(select(RuntimeImage).where(RuntimeImage.kind == kind, RuntimeImage.version == version))
    if row is None:
        raise HTTPException(404, "runtime not found")
    if row.managed_by == INSTALLATION:
        raise HTTPException(409, "this version comes with the installation (Helm values); disable it instead")
    if row.is_default:
        raise HTTPException(409, "make another version the default first")
    db.delete(row)
    audit(db, admin.username, "runtime.delete", f"{kind} {version}")
    db.commit()


# --- secrets -----------------------------------------------------------------------------------

class SecretOut(BaseModel):
    scope: str
    owner: str
    name: str
    kind: str = "string"
    ref: str = Field(description="How to use it in fdp.json 'secrets'")
    description: str
    updated_by: str
    updated_at: datetime
    # Certificates only: subject, issuer, validity, fingerprint, alternative names, key/chain presence.
    certificate: Optional[Dict[str, object]] = None


class SecretIn(BaseModel):
    kind: Literal["string", "certificate"] = "string"
    value: Optional[str] = Field(default=None, min_length=1, max_length=65536)
    # Certificates: PEM certificate (optionally followed by its chain) with an optional PEM private key,
    # or a PKCS#12 (.p12/.pfx) file as base64. The password opens an encrypted key or PKCS#12 file and
    # is not stored; decrypted keys are kept in the configured secret backend.
    certificate: Optional[str] = None
    private_key: Optional[str] = None
    ca_chain: Optional[str] = None
    pkcs12_base64: Optional[str] = None
    password: Optional[str] = Field(default=None, max_length=1024)
    description: str = Field(default="", max_length=300)


def secret_out(e) -> SecretOut:
    return SecretOut(scope=e.scope, owner=e.owner, name=e.name, kind=e.kind or "string", ref=ref_of(e),
                     description=e.description, updated_by=e.updated_by, updated_at=e.updated_at,
                     certificate=e.meta if e.kind == "certificate" else None)


def _scope_owner(scope: str, owner: str, user: User) -> str:
    if scope == "user":
        if owner not in ("me", user.username):
            raise HTTPException(403, "personal secrets belong to their user")
        return user.username
    if scope not in ("group", "sa"):
        raise HTTPException(422, "scope must be user, group or sa")
    return owner


@router.get("/secrets", response_model=List[SecretOut])
def list_secrets(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Names (never values) of the secrets you can use: yours, your groups', your service accounts'."""
    out = []
    for e in db.scalars(select(SecretEntry).order_by(SecretEntry.scope, SecretEntry.owner, SecretEntry.name)):
        if can_access(db, e.scope, e.owner, user):
            out.append(secret_out(e))
    return out


@router.put("/secrets/{scope}/{owner}/{name}", response_model=SecretOut)
def set_secret(scope: str, owner: str, name: str, body: SecretIn, db: Session = Depends(get_db),
               store: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    import re

    if not re.fullmatch(SECRET_NAME, name):
        raise HTTPException(422, "secret names: letters, digits, '.', '_' and '-'")
    owner = _scope_owner(scope, owner, user)
    if not can_access(db, scope, owner, user):
        raise HTTPException(403, f"you may not manage {scope} secrets of {owner}")
    if body.kind == "certificate":
        from ..certificates import bundle
        value = bundle(body.certificate, body.private_key, body.ca_chain, body.pkcs12_base64, body.password)
    elif not body.value:
        raise HTTPException(422, {"message": "secret value is required", "errors": {"value": "required"}})
    else:
        value = body.value
    e = put_secret(db, store, scope, owner, name, value, user.username, body.description)
    audit(db, user.username, "secret.set", f"{scope}:{owner}/{name}", kind=body.kind)  # never the value
    db.commit()
    return secret_out(e)


@router.delete("/secrets/{scope}/{owner}/{name}", status_code=204)
def delete_secret(scope: str, owner: str, name: str, db: Session = Depends(get_db),
                  store: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    owner = _scope_owner(scope, owner, user)
    if not can_access(db, scope, owner, user):
        raise HTTPException(403, "not allowed")
    e = db.scalar(select(SecretEntry).where(SecretEntry.scope == scope, SecretEntry.owner == owner, SecretEntry.name == name))
    if e is None:
        raise HTTPException(404, "secret not found")
    db.delete(e)
    store.delete(secret_path(scope, owner, name))
    audit(db, user.username, "secret.delete", f"{scope}:{owner}/{name}")
    db.commit()


# --- service accounts ----------------------------------------------------------------------------

class ServiceAccountIn(BaseModel):
    layer_id: Optional[str] = None
    environment_id: Optional[str] = None
    description: str = ""
    groups: List[str] = Field(default_factory=list, description="Its data access: data sources granted to these groups")
    allowed_groups: List[str] = Field(default_factory=list, description="People who may run as it and manage its tokens")
    disabled: bool = False


class ServiceAccountOut(BaseModel):
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    environment_id: str = ""
    can_manage: bool = False
    resource_id: Optional[str] = None
    id: str
    name: str
    principal: str
    description: str
    groups: List[str]
    allowed_groups: List[str]
    disabled: bool
    created_by: str
    created_at: datetime


class TokenIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    expires_days: Optional[int] = Field(default=None, ge=1, le=3650)
    store_as_secret: Optional[str] = Field(default=None, pattern=rf"^{SECRET_NAME}$",
                                           description="Also save it as the service account's secret with this name")


class TokenOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_by: str
    created_at: datetime
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    revoked: bool
    stored_as: Optional[str]
    token: Optional[str] = Field(default=None, description="Only in the response that creates it")


def _sa_out(sa: ServiceAccount, user: User) -> ServiceAccountOut:
    from sqlalchemy.orm import object_session
    from ..rbac import allowed, service_account_resource
    db = object_session(sa)
    resource = service_account_resource(db, sa)
    return ServiceAccountOut(**layers.info(db, resource), environment_id=resource.config.get("environment_id", ""), resource_id=resource.id, can_manage=allowed(db, user, "identity", resource), id=sa.id, name=sa.name, principal=f"{SA_PREFIX}{sa.name}", description=sa.description,
                             groups=sa.groups, allowed_groups=sa.allowed_groups, disabled=sa.disabled,
                             created_by=sa.created_by, created_at=sa.created_at)


def _tok_out(t: ServiceToken, raw: Optional[str] = None) -> TokenOut:
    return TokenOut(id=t.id, name=t.name, prefix=t.prefix, created_by=t.created_by, created_at=t.created_at,
                    expires_at=t.expires_at, last_used_at=t.last_used_at, revoked=t.revoked, stored_as=t.stored_as,
                    token=raw)


def _usable_sa(db: Session, name: str, user: User) -> ServiceAccount:
    sa = get_sa(db, name)
    if sa is None:
        raise HTTPException(404, "service account not found")
    from ..rbac import require, service_account_resource
    require(db, user, "identity", service_account_resource(db, sa))
    if sa.disabled:
        raise HTTPException(404, "service account not found")
    return sa


@router.get("/service-accounts", response_model=List[ServiceAccountOut])
def list_service_accounts(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Service accounts you may run things as."""
    rows = db.scalars(select(ServiceAccount).order_by(ServiceAccount.name))
    return [_sa_out(sa, user) for sa in rows if user.is_admin or may_use(sa, user)]


@router.put("/admin/service-accounts/{name}", response_model=ServiceAccountOut, tags=["admin"])
def upsert_service_account(name: str, body: ServiceAccountIn, db: Session = Depends(get_db),
                           admin: User = Depends(require_admin)):
    import re

    if not re.match(NAME, name):
        raise HTTPException(422, "names: lowercase letters, digits and dashes")
    sa = get_sa(db, name) or ServiceAccount(name=name, created_by=admin.username)
    sa.description, sa.groups, sa.allowed_groups, sa.disabled = (
        body.description, body.groups, body.allowed_groups, body.disabled)
    db.add(sa)
    db.flush()
    from .. import rbac
    from ..models import AppGroup, IdentityBinding, RoleAssignment
    current = rbac.resolve_user(db, admin, refresh=True)
    resource = rbac.service_account_resource(db, sa)
    from ..layers import canonical, require_layer
    environment_id = require_layer(db, body.layer_id or body.environment_id or resource.config.get("layer_id") or resource.config.get("environment_id")).id
    bound = resource.config.get("layer_id") or resource.config.get("environment_id")
    if bound and canonical(db, bound) != environment_id and rbac.has_resource_assignments(db, resource.id):
        raise HTTPException(409, "a service account cannot change layers")
    resource.config = {**resource.config, "layer_id": environment_id}
    if not rbac.has_resource_assignments(db, resource.id):
        from ..ownership import require_create
        require_create(db,current)
        rbac.own(db, current, resource)
    # Legacy allowed_groups input is translated into the app-owned layer, never checked
    # directly for token/secret authority. Only the platform-admin route accepts it.
    binding_group = db.scalar(select(AppGroup).where(AppGroup.name == f"Service users {sa.id}"))
    if binding_group is None and body.allowed_groups:
        binding_group = AppGroup(name=f"Service users {sa.id}", owners=[current.principal_id])
        db.add(binding_group)
        db.flush()
        db.add(RoleAssignment(resource_id=resource.id, app_group_id=binding_group.id, role="Executor"))
    from sqlalchemy import delete
    if binding_group:
        db.execute(delete(IdentityBinding).where(IdentityBinding.app_group_id == binding_group.id))
    for name in set(body.allowed_groups):
        p = rbac.principal(db, "group", name)
        db.add(IdentityBinding(app_group_id=binding_group.id, principal_id=p.id))
    audit(db, admin.username, "service_account.upsert", name, groups=body.groups, allowed=body.allowed_groups,
          disabled=body.disabled)
    db.commit()
    return _sa_out(sa, admin)


@router.delete("/admin/service-accounts/{name}", status_code=204, tags=["admin"])
def delete_service_account(name: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from ..models import Workload

    sa = get_sa(db, name)
    if sa is None:
        raise HTTPException(404, "service account not found")
    if db.scalar(select(Workload).where(Workload.run_as == name, Workload.status != "stopped")):
        raise HTTPException(409, "workloads run as this account; change or undeploy them first")
    sa.disabled = True
    from ..rbac import service_account_resource
    service_account_resource(db, sa)  # Disable its directory identity and direct grants immediately.
    audit(db, admin.username, "service_account.delete", name)
    db.commit()


@router.get("/service-accounts/{name}/tokens", response_model=List[TokenOut])
def list_tokens(name: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    sa = _usable_sa(db, name, user)
    rows = db.scalars(select(ServiceToken).where(ServiceToken.service_account_id == sa.id)
                      .order_by(ServiceToken.created_at.desc()))
    return [_tok_out(t) for t in rows]


@router.post("/service-accounts/{name}/tokens", response_model=TokenOut, status_code=201)
def create_token(name: str, body: TokenIn, db: Session = Depends(get_db),
                 store: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    """A new API token. The response is the only time it is shown (unless saved as a secret)."""
    sa = _usable_sa(db, name, user)
    tok, raw = issue_token(db, sa, body.name, user.username, body.expires_days)
    if body.store_as_secret:
        put_secret(db, store, "sa", sa.name, body.store_as_secret, raw, user.username,
                   f"service token {body.name} of {sa.name}")
        tok.stored_as = f"sa:{sa.name}/{body.store_as_secret}"
    audit(db, user.username, "service_token.create", f"{sa.name}/{body.name}", expires_days=body.expires_days,
          stored_as=tok.stored_as)
    db.commit()
    return _tok_out(tok, raw)


@router.delete("/service-accounts/{name}/tokens/{token_id}", status_code=204)
def revoke_token(name: str, token_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    sa = _usable_sa(db, name, user)
    tok = db.get(ServiceToken, token_id)
    if tok is None or tok.service_account_id != sa.id:
        raise HTTPException(404, "token not found")
    tok.revoked = True
    audit(db, user.username, "service_token.revoke", f"{sa.name}/{tok.name}")
    db.commit()

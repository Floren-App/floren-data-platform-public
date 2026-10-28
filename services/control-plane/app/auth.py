"""Authentication for humans (OIDC access tokens) and executions (Floren Data Platform session tokens)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


@dataclass
class User:
    username: str
    roles: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    admin_role: str = "fdp-admin"

    principal_id: str = ""
    subject: str = ""
    issuer: str = ""
    # None = interactive human; empty = legacy unscoped execution. Never client supplied.
    environment_id: Optional[str] = None
    execution_context: str = ""
    active_group_id: Optional[str] = None  # Captured once per admission; switches affect later requests.
    global_admin: bool = False  # Server-resolved application group membership, never an OIDC claim.

    @property
    def is_admin(self) -> bool:
        return self.admin_role in self.roles or self.global_admin


def claim(claims: dict, path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def normalize_groups(raw: Any) -> List[str]:
    # Keycloak emits "/finance" when full group paths are enabled.
    return [g.lstrip("/") for g in (raw or []) if isinstance(g, str)]


class OIDCVerifier:
    """Validates access tokens from any OIDC provider using its JWKS."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._jwks: Optional[jwt.PyJWKClient] = None

    def _jwks_client(self) -> jwt.PyJWKClient:
        if self._jwks is None:
            url = self.settings.oidc_jwks_url
            if not url:
                discovery = f"{self.settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
                url = httpx.get(discovery, timeout=10).json()["jwks_uri"]
            self._jwks = jwt.PyJWKClient(url, cache_keys=True)
        return self._jwks

    def verify(self, token: str) -> User:
        s = self.settings
        try:
            key = self._jwks_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                audience=s.oidc_audience,
                issuer=s.oidc_issuer,
            )
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
        return User(
            username=claim(claims, s.oidc_username_claim) or claims["sub"],
            roles=list(claim(claims, s.oidc_roles_claim) or []),
            groups=normalize_groups(claim(claims, s.oidc_groups_claim)),
            admin_role=s.admin_role, subject=claims["sub"], issuer=claims["iss"],
        )


_verifier: Optional[OIDCVerifier] = None


def get_verifier() -> OIDCVerifier:
    global _verifier
    if _verifier is None:
        _verifier = OIDCVerifier(get_settings())
    return _verifier


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return authorization.split(" ", 1)[1]


def get_current_user(authorization: Optional[str] = Header(default=None)) -> User:
    from .db import SessionLocal
    from .rbac import resolve_user
    with SessionLocal() as db:
        user = resolve_user(db, get_verifier().verify(_bearer(authorization)), refresh=True)
        db.commit()
        return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


# --- Floren Data Platform-issued tokens (HS256, signed with session_signing_key) ---

def mint_token(kind: str, subject: str, ttl: int, **extra: Any) -> str:
    now = int(time.time())
    payload = {"typ": kind, "sub": subject, "iat": now, "exp": now + ttl, **extra}
    return jwt.encode(payload, get_settings().session_signing_key, algorithm="HS256")


def session_keys() -> list:
    """The signing key first, then former keys still accepted during a rotation."""
    s = get_settings()
    return [s.session_signing_key, *(k.strip() for k in s.session_signing_previous_keys.split(",") if k.strip())]


def decode_session(token: str, verify_exp: bool = True) -> dict:
    error: Optional[jwt.PyJWTError] = None
    for key in session_keys():
        try:
            return jwt.decode(token, key, algorithms=["HS256"], options={"verify_exp": verify_exp})
        except jwt.InvalidSignatureError as exc:
            error = exc  # try the next (former) key
        except jwt.PyJWTError as exc:
            raise exc
    raise error or jwt.InvalidSignatureError("Signature verification failed")


def read_token(token: str, kind: str) -> dict:
    try:
        payload = decode_session(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
    if payload.get("typ") != kind:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")
    return payload


@dataclass
class Execution:
    """Identity of a running workspace/compute container, acting on behalf of a user."""

    username: str
    groups: List[str]
    workspace_id: str
    workload_id: str = ""
    run_id: str = ""
    principal_id: str = ""
    commit_sha: str = ""


def get_execution(authorization: Optional[str] = Header(default=None)) -> Execution:
    token = _bearer(authorization)
    if token.startswith("dcs_"):
        from .deps import service_token_user

        u = service_token_user(token)
        return Execution(username=u.username, groups=[], workspace_id="", principal_id=u.principal_id)
    p = read_token(token, "execution")
    validate_execution(p)
    return Execution(username=p["sub"], groups=p.get("groups", []), workspace_id=p.get("ws", ""),
                    workload_id=p.get("pub", ""), run_id=p.get("run", ""), principal_id=p.get("pid", ""), commit_sha=p.get("sha", ""))


def validate_execution(claims):
    """A stopped/deleted execution cannot use its old token to create new work."""
    from .db import SessionLocal
    from .models import Run, Workspace, Workload
    with SessionLocal() as db:
        if claims.get("run"):
            run = db.get(Run, claims["run"])
            if not run or run.status not in ("pending", "running") or run.owner != claims["sub"]:
                raise HTTPException(401, "run is no longer active")
        elif claims.get("ws"):
            ws = db.get(Workspace, claims["ws"])
            if not ws or ws.status not in ("starting","running") or ws.owner != claims["sub"]:
                raise HTTPException(401, "workspace is no longer active")
        elif claims.get("pub"):
            pub = db.get(Workload, claims["pub"])
            if not pub or pub.status not in ("live", "deploying") or pub.identity != claims["sub"]:
                raise HTTPException(401, "workload execution is no longer authorized")
            valid_commits = {pub.deployed_sha}
            if pub.latest_sha != pub.failed_sha:
                valid_commits.add(pub.latest_sha)
            if not claims.get("sha") or claims["sha"] not in valid_commits:
                raise HTTPException(401, "workload execution commit is no longer authorized")
        else:
            raise HTTPException(401, "execution context required")

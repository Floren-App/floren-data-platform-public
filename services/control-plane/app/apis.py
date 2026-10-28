"""API management, modelled on Azure API Management: API (service) → endpoint group → operation.

internal APIs: outside callers start *deployed programs* inside Floren Data Platform (never data). Callers prove who they
    are with JWT and/or mTLS; the program runs as the API's own managed service identity, which gets its access
    the usual way (app groups with roles on workloads and data).
external APIs: definitions of outside REST/SOAP services. Programs call them through Floren Data Platform as their own
    identity; Floren Data Platform adds the configured headers, credentials and client certificate from secrets, so
    programs never hold them. Every API, endpoint group and operation is its own permission scope.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import ssl
import tempfile
import time
import os
from . import layers

from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import unquote, quote, urlsplit
from xml.sax.saxutils import escape

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rbac
from .auth import User
from .config import get_settings
from .models import ApiDefinition, ApiOperation, Resource, SecretEntry

SLUG = re.compile(r"[a-z][a-z0-9-]{1,40}")
OPERATION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
GROUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")
MAX_BODY = 10 * 1024 * 1024
# Never forwarded from a program to a backend, whatever the pass-through list says.
TRANSPORT_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization"}
HOP_BY_HOP = TRANSPORT_HEADERS | {"authorization", "cookie", "x-fdp-viewer", "x-fdp-gateway-proof", "x-fdp-gateway-config"}


# --- configuration -------------------------------------------------------------------------------------

class Value(BaseModel):
    """A header or query value: literal, or taken from a secret (group:<g>/<name> or sa:<account>/<name>)."""
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=4096)
    secret: str = Field(default="", max_length=256)


class JwtAuth(BaseModel):
    enabled: bool = False
    issuer: str = ""
    audience: str = ""
    jwks_uri: str = ""
    # claim → required value; a list or space-separated claim (scope, roles) must contain it.
    required_claims: Dict[str, str] = Field(default_factory=dict)
    algorithms: List[str] = Field(default_factory=lambda: ["RS256", "ES256", "PS256"])


class MtlsAuth(BaseModel):
    enabled: bool = False
    ca_secret: str = ""                                  # certificate secret: trusted issuing CA(s)
    allowed_subjects: List[str] = Field(default_factory=list)
    allowed_thumbprints: List[str] = Field(default_factory=list)   # SHA-256, colons optional


class InboundConfig(BaseModel):
    jwt: JwtAuth = Field(default_factory=JwtAuth)
    mtls: MtlsAuth = Field(default_factory=MtlsAuth)
    # Testing only: no caller authentication, anyone who reaches the API gateway can run the programs.
    # Never in production layers; calls are recorded as "anonymous".
    anonymous: bool = False
    soap_version: Literal["1.1", "1.2"] = "1.1"


class OutboundAuth(BaseModel):
    type: Literal["none", "api_key", "basic", "bearer", "oauth2"] = "none"
    name: str = "X-API-Key"                              # api_key: header or query parameter name
    location: Literal["header", "query"] = "header"
    username: str = ""                                   # basic
    secret: str = ""                                     # api key, password, token or OAuth2 client secret
    token_url: str = ""                                  # oauth2 client credentials
    client_id: str = ""
    scope: str = ""


class OutboundConfig(BaseModel):
    base_url: str = ""
    soap_version: Literal["1.1", "1.2"] = "1.1"
    headers: List[Value] = Field(default_factory=list)
    query: List[Value] = Field(default_factory=list)
    auth: OutboundAuth = Field(default_factory=OutboundAuth)
    client_certificate: str = ""                         # certificate secret with private key: mTLS to the backend
    server_ca: str = ""                                  # certificate secret: trust these CAs for the backend
    verify_tls: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    pass_request_headers: List[str] = Field(default_factory=list)
    pass_response_headers: List[str] = Field(default_factory=lambda: ["content-type"])


class InternalOperation(BaseModel):
    workload_id: str = ""
    mode: Literal["async", "sync"] = "async"
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    # program argument → "path.x" | "query.x" | "body.x" (dots for nesting) | "header.x" | "=literal"
    args: Dict[str, str] = Field(default_factory=dict)
    pool: str = ""


class ExternalOperation(BaseModel):
    headers: List[Value] = Field(default_factory=list)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=120)


def invalid(errors: Dict[str, str], message: str = "API configuration is incomplete"):
    raise HTTPException(422, {"message": message, "errors": errors})


def _url_ok(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not (parsed.username or parsed.password or parsed.fragment) and not re.search(r"\s", url) and (parsed.port is None or parsed.port > 0)
    except ValueError:
        return False


def check_secret(db: Session, user: User, ref: str, field: str, errors: Dict[str, str], certificate: bool = False,
                 key: bool = False) -> None:
    """Shared configuration may only use group or service-account secrets the editor can use."""
    from .identity import SECRET_REF, can_access, parse_ref
    if not ref:
        return
    if ref.startswith("me/") or not SECRET_REF.match(ref):
        errors[field] = "use a group:<group>/<name> or sa:<account>/<name> secret; personal secrets cannot be shared"
        return
    scope, owner, name = parse_ref(ref, user)
    if not can_access(db, scope, owner, user):
        errors[field] = f"you may not use secret {ref}"
        return
    entry = db.scalar(select(SecretEntry).where(SecretEntry.scope == scope, SecretEntry.owner == owner, SecretEntry.name == name))
    if entry is None:
        errors[field] = f"secret {ref} does not exist"
    elif certificate and entry.kind != "certificate":
        errors[field] = "choose a certificate secret"
    elif not certificate and entry.kind == "certificate":
        errors[field] = "choose a string secret"
    elif key and not (entry.meta or {}).get("has_private_key"):
        errors[field] = "this certificate has no private key"


def validate_inbound(db: Session, user: User, cfg: InboundConfig) -> dict:
    errors: Dict[str, str] = {}
    if cfg.anonymous and (cfg.jwt.enabled or cfg.mtls.enabled):
        errors["inbound.auth"] = "anonymous access means no JWT or mTLS; turn those off, or turn anonymous access off"
    elif not cfg.jwt.enabled and not cfg.mtls.enabled and not cfg.anonymous:
        errors["inbound.auth"] = "require JWT, mTLS or both (or allow anonymous callers for testing)"
    if cfg.jwt.enabled:
        for field in ("issuer", "audience"):
            if not getattr(cfg.jwt, field).strip():
                errors[f"inbound.jwt.{field}"] = "required"
        if not _url_ok(cfg.jwt.jwks_uri):
            errors["inbound.jwt.jwks_uri"] = "enter the issuer's JWKS URL"
        if not set(cfg.jwt.algorithms) <= {"RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"} or not cfg.jwt.algorithms:
            errors["inbound.jwt.algorithms"] = "asymmetric algorithms only (RS*, ES*, PS*)"
    if cfg.mtls.enabled:
        if not cfg.mtls.ca_secret:
            errors["inbound.mtls.ca_secret"] = "choose the certificate secret of the CA that issues client certificates"
        check_secret(db, user, cfg.mtls.ca_secret, "inbound.mtls.ca_secret", errors, certificate=True)
    if errors:
        invalid(errors)
    out = cfg.model_dump()
    out["mtls"]["allowed_thumbprints"] = [t.replace(":", "").upper() for t in cfg.mtls.allowed_thumbprints if t.strip()]
    return out


def validate_outbound(db: Session, user: User, cfg: OutboundConfig) -> dict:
    errors: Dict[str, str] = {}
    if not _url_ok(cfg.base_url) or urlsplit(cfg.base_url).query:
        errors["outbound.base_url"] = "enter an http(s) base URL without credentials, query parameters or a fragment"
    for kind, values in (("headers", cfg.headers), ("query", cfg.query)):
        for i, v in enumerate(values):
            if kind == "headers" and not HEADER_NAME.fullmatch(v.name):
                errors[f"outbound.{kind}.{i}.name"] = "invalid header name"
            if v.secret:
                check_secret(db, user, v.secret, f"outbound.{kind}.{i}.secret", errors)
    a = cfg.auth
    if a.type != "none":
        if not a.secret:
            errors["outbound.auth.secret"] = "choose the secret holding the credential"
        check_secret(db, user, a.secret, "outbound.auth.secret", errors)
    if a.type == "api_key" and not HEADER_NAME.fullmatch(a.name):
        errors["outbound.auth.name"] = "enter the header or query parameter name"
    if a.type == "basic" and not a.username.strip():
        errors["outbound.auth.username"] = "required"
    if a.type == "oauth2":
        if not _url_ok(a.token_url):
            errors["outbound.auth.token_url"] = "enter the token endpoint URL"
        if not a.client_id.strip():
            errors["outbound.auth.client_id"] = "required"
    check_secret(db, user, cfg.client_certificate, "outbound.client_certificate", errors, certificate=True, key=True)
    check_secret(db, user, cfg.server_ca, "outbound.server_ca", errors, certificate=True)
    for i, h in enumerate(cfg.pass_request_headers):
        if h.lower() in HOP_BY_HOP:
            errors[f"outbound.pass_request_headers.{i}"] = f"{h} is never passed through"
    if errors:
        invalid(errors)
    return cfg.model_dump()


def validate_path(path: str, errors: Dict[str, str]) -> None:
    if not re.fullmatch(r"/([A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(/([A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))*", path):
        errors["path"] = "a path like /orders/{id}: literal segments and {parameters}"
    parameters = re.findall(r"\{([^}]+)\}", path)
    if len(parameters) != len(set(parameters)) or any(part in (".", "..") for part in path.split("/")):
        errors["path"] = "use unique parameter names and no '.' or '..' segments"


# --- resources and identity --------------------------------------------------------------------------

def api_resource(db: Session, api: ApiDefinition) -> Resource:
    r = db.get(Resource, api.resource_id) if api.resource_id else None
    if r is None:
        r = Resource(kind="api", name=api.name, parent_id=rbac.api_root(db).id, object_id=api.id)
        db.add(r)
        db.flush()
        api.resource_id = r.id
    r.name, r.config = api.name, {"direction": api.direction, "protocol": api.protocol, "slug": api.slug, "environment_id": (api.config or {}).get("environment_id", ""), "layer_id": (api.config or {}).get("layer_id") or layers.layer_of(db, (api.config or {}).get("environment_id"))}
    return r


def find_group(db: Session, api: ApiDefinition, group: str) -> Optional[Resource]:
    """An endpoint group is identified by its API and name."""
    if not group or not api.resource_id:
        return None
    return db.scalar(select(Resource).where(Resource.kind == "api-group", Resource.parent_id == api.resource_id, Resource.name == group))


def group_resource(db: Session, api: ApiDefinition, group: str) -> Resource:
    parent = api_resource(db, api)
    r = find_group(db, api, group)
    if r is None:
        r = Resource(kind="api-group", name=group, parent_id=parent.id, object_id=api.id, config={"api_id": api.id})
        db.add(r)
        db.flush()
    return r


def operation_resource(db: Session, api: ApiDefinition, op: ApiOperation) -> Resource:
    parent = group_resource(db, api, op.group) if op.group else api_resource(db, api)
    r = db.get(Resource, op.resource_id) if op.resource_id else None
    if r is None:
        r = Resource(kind="api-operation", name=op.name, parent_id=parent.id, object_id=op.id)
        db.add(r)
        db.flush()
        op.resource_id = r.id
    # Moving an operation between groups changes what it inherits, like moving between folders.
    r.parent_id, r.name = parent.id, op.name
    r.config = {"api_id": api.id, "method": op.method, "path": op.path, "group": op.group}
    return r


def delete_resource(db: Session, resource_id: Optional[str]) -> None:
    if not resource_id:
        return
    rbac.delete_resource_assignments(db, resource_id)
    r = db.get(Resource, resource_id)
    if r is not None:
        db.delete(r)


def prune_groups(db: Session, api: ApiDefinition) -> None:
    used = {g for g in db.scalars(select(ApiOperation.group).where(ApiOperation.api_id == api.id)) if g}
    for r in db.scalars(select(Resource).where(Resource.kind == "api-group", Resource.parent_id == api.resource_id)):
        if r.name not in used:
            delete_resource(db, r.id)


def identity_principal(db: Session, api: ApiDefinition):
    """An internal API's managed identity: a service principal, bound to app groups like any service."""
    p = rbac.principal(db, "service", f"api:{api.id}", f"API: {api.name}", "local")
    p.name = f"API: {api.name}"
    api.principal_id = p.id
    return p


def identity_user(db: Session, api: ApiDefinition) -> User:
    return rbac.resolve_user(db, User(username=f"service:{api.principal_id}"))


# --- secrets ---------------------------------------------------------------------------------------------

def read_secret(store, ref: str, user: User) -> Dict[str, str]:
    from .identity import parse_ref, secret_path
    scope, owner, name = parse_ref(ref, user)
    data = store.read(secret_path(scope, owner, name))
    if not data:
        raise HTTPException(502, f"API secret {ref} is missing; ask the API's owner to fix its configuration")
    return data


def secret_value(store, ref: str, user: User) -> str:
    value = read_secret(store, ref, user).get("value")
    if value is None:
        raise HTTPException(502, f"API secret {ref} is not a string secret")
    return value


# --- inbound: caller authentication ----------------------------------------------------------------

def gateway_proof() -> str:
    """Shared only between the control plane and the gateway: client-certificate headers are trusted only
    on requests the gateway forwarded, never on direct requests from inside the network."""
    key = get_settings().session_signing_key.encode()
    return hmac.new(key, b"fdp-api-gateway-v1", hashlib.sha256).hexdigest()


_jwks: Dict[str, object] = {}


def _jwks_client(uri: str):
    import jwt
    client = _jwks.get(uri)
    if client is None:
        client = _jwks[uri] = jwt.PyJWKClient(uri, cache_keys=True, lifespan=300, timeout=10)
    return client


def verify_jwt(cfg: dict, authorization: str) -> str:
    import jwt
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a bearer token is required", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    try:
        key = _jwks_client(cfg["jwks_uri"]).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=cfg["algorithms"], audience=cfg["audience"], issuer=cfg["issuer"],
                            options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"token rejected: {exc}", headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
    for claim, required in (cfg.get("required_claims") or {}).items():
        from .auth import claim as read_claim
        actual = read_claim(claims, claim)
        values = actual if isinstance(actual, list) else str(actual or "").split()
        if required not in values and actual != required:
            raise HTTPException(403, f"token lacks the required claim {claim}={required}")
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        raise HTTPException(401, "token subject is required")
    return "jwt:" + claims["sub"]


def _client_chain(header: str) -> list:
    from cryptography import x509
    certs = []
    for part in unquote(header).split(","):
        body = re.sub(r"-----(BEGIN|END) CERTIFICATE-----|\s", "", part)
        if body:
            certs.append(x509.load_der_x509_certificate(base64.b64decode(body)))
    return certs


def verify_client_certificate(cfg: dict, header: str, trusted_pem: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from datetime import datetime, timezone
    if not header:
        raise HTTPException(401, "a client certificate is required")
    try:
        chain = _client_chain(header)
        cas = x509.load_pem_x509_certificates(trusted_pem.encode())
    except (ValueError, TypeError):
        raise HTTPException(401, "the client certificate could not be read") from None
    if not chain:
        raise HTTPException(401, "a client certificate is required")
    leaf, now = chain[0], datetime.now(timezone.utc)
    if not (leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc):
        raise HTTPException(401, "the client certificate is expired or not yet valid")
    issued = False
    for ca in cas:
        try:
            leaf.verify_directly_issued_by(ca)
            issued = ca.not_valid_before_utc <= now <= ca.not_valid_after_utc
        except Exception:  # wrong issuer, or the signature does not verify (InvalidSignature)
            continue
        if issued:
            break
    if not issued:
        raise HTTPException(401, "the client certificate is not issued by a trusted CA")
    subject = leaf.subject.rfc4514_string()
    thumbprint = leaf.fingerprint(hashes.SHA256()).hex().upper()
    if cfg.get("allowed_subjects") and subject not in cfg["allowed_subjects"]:
        raise HTTPException(403, f"client certificate {subject} is not allowed")
    if cfg.get("allowed_thumbprints") and thumbprint not in cfg["allowed_thumbprints"]:
        raise HTTPException(403, "client certificate thumbprint is not allowed")
    return f"cert:{subject}#{thumbprint[:16]}"


def authenticate(api: ApiDefinition, headers: Dict[str, str], store, via_gateway: bool) -> str:
    """Who is calling an internal API. Every enabled method must pass."""
    cfg = api.config.get("inbound") or {}
    callers = []
    if (cfg.get("mtls") or {}).get("enabled"):
        bundle = read_secret(store, cfg["mtls"]["ca_secret"], User(username="api"))
        trusted = bundle.get("certificate", "") + bundle.get("ca_chain", "")
        callers.append(verify_client_certificate(cfg["mtls"], headers.get("x-forwarded-tls-client-cert", "") if via_gateway else "", trusted))
    if (cfg.get("jwt") or {}).get("enabled"):
        callers.append(verify_jwt(cfg["jwt"], headers.get("authorization", "")))
    if not callers and cfg.get("anonymous"):
        return "anonymous"  # testing only: no caller authentication
    if not callers:
        raise HTTPException(401, "this API has no caller authentication configured")
    caller = " + ".join(callers)
    if len(caller) > 512:
        raise HTTPException(401, "the caller identity exceeds 512 characters")
    return caller


# --- inbound: matching and arguments ----------------------------------------------------------------

def _template(path: str) -> re.Pattern:
    parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\})", path.rstrip("/") or "/")
    return re.compile("".join(f"(?P<{p[1:-1]}>[^/]+)" if p.startswith("{") else re.escape(p) for p in parts) + "/?")


def match_rest(ops: List[ApiOperation], method: str, path: str) -> Tuple[Optional[ApiOperation], Dict[str, str]]:
    path = "/" + path.lstrip("/")
    for op in sorted(ops, key=lambda o: (o.path.count("{"), -len(o.path))):   # literal paths win
        m = _template(op.path).fullmatch(path)
        if m and op.method == method:
            return op, {k: unquote(v) for k, v in m.groupdict().items()}
    return None, {}


def soap_request(body: bytes) -> Tuple[str, Dict[str, object]]:
    """(operation element name, its child elements) from a SOAP 1.1/1.2 envelope. DTDs are refused."""
    import xml.etree.ElementTree as ET
    if len(body) > 1024 * 1024:
        raise HTTPException(413, "SOAP requests are limited to 1 MB")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise HTTPException(400, "DTDs and entities are not accepted")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise HTTPException(400, "the request is not well-formed XML") from None
    local = lambda tag: tag.rsplit("}", 1)[-1]
    envelope_body = next((c for c in root if local(c.tag) == "Body"), None) if local(root.tag) == "Envelope" else None
    if envelope_body is None or not len(envelope_body):
        raise HTTPException(400, "expected a SOAP Envelope with a Body element")
    operation = envelope_body[0]

    def value(el):
        return {local(c.tag): value(c) for c in el} if len(el) else (el.text or "").strip()
    return local(operation.tag), value(operation) if len(operation) else {}


def match_soap(ops: List[ApiOperation], action: str, element: str) -> Optional[ApiOperation]:
    action = action.strip().strip('"')
    return next((o for o in ops if action and o.soap_action == action), None) or next((o for o in ops if o.name == element), None)


def _lookup(source, dotted: str):
    for key in dotted.split("."):
        if not isinstance(source, dict) or key not in source:
            return None
        source = source[key]
    return source


def collect_args(mapping: Dict[str, str], arg_names: List[str], sources: Dict[str, dict]) -> Dict[str, object]:
    """Program arguments from the request. Mapped explicitly, else by the same name in path, query, body."""
    args: Dict[str, object] = {}
    for name in arg_names:
        rule = (mapping or {}).get(name, "")
        if rule.startswith("="):
            value = rule[1:]
        elif rule:
            where, _, key = rule.partition(".")
            value = _lookup(sources.get(where) or {}, key.lower() if where == "header" else key)
        else:
            value = next((v for v in (sources["path"].get(name), sources["query"].get(name), _lookup(sources["body"], name))
                          if v is not None), None)
        if value is not None and value != "":
            args[name] = value
    return args


def to_xml(value, tag: str) -> str:
    if isinstance(value, dict):
        inner = "".join(to_xml(v, re.sub(r"[^A-Za-z0-9_.-]", "_", str(k)) or "item") for k, v in value.items())
    elif isinstance(value, list):
        inner = "".join(to_xml(v, "item") for v in value)
    else:
        inner = escape("" if value is None else str(value).lower() if isinstance(value, bool) else str(value))
    return f"<{tag}>{inner}</{tag}>"


SOAP_NS = {"1.1": "http://schemas.xmlsoap.org/soap/envelope/", "1.2": "http://www.w3.org/2003/05/soap-envelope"}


def soap_envelope(version: str, payload: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{SOAP_NS[version]}"><soap:Body>{payload}</soap:Body></soap:Envelope>'


def soap_fault(version: str, client_error: bool, message: str) -> str:
    if version == "1.2":
        code = "soap:Sender" if client_error else "soap:Receiver"
        fault = f"<soap:Fault><soap:Code><soap:Value>{code}</soap:Value></soap:Code><soap:Reason><soap:Text xml:lang=\"en\">{escape(message)}</soap:Text></soap:Reason></soap:Fault>"
    else:
        code = "soap:Client" if client_error else "soap:Server"
        fault = f"<soap:Fault><faultcode>{code}</faultcode><faultstring>{escape(message)}</faultstring></soap:Fault>"
    return soap_envelope(version, fault)


def soap_media_type(version: str) -> str:
    return "application/soap+xml; charset=utf-8" if version == "1.2" else "text/xml; charset=utf-8"


# --- outbound: calling an external API ----------------------------------------------------------

_oauth_tokens: Dict[str, Tuple[str, float]] = {}


def _oauth_token(api: ApiDefinition, auth: dict, store, user: User, verify) -> str:
    import httpx
    key = f"{api.id}:{auth['client_id']}:{auth['token_url']}:{auth.get('scope', '')}"
    cached = _oauth_tokens.get(key)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    data = {"grant_type": "client_credentials", "client_id": auth["client_id"],
            "client_secret": secret_value(store, auth["secret"], user)}
    if auth.get("scope"):
        data["scope"] = auth["scope"]
    try:
        r = httpx.post(auth["token_url"], data=data, timeout=15, verify=verify)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"could not obtain a token for {api.name}: {exc}") from None
    _oauth_tokens[key] = (body["access_token"], time.time() + float(body.get("expires_in", 300)))
    return body["access_token"]


def _expand(values: List[dict], store, user: User) -> List[Tuple[str, str]]:
    return [(v["name"], secret_value(store, v["secret"], user) if v.get("secret") else v.get("value", "")) for v in values]


def call_external(api: ApiDefinition, op: ApiOperation, path: str, query: List[Tuple[str, str]],
                  request_headers: Dict[str, str], body: bytes, store) -> Tuple[int, Dict[str, str], bytes]:
    """Calls the backend with the API's configuration. Credentials come from secrets and never reach the caller."""
    import httpx
    cfg, op_cfg = api.config.get("outbound") or {}, op.config or {}
    owner = User(username="api")   # group:/sa: references are absolute; validated when configured
    parts = [unquote(part) for part in path.lstrip("/").split("/")]
    if any(part in (".", "..") or any(c in part for c in "/\\?#") for part in parts):
        raise HTTPException(400, "backend path parameters cannot contain traversal or path separators")
    url = cfg["base_url"].rstrip("/") + "/" + "/".join(quote(part, safe="-._~") for part in parts)
    allowed = {h.lower() for h in cfg.get("pass_request_headers", [])} - HOP_BY_HOP
    headers = {k.lower(): v for k, v in request_headers.items() if k.lower() in allowed or k.lower() in ("content-type", "accept")}
    headers.update({k.lower(): v for k, v in _expand(cfg.get("headers", []), store, owner)})
    headers.update({k.lower(): v for k, v in _expand(op_cfg.get("headers", []), store, owner)})
    configured_query = _expand(cfg.get("query", []), store, owner)
    names = {k for k, _ in configured_query}
    params = [(k, v) for k, v in query if k not in names] + configured_query

    verify: object = True
    if cfg.get("server_ca"):
        ca = read_secret(store, cfg["server_ca"], owner)
        verify = ssl.create_default_context(cadata=ca.get("certificate", "") + ca.get("ca_chain", ""))
    elif not cfg.get("verify_tls", True):
        verify = False
    auth = cfg.get("auth") or {}
    kind = auth.get("type", "none")
    if kind == "api_key":
        value = secret_value(store, auth["secret"], owner)
        if auth.get("location") == "query":
            params = [(k, v) for k, v in params if k != auth["name"]] + [(auth["name"], value)]
        else:
            headers[auth["name"].lower()] = value
    elif kind == "basic":
        token = base64.b64encode(f"{auth['username']}:{secret_value(store, auth['secret'], owner)}".encode()).decode()
        headers["authorization"] = "Basic " + token
    elif kind == "bearer":
        headers["authorization"] = "Bearer " + secret_value(store, auth["secret"], owner)
    elif kind == "oauth2":
        headers["authorization"] = "Bearer " + _oauth_token(api, auth, store, owner, verify)

    if api.protocol == "soap":
        version = cfg.get("soap_version", "1.1")
        if b"Envelope" not in body[:2048]:
            body = soap_envelope(version, body.decode("utf-8")).encode()
        headers["content-type"] = soap_media_type(version) + (f'; action="{op.soap_action}"' if version == "1.2" and op.soap_action else "")
        if version == "1.1":
            headers["soapaction"] = f'"{op.soap_action}"'

    with tempfile.TemporaryDirectory(prefix="fdp-mtls-") as folder:
        cert = None
        if cfg.get("client_certificate"):
            bundle = read_secret(store, cfg["client_certificate"], owner)
            cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
            for path_, content in ((cert_path, bundle["certificate"] + bundle.get("ca_chain", "")), (key_path, bundle.get("private_key", ""))):
                fd = os.open(path_, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(content)
            cert = (cert_path, key_path)
            if verify is True:
                verify = ssl.create_default_context()
            if isinstance(verify, ssl.SSLContext):
                verify.load_cert_chain(cert_path, key_path)
                cert = None
        timeout = op_cfg.get("timeout_seconds") or cfg.get("timeout_seconds", 30)
        try:
            with httpx.Client(verify=verify, cert=cert, timeout=timeout, follow_redirects=False) as client:
                with client.stream(op.method, url, params=params, headers=headers, content=body or None) as r:
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BODY:
                            raise HTTPException(502, "the backend response exceeds 10 MB")
                        chunks.append(chunk)
                    status, reply = r.status_code, r.headers
        except httpx.TimeoutException:
            raise HTTPException(504, f"{api.name} did not answer within {timeout} s") from None
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"could not reach {api.name}: {exc}") from None
    # iter_bytes() decodes compression, so transport lengths/encodings no longer describe this body.
    passed = ({h.lower() for h in cfg.get("pass_response_headers", ["content-type"])} | {"content-type"}) - TRANSPORT_HEADERS - {"content-encoding"}
    return status, {k: v for k, v in reply.items() if k.lower() in passed}, b"".join(chunks)

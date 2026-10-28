"""Endpoints Traefik calls directly. Not routed through the public gateway.

- /internal/traefik/config: dynamic routing (Traefik HTTP provider) for running editors and apps.
- /internal/auth/forward:   forwardAuth check for every request to /ws/* and /apps/*.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import os
import time
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..auth import mint_token, read_token
from ..config import get_settings
from ..db import get_db
from ..models import Workload, Run, Workspace, utcnow
from ..deploying import docs_path
from .deploy import can_view

router = APIRouter(prefix="/internal", include_in_schema=False)

ACTIVITY_THROTTLE = timedelta(seconds=60)


def _target(path: str) -> Optional[Tuple[str, str]]:
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] in ("ws", "apps", "p") and parts[2]:
        return parts[1], parts[2]
    return None


# CloudBeaver endpoints that move files between the browser and the server.
SQL_WORKSPACE_TRANSFER = ("data/", "resultset/blob/", "fs-data/")


from ..apis import gateway_proof  # noqa: E402


@router.get("/metrics")
def platform_metrics(request: Request, db: Session = Depends(get_db), authorization: str = Header(default="")):
    """Prometheus metrics of this replica, plus the platform's state when it leads (see app/metrics.py)."""
    from fastapi.responses import PlainTextResponse
    from .. import metrics
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(404, "metrics are disabled; set FDP_METRICS_TOKEN")
    if not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "metrics authentication required")
    leadership = getattr(request.app.state, "leadership", None)
    leader = leadership.leader if leadership is not None else True
    return PlainTextResponse(metrics.render(db, leader), media_type="text/plain; version=0.0.4")


@router.get("/metrics/certificates")
def certificate_metrics(db: Session = Depends(get_db), authorization: str = Header(default="")):
    """Certificate expiry and problems for Prometheus alerting; hostnames and dates only."""
    from fastapi.responses import PlainTextResponse
    from .. import cert_monitor
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(404, "certificate metrics are disabled; set FDP_METRICS_TOKEN")
    if not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "metrics authentication required")
    return PlainTextResponse(cert_monitor.metrics(cert_monitor.rows(db)), media_type="text/plain; version=0.0.4")


@router.get("/egress/allowlist")
def egress_allowlist(db: Session = Depends(get_db)):
    """For the egress proxy. Host names only: workspaces may read it too, they are told what they may reach."""
    from .. import egress
    return {"hosts": [{"host": h["host"]} for h in egress.hosts(db)]}


@router.get("/traefik/config")
def traefik_config(db: Session = Depends(get_db), x_fdp_gateway_config: str = Header(default="")):
    s = get_settings()
    if not s.gateway_config_token:
        raise HTTPException(503, "gateway configuration authentication is not configured")
    if not hmac.compare_digest(x_fdp_gateway_config, s.gateway_config_token):
        raise HTTPException(401, "gateway configuration authentication required")
    routers = {
        # SCIM has its own provisioning bearer; /internal never has a public route.
        "api": {"rule": "PathPrefix(`/api/v1`) || PathPrefix(`/api/scim/v2`)", "service": "api", "middlewares": ["api-strip"]},
        "sql": {"rule": "PathPrefix(`/v1/statement`) || PathPrefix(`/v1/sql/`) || Path(`/v1/info`)", "service": "api"},
        # Internal APIs: callers authenticate per API (JWT and/or mTLS). The gateway asks for a client
        # certificate without requiring one, forwards it, and proves to the control plane that it did.
        # TLS options are chosen by host name (SNI) during the handshake, so the router names its host.
        "gw": {"rule": f"Host(`{urlsplit(s.api_gateway_url).hostname}`) && PathPrefix(`/gw/`)", "service": "api",
               "middlewares": ["gw-client-cert", "gw-proof"], "entryPoints": [s.api_gateway_entrypoint],
               "tls": {"options": "api-gateway@file"}},
    }
    identity_service = None
    if s.identity_proxy_url:
        routers['identity'] = {'rule':f"Host(`{urlsplit(s.oidc_issuer).hostname}`)",
                               'service':'identity', 'priority':10000}
        identity_service = {'loadBalancer':{'servers':[{'url':s.identity_proxy_url}]}}
    services = {"api": {"loadBalancer": {"servers": [{"url": s.internal_url}]}}}
    if s.web_url:
        # The app's pages: lowest priority, so the API, editors, apps and identity routes always win.
        routers["web"] = {"rule": f"Host(`{urlsplit(s.public_url).hostname}`)", "service": "web", "priority": 1,
                          "entryPoints": ["websecure"]}
        services["web"] = {"loadBalancer": {"servers": [{"url": s.web_url}]}}
    middlewares = {
        "api-strip": {"stripPrefix": {"prefixes": ["/api"]}},
        # X-FDP-User tells apps who is viewing (Streamlit: st.context.headers).
        "gw-client-cert": {"passTLSClientCert": {"pem": True}},
        "gw-proof": {"headers": {"customRequestHeaders": {"X-FDP-Gateway-Proof": gateway_proof()}}},
        "fdp-auth": {"forwardAuth": {"address": f"{s.internal_url}/internal/auth/forward",
                                    "authResponseHeaders": ["X-FDP-User", "X-FDP-Viewer", "X-FDP-Team", "X-FDP-Display-Name"]}},
        "sql-sanitize": {"headers":{"customRequestHeaders":{name:"" for name in (
            "X-User","X-Team","X-Role","X-Role-TE","X-First-name","X-Last-name","X-Full-name",
            "X-FDP-User","X-FDP-Team","X-FDP-Display-Name")}}},
    }

    def add(kind: str, item_id: str, upstream: str, strip: bool, rstudio: bool = False) -> None:
        key = f"{kind}-{item_id}"
        mws = ["fdp-auth"]
        if strip:
            middlewares[f"{key}-strip"] = {"stripPrefix": {"prefixes": [f"/{kind}/{item_id}"]}}
            mws.append(f"{key}-strip")
        if rstudio:
            # Lets RStudio Server build correct URLs and redirects behind a sub-path.
            middlewares[f"{key}-root"] = {"headers": {"customRequestHeaders": {"X-RStudio-Root-Path": f"/{kind}/{item_id}"}}}
            mws.append(f"{key}-root")
        routers[key] = {"rule": f"PathPrefix(`/{kind}/{item_id}/`)", "service": key, "middlewares": mws}
        services[key] = {"loadBalancer": {"servers": [{"url": upstream}]}}

    for ws in db.scalars(select(Workspace)):
        if ws.status != "running" or not ws.upstream_url:
            add("ws",ws.id,s.internal_url,strip=False)
            continue
        # CloudBeaver serves under its /ws/<id>/ root itself; the others expect the prefix stripped.
        add("ws", ws.id, ws.upstream_url, strip=ws.editor != "sql", rstudio=ws.editor == "rstudio")
        if ws.editor == "sql":
            routers[f"ws-{ws.id}"]["middlewares"].insert(0,"sql-sanitize")
            # Data stays on the server: refuse CloudBeaver's export downloads, imports and file transfer,
            # like downloads/uploads are disabled in code-server. The editor hides these features too.
            blocked = " || ".join(f"PathPrefix(`/ws/{ws.id}/api/{path}`)" for path in SQL_WORKSPACE_TRANSFER)
            routers[f"ws-{ws.id}-transfer"] = {"rule": blocked, "service": f"ws-{ws.id}", "priority": 10000,
                                               "middlewares": ["fdp-auth", "deny-all"]}
            middlewares["deny-all"] = {"ipAllowList": {"sourceRange": ["127.0.0.2/32"]}}
    # Streamlit and Airflow serve under their address prefix (base URL); only Shiny needs it stripped.
    for run in db.scalars(select(Run).where(Run.kind.in_(("app", "airflow")), Run.status == "running",
                                            Run.upstream_url.is_not(None))):
        add("apps", run.id, run.upstream_url, strip=run.kind == "app" and run.runtime == "r")
    for pub in db.scalars(select(Workload).where(Workload.status == "live")):
        if pub.kind in ("app", "airflow") and pub.upstream_url:
            add("p", pub.slug, pub.upstream_url, strip=pub.kind == "app" and pub.runtime == "r")
        elif pub.kind == "dbt":
            add("p", pub.slug, s.internal_url, strip=False)   # docs are served by the control plane

    if identity_service:
        services['identity'] = identity_service
    return {"http": {"routers": routers, "services": services, "middlewares": middlewares}}


def _touch(db: Session, kind: str, item_id: str) -> None:
    if kind == "p":
        return  # deployed apps don't idle out
    item = db.get(Workspace if kind == "ws" else Run, item_id)
    now = utcnow()
    last = item.last_activity_at if item else None
    if item and (last is None or now - last.replace(tzinfo=now.tzinfo) > ACTIVITY_THROTTLE):
        item.last_activity_at = now
        db.commit()


def _allowed(db: Session, kind: str, item_id: str, username: str, groups: list, principal_id: str = "") -> bool:
    """Workspaces and workspace apps: owner only. Deployed apps: anyone their visibility allows."""
    from ..rbac import resolve_user, allowed, workload_resource
    from ..auth import User
    try:
        user = resolve_user(db, User(username, principal_id=principal_id))
    except HTTPException:
        return False
    if kind == "p":
        pub = db.scalar(select(Workload).where(Workload.slug == item_id, Workload.status == "live"))
        return pub is not None and allowed(db, user, "execute", workload_resource(db, pub))
    item = db.get(Workspace if kind == "ws" else Run, item_id)
    from ..rbac import same_identity
    return item is not None and (same_identity(item, user) or user.is_admin)


@router.get("/auth/forward")
def forward_auth(
    request: Request,
    db: Session = Depends(get_db),
    x_forwarded_uri: str = Header(default="/"),
    x_forwarded_proto: Optional[str] = Header(default=None),
    x_forwarded_host: Optional[str] = Header(default=None),
):
    # Asset bursts can fill the sync worker pool while dependencies still hold
    # connections pending teardown. Release here, in the same worker that did
    # the authorization, before another request waits for a connection.
    for attempt in range(3):
        try:
            return _forward_auth(request, db, x_forwarded_uri, x_forwarded_proto, x_forwarded_host)
        except OperationalError as error:
            # A failed connection/DNS lookup must not permanently break a lazy
            # browser import. Retry a fresh authorization transaction, never a
            # cached allow decision. SQL errors with other states still surface.
            sqlstate = getattr(error.orig, "sqlstate", None)
            if attempt == 2 or (sqlstate and not sqlstate.startswith("08")):
                raise
        finally:
            db.close()
        time.sleep(0.1 * (attempt + 1))


def _forward_auth(request, db, x_forwarded_uri, x_forwarded_proto, x_forwarded_host):
    parts = urlsplit(x_forwarded_uri)
    target = _target(parts.path)
    if not target:
        raise HTTPException(403)
    kind, item_id = target
    cookie_name = f"dc_{kind}_{item_id}"

    query = dict(parse_qsl(parts.query))
    ticket = query.pop("dc_ticket", None)
    if ticket:
        p = read_token(ticket, "ticket")
        groups = p.get("groups", [])
        if p.get("target") != f"{kind}:{item_id}" or not _allowed(db, kind, item_id, p["sub"], groups, p.get("pid", "")):
            raise HTTPException(403)
        # Swap the one-time ticket for a cookie scoped to this workspace/app only.
        # Absolute URL: Traefik resolves a relative Location against the auth server's address.
        if x_forwarded_host:
            scheme, host = x_forwarded_proto or "https", x_forwarded_host
        else:
            public = urlsplit(get_settings().public_url)
            scheme, host = public.scheme, public.netloc
        clean = urlunsplit((scheme, host, parts.path, urlencode(query), ""))
        resp = RedirectResponse(clean, status_code=302)
        cookie = mint_token("gateway", p["sub"], get_settings().session_token_ttl_seconds,
                            target=f"{kind}:{item_id}", groups=[], pid=p.get("pid", ""))
        resp.set_cookie(cookie_name, cookie, path=f"/{kind}/{item_id}/", httponly=True, samesite="lax",
                        secure=get_settings().public_url.startswith("https"))
        return resp

    cookie = request.cookies.get(cookie_name)
    if not cookie:
        raise HTTPException(401)
    p = read_token(cookie, "gateway")
    # Re-checked on every request: undeploying or narrowing visibility takes effect immediately.
    if p.get("target") != f"{kind}:{item_id}" or not _allowed(db, kind, item_id, p["sub"], p.get("groups", []), p.get("pid", "")):
        raise HTTPException(403)
    if kind == "ws":
        ws = db.get(Workspace,item_id)
        if ws and ws.status != "running":
            # Also intercept stale gateway routes during its configuration polling window.
            relative = parts.path.split("/",3)[3] if len(parts.path.split("/",3)) > 3 else ""
            return workspace_waiting(item_id,relative,request,db)
    _touch(db, kind, item_id)
    headers = {"X-FDP-User": p["sub"]}
    if kind == "ws":
        ws = db.get(Workspace,item_id)
        if ws and ws.editor == "sql":
            # CloudBeaver team IDs include 'admin' and 'user'; avoid collisions with human names.
            headers.update({"X-FDP-User":"fdp-"+(ws.principal_id or ws.id),
                            "X-FDP-Team":"user","X-FDP-Display-Name":ws.owner})
    if kind == "p":
        from ..rbac import resolve_user
        from ..auth import User
        user = resolve_user(db, User(p["sub"], principal_id=p.get("pid", "")))
        pub = db.scalar(select(Workload).where(Workload.slug == item_id))
        if pub and pub.execution_mode == "caller":
            headers["X-FDP-Viewer"] = mint_token("viewer", user.username, max(1, int(p["exp"] - time.time())), pid=user.principal_id, pub=pub.id)
    return Response(status_code=200, headers=headers)


# --- dbt docs ------------------------------------------------------------------------------------
# Reached through the gateway route /p/<slug>/ (after forwardAuth). Executions can reach the control
# plane directly, so the viewer's cookie is checked here again rather than trusting the proxy.
docs_router = APIRouter(include_in_schema=False)


@docs_router.get("/ws/{ws_id}/{path:path}")
def workspace_waiting(ws_id: str, path: str, request: Request, db: Session = Depends(get_db)):
    """Authenticated fallback during startup, failure, suspension or the routing refresh window."""
    from html import escape
    cookie = request.cookies.get(f"dc_ws_{ws_id}")
    if not cookie:
        raise HTTPException(401,"Sign in to Floren Data Platform and open this workspace again.")
    claims = read_token(cookie,"gateway")
    if claims.get("target") != f"ws:{ws_id}" or not _allowed(db,"ws",ws_id,claims["sub"],[],claims.get("pid","")):
        raise HTTPException(403)
    ws = db.get(Workspace,ws_id)
    if not ws:
        raise HTTPException(404,"workspace not found")
    loading = ws.status in ("starting","running")
    title = "Starting your workspace" if ws.status == "starting" else "Connecting to your editor" if loading else "Workspace could not start" if ws.status == "failed" else "Workspace is stopped"
    message = "The editor is preparing. This page will open it automatically when it is ready." if loading else ws.startup_error or "Return to Floren Data Platform and start this workspace. Your files are kept."
    headers = {"Cache-Control":"no-store","Retry-After":"3"}
    if path and not path.endswith((".html","/")):
        return Response(status_code=503,content="Workspace editor is not ready.",headers=headers)
    refresh = '<meta http-equiv="refresh" content="3">' if loading else ""
    body = f'''<!doctype html><html><head><meta charset="utf-8">{refresh}<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} · Floren Data Platform</title>
<style>body{{font:16px system-ui;margin:0;background:#f7f8fa;color:#20242a;display:grid;min-height:100vh;place-items:center}}main{{max-width:560px;padding:32px}}small{{color:#657080}}h1{{font-size:24px}}p{{line-height:1.6}}</style></head>
<body><main><small>FLOREN DATA PLATFORM · {escape(ws.name)}</small><h1>{escape(title)}</h1><p>{escape(message)}</p></main></body></html>'''
    return HTMLResponse(body,status_code=503,headers=headers)


@docs_router.get("/p/{slug}/")
@docs_router.get("/p/{slug}/index.html")
def dbt_docs(slug: str, request: Request, db: Session = Depends(get_db)):
    cookie = request.cookies.get(f"dc_p_{slug}")
    if not cookie:
        raise HTTPException(401)
    p = read_token(cookie, "gateway")
    if p.get("target") != f"p:{slug}" or not _allowed(db, "p", slug, p["sub"], p.get("groups", []), p.get("pid", "")):
        raise HTTPException(403)
    pub = db.scalar(select(Workload).where(Workload.slug == slug, Workload.kind == "dbt"))
    if not pub:
        raise HTTPException(404, "no docs generated yet")
    from ..artifacts import SharedArtifacts, for_workload
    from ..deploying import docs_artifact
    store = for_workload(db, pub)
    if isinstance(store, SharedArtifacts):
        path = docs_path(pub.id)
        if not os.path.exists(path):
            raise HTTPException(404, "no docs generated yet")
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})
    body = store.read(docs_artifact(pub.id), 64 * 1024 * 1024)
    if body is None:
        raise HTTPException(404, "no docs generated yet")
    return Response(body, media_type="text/html", headers={"Cache-Control": "no-cache"})

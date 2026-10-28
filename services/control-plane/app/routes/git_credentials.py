"""Your own Git credentials (Account → Git credentials), each workspace's choice of one, and the credential
helper's endpoint in your workspaces."""
from __future__ import annotations

import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import git_credentials as gc
from ..auth import Execution, User, get_current_user, get_execution
from ..db import get_db
from ..deps import get_secret_store
from ..models import GitCredential, Workspace, utcnow
from ..schemas import WorkspaceOut
from ..secrets import SecretStore
from .common import audit

router = APIRouter(prefix="/v1", tags=["workspaces"])
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GIT_EDITORS = ("vscode", "rstudio")


class GitCredentialIn(BaseModel):
    """People only (signed in): service accounts never get Git tokens, the platform only reads from Git."""
    label: str = Field(default="", max_length=64, description='How you recognize it, e.g. "GitHub work"; default: the host')
    host: str = Field(min_length=1, max_length=255, description="github.com, or git.example.com:8443")
    username: str = Field(min_length=1, max_length=128, description="Your username on the Git host")
    token: Optional[str] = Field(default=None, max_length=4096,
                                 description="A personal access token; stored in the secret store, never shown again. "
                                             "Required for a new credential; empty keeps the saved one")
    commit_name: str = Field(default="", max_length=128, description="Name on your commits; default: your platform name")
    commit_email: str = Field(default="", max_length=254, description="Email on your commits, e.g. your GitHub noreply address")


def _host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if not gc.valid_host(host):
        raise HTTPException(422, f"{host!r} is not a Git host name; use github.com or git.example.com:8443")
    return host


def _out(row: GitCredential) -> dict:
    return {"id": row.id, "label": row.label, "host": row.host, "username": row.username,
            "commit_name": row.commit_name, "commit_email": row.commit_email, "updated_at": row.updated_at}


def _mine(db: Session, user: User, credential_id: str) -> GitCredential:
    row = db.get(GitCredential, credential_id)
    if row is None or row.owner != user.username:
        raise HTTPException(404, "Git credential not found")
    return row


def _apply(db: Session, row: GitCredential, body: GitCredentialIn, store: SecretStore) -> None:
    host = _host(body.host)
    label = body.label.strip() or host
    email = body.commit_email.strip()
    if email and not EMAIL.fullmatch(email):
        raise HTTPException(422, "the commit email is not an email address")
    if any(c.id != row.id and c.label.lower() == label.lower() for c in gc.owned(db, row.owner)):
        raise HTTPException(409, f"you already have a Git credential called {label}")
    row.label, row.host, row.username = label, host, body.username.strip()
    row.commit_name, row.commit_email, row.updated_at = body.commit_name.strip(), email, utcnow()
    db.add(row)
    db.flush()
    if body.token and body.token.strip():
        store.put(gc.path(row.owner, row.id), {"token": body.token.strip()})


@router.get("/me/git-credentials")
def list_git_credentials(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> List[dict]:
    """Your credentials; tokens are never returned."""
    return [_out(r) for r in gc.owned(db, user.username)]


@router.post("/me/git-credentials", status_code=201)
def create_git_credential(body: GitCredentialIn, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                          store: SecretStore = Depends(get_secret_store)):
    if not (body.token or "").strip():
        raise HTTPException(422, "a token is required")
    row = GitCredential(owner=user.username)
    _apply(db, row, body, store)
    audit(db, user.username, "git.credential.create", row.host, label=row.label)   # never the token
    db.commit()
    return _out(row)


@router.put("/me/git-credentials/{credential_id}")
def update_git_credential(credential_id: str, body: GitCredentialIn, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user), store: SecretStore = Depends(get_secret_store)):
    row = _mine(db, user, credential_id)
    _apply(db, row, body, store)
    audit(db, user.username, "git.credential.update", row.host, label=row.label, token_changed=bool((body.token or "").strip()))
    db.commit()
    return _out(row)


@router.delete("/me/git-credentials/{credential_id}", status_code=204)
def delete_git_credential(credential_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                          store: SecretStore = Depends(get_secret_store)):
    row = _mine(db, user, credential_id)
    store.delete(gc.path(row.owner, row.id))
    store.delete(gc.legacy_path(row.owner, row.host))
    for ws in db.scalars(select(Workspace).where(Workspace.git_credential_id == row.id)):
        ws.git_credential_id, ws.git_asked = None, False   # asked again at its next start
    db.delete(row)
    audit(db, user.username, "git.credential.delete", row.host, label=row.label)
    db.commit()


class WorkspaceGitIn(BaseModel):
    credential_id: Optional[str] = Field(default=None, description="One of your Git credentials; empty: no Git in this workspace")


@router.put("/workspaces/{ws_id}/git", response_model=WorkspaceOut)
def choose_workspace_git(ws_id: str, body: WorkspaceGitIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The token applies right away; the commit name and email at the workspace's next start."""
    ws = db.get(Workspace, ws_id)
    if ws is None or ws.owner != user.username:
        raise HTTPException(404, "workspace not found")
    if ws.editor not in GIT_EDITORS:
        raise HTTPException(422, "only VS Code and RStudio workspaces use Git")
    if body.credential_id:
        _mine(db, user, body.credential_id)
    ws.git_credential_id, ws.git_asked = body.credential_id or None, True
    audit(db, user.username, "workspace.git", ws.id, credential=body.credential_id or "none")
    db.commit()
    return ws


@router.get("/workspaces/{ws_id}/git-credential", include_in_schema=False)
def workspace_git_credential(ws_id: str, host: str, protocol: str = "https", db: Session = Depends(get_db),
                             execution: Execution = Depends(get_execution), store: SecretStore = Depends(get_secret_store)):
    """For git's credential helper in the person's own running editor; runs and deployed programs get nothing."""
    ws = db.get(Workspace, ws_id)
    if execution.workspace_id != ws_id or execution.run_id or execution.workload_id or not ws or ws.owner != execution.username:
        raise HTTPException(403, "only your own running workspace may use your Git tokens")
    if ws.editor not in GIT_EDITORS:
        raise HTTPException(403, "this editor has no Git access")
    if protocol != "https":
        raise HTTPException(404, "Git tokens are used over HTTPS only")
    row, reason = gc.for_workspace(db, ws, _host(host))
    secret = gc.token(store, row) if row else None
    if not secret:
        raise HTTPException(404, reason or f"the token of {row.label} is missing; save it again under Account → Git credentials")
    return PlainTextResponse(f"username={row.username}\npassword={secret}\n", headers={"Cache-Control": "no-store"})

"""People's own Git tokens: saved in the secret store, served to git in their running workspaces.

A person keeps one or more credentials (e.g. "GitHub work" and "GitHub personal"), each for a host, with the
name and email to commit as. Each VS Code or RStudio workspace uses the one its owner chose (the app asks at
the first start). A credential helper in the editor asks the platform for the token of the host git talks to,
with the workspace's execution token, so the token is never written to the workspace volume. Platform service
accounts only read from Git (deployments fetch); people push with their own tokens.
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GitCredential, Workspace
from .secrets import SecretStore

# github.com, or git.internal:8443 for a self-hosted server on another port.
HOST = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(:[1-9][0-9]{0,4})?$")


def valid_host(host: str) -> bool:
    return len(host) <= 255 and bool(HOST.fullmatch(host))


def _owner_part(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()[:32]


def path(owner: str, credential_id: str) -> str:
    return f"git/{_owner_part(owner)}/id/{credential_id}"


def legacy_path(owner: str, host: str) -> str:
    """Where tokens were kept while a person had one credential per host."""
    return f"git/{_owner_part(owner)}/{host}"


def owned(db: Session, owner: str) -> List[GitCredential]:
    return list(db.scalars(select(GitCredential).where(GitCredential.owner == owner).order_by(GitCredential.label)))


def token(store: SecretStore, row: GitCredential) -> Optional[str]:
    return store.read(path(row.owner, row.id)).get("token") or store.read(legacy_path(row.owner, row.host)).get("token")


def for_workspace(db: Session, ws: Workspace, host: str) -> Tuple[Optional[GitCredential], str]:
    """(credential, reason when none): the workspace's chosen credential for its host; for another host, the
    owner's only credential for it. A workspace whose owner chose no Git gets nothing."""
    chosen = db.get(GitCredential, ws.git_credential_id) if ws.git_credential_id else None
    if chosen is not None and chosen.owner == ws.owner and chosen.host == host:
        return chosen, ""
    if ws.git_asked and chosen is None:
        return None, "this workspace uses no Git credential; choose one on the workspace's page in the app"
    candidates = [c for c in owned(db, ws.owner) if c.host == host]
    if len(candidates) == 1:
        return candidates[0], ""
    if candidates:
        return None, f"you have several credentials for {host}; choose one on the workspace's page in the app"
    return None, f"no Git credential for {host}: add one in the app under Account → Git credentials"


def identity(db: Session, ws: Workspace) -> Tuple[str, str]:
    """(name, email) to commit as in this workspace, from its credential; empty when not set."""
    chosen = db.get(GitCredential, ws.git_credential_id) if ws.git_credential_id else None
    if chosen is None or chosen.owner != ws.owner:
        return "", ""
    return chosen.commit_name, chosen.commit_email

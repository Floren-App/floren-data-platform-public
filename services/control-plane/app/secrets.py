"""Secret storage: data source, Git, program, and certificate credentials.

Backends: OpenBao (MPL-2.0) or HashiCorp Vault through the same API, namespace-scoped Kubernetes,
AWS Secrets Manager, Azure Key Vault, Google Secret Manager, or test memory.

Paths: datasources/<name> for data sources, repos/<name> for git repositories.
"""

from __future__ import annotations

from typing import Dict, Optional, Protocol

from .config import Settings


class SecretBackendError(Exception):
    """A sanitized backend error; never include provider responses or secret material."""


def datasource_path(name: str) -> str:
    return f"datasources/{name}"


def repo_path(name: str) -> str:
    return f"repos/{name}"


class SecretStore(Protocol):
    def put(self, path: str, secret: Dict[str, str]) -> None: ...
    def read(self, path: str) -> Dict[str, str]: ...
    def delete(self, path: str) -> None: ...
    def credentials(self, source: str, config: dict) -> Dict[str, str]: ...


class MemorySecretStore:
    """For tests and offline development only."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, str]] = {}

    def put(self, path: str, secret: Dict[str, str]) -> None:
        self._data[path] = dict(secret)

    def read(self, path: str) -> Dict[str, str]:
        return dict(self._data.get(path, {}))

    def delete(self, path: str) -> None:
        self._data.pop(path, None)

    def credentials(self, source: str, config: dict) -> Dict[str, str]:
        return self.read(datasource_path(source))


class OpenBaoSecretStore:
    """OpenBao or HashiCorp Vault (same API): static credentials in KV v2, short-lived ones from the
    database secrets engine.

    The platform authenticates with a token, its Kubernetes service account, or an AppRole. Login tokens
    are renewed by logging in again shortly before they expire, or after the server refuses one.
    If a data source config sets ``dynamic_role``, every connect() gets a fresh username/password with a lease.
    """

    label = 'OpenBao'
    auth = 'token'
    _expires = None
    _lock = None

    def __init__(self, settings: Settings):
        import threading

        import hvac

        self.label = 'Vault' if settings.secrets_backend == 'vault' else 'OpenBao'
        self.mount = settings.openbao_kv_mount
        self.auth = settings.openbao_auth
        self.role = settings.openbao_auth_role
        self.auth_mount = settings.openbao_auth_mount or settings.openbao_auth
        self.jwt_file = settings.kubernetes_token_file
        self.role_id, self.secret_id = settings.openbao_approle_role_id, settings.openbao_approle_secret_id
        self._lock = threading.Lock()
        token = None
        if self.auth == 'token':
            token = settings.openbao_token
            if not token and settings.openbao_token_file:
                with open(settings.openbao_token_file, encoding="utf-8") as f:
                    token = f.read().strip()
        self.client = hvac.Client(url=settings.openbao_addr, token=token,
                                  namespace=settings.openbao_namespace or None, timeout=15)
        # hvac falls back to VAULT_TOKEN or ~/.vault-token: use only the configured identity.
        self.client.token = token

    def _login(self) -> None:
        """Exchange the platform identity for a token. Called under the lock."""
        import time

        # The server refuses every request that carries an invalid token, logins included.
        self.client.token = None

        if self.auth == 'kubernetes':
            try:
                with open(self.jwt_file, encoding='utf-8') as f:
                    jwt = f.read().strip()
            except OSError:
                raise SecretBackendError('The platform service account token is unavailable for '
                                         f'{self.label} login; check automountServiceAccountToken') from None
            response = self.client.auth.kubernetes.login(role=self.role, jwt=jwt, mount_point=self.auth_mount)
        else:
            response = self.client.auth.approle.login(role_id=self.role_id, secret_id=self.secret_id,
                                                      mount_point=self.auth_mount)
        lease = int(response['auth'].get('lease_duration') or 0)
        # Log in again at 80% of the lease (renewable tokens may still end at their maximum TTL).
        self._expires = time.monotonic() + lease * 0.8 if lease else None

    def _ensure_login(self, force: bool = False) -> None:
        import time

        if self.auth == 'token':
            return
        with self._lock:
            if force or not self.client.token or (self._expires is not None and time.monotonic() >= self._expires):
                self._call_once(self._login)

    def _call_once(self, operation):
        """Run one request; failures become sanitized 503s (never provider responses)."""
        import hvac
        import requests

        try:
            return operation()
        except hvac.exceptions.InvalidPath:
            raise
        except hvac.exceptions.VaultDown:
            raise SecretBackendError(f'{self.label} is sealed or starting; unseal it, then retry') from None
        except hvac.exceptions.Forbidden:
            raise SecretBackendError(f'{self.label} refused the platform token; check its policy and that it has not expired') from None
        except (requests.exceptions.RequestException, OSError):
            raise SecretBackendError(f'Cannot reach {self.label}; check its address and network access') from None
        except hvac.exceptions.VaultError:
            raise SecretBackendError(f'{self.label} rejected the request; check its logs') from None

    def _call(self, operation):
        import hvac

        self._ensure_login()
        if self.auth == 'token':
            return self._call_once(operation)
        try:
            return operation()
        except hvac.exceptions.Forbidden:
            self._ensure_login(force=True)   # the login token expired or was revoked: log in once more
        except hvac.exceptions.InvalidPath:
            raise
        except Exception:
            pass   # sanitized below by the retry
        return self._call_once(operation)

    def put(self, path: str, secret: Dict[str, str]) -> None:
        self._call(lambda: self.client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=secret, mount_point=self.mount))

    def read(self, path: str) -> Dict[str, str]:
        import hvac

        try:
            resp = self._call(lambda: self.client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self.mount, raise_on_deleted_version=True))
        except hvac.exceptions.InvalidPath:
            return {}
        return dict(resp["data"]["data"])

    def delete(self, path: str) -> None:
        import hvac

        try:
            self._call(lambda: self.client.secrets.kv.v2.delete_metadata_and_all_versions(path=path, mount_point=self.mount))
        except hvac.exceptions.InvalidPath:
            pass

    def credentials(self, source: str, config: dict) -> Dict[str, str]:
        role: Optional[str] = config.get("dynamic_role")
        if role:
            resp = self._call(lambda: self.client.secrets.database.generate_credentials(name=role))
            data = resp["data"]
            return {"username": data["username"], "password": data["password"],
                    "lease_seconds": str(resp.get("lease_duration", 0))}
        return self.read(datasource_path(source))


def make_secret_store(settings: Settings) -> SecretStore:
    if settings.secrets_backend == "memory":
        return MemorySecretStore()
    if settings.secrets_backend == 'kubernetes':
        from .kubernetes_secrets import KubernetesSecretStore
        return KubernetesSecretStore(settings)
    if settings.secrets_backend in ('aws', 'azure', 'gcp'):
        from . import cloud_secrets
        return cloud_secrets.make(settings)
    return OpenBaoSecretStore(settings)

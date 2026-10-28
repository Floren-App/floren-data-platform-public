"""Secret storage: data source, Git, program, and certificate credentials.

Backends: external OpenBao (MPL-2.0), namespace-scoped Kubernetes, or test memory.

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
    """Static credentials in KV v2; short-lived ones from the database secrets engine.

    If a data source config sets ``dynamic_role``, every connect() gets a fresh
    username/password with a lease, so nothing long-lived ever reaches an execution.
    """

    def __init__(self, settings: Settings):
        import hvac

        self.mount = settings.openbao_kv_mount
        token = settings.openbao_token
        if not token and settings.openbao_token_file:
            with open(settings.openbao_token_file, encoding="utf-8") as f:
                token = f.read().strip()
        self.client = hvac.Client(url=settings.openbao_addr, token=token)

    @staticmethod
    def _call(operation):
        """Run one OpenBao request; failures become sanitized 503s (never provider responses)."""
        import hvac
        import requests

        try:
            return operation()
        except hvac.exceptions.InvalidPath:
            raise
        except hvac.exceptions.VaultDown:
            raise SecretBackendError('OpenBao is sealed or starting; unseal it, then retry') from None
        except hvac.exceptions.Forbidden:
            raise SecretBackendError('OpenBao refused the platform token; check its policy and that it has not expired') from None
        except requests.exceptions.RequestException:
            raise SecretBackendError('Cannot reach OpenBao; check its address and network access') from None
        except hvac.exceptions.VaultError:
            raise SecretBackendError('OpenBao rejected the request; check its logs') from None

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
    return OpenBaoSecretStore(settings)

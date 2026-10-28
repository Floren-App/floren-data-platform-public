"""Secret values in a cloud provider's secret manager: AWS Secrets Manager, Azure Key Vault or Google Secret Manager.

Each platform secret path (``datasources/sales``, ``env/user/ana/token``) becomes one provider secret holding
the JSON-encoded key/value map. The platform authenticates with the pod's workload identity through the
provider's default credential chain; no static cloud key is required. Errors are sanitized: provider responses,
secret names and values never reach callers or logs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Dict

from .config import Settings
from .secrets import SecretBackendError, datasource_path


def provider_name(prefix: str, path: str, limit: int, allowed: str = r'A-Za-z0-9-') -> str:
    """A stable provider-safe name: a readable form of the path plus a hash that keeps names unique."""
    readable = re.sub(f'[^{allowed}]+', '-', path).strip('-')
    digest = hashlib.sha256(path.encode()).hexdigest()[:16]
    head = (prefix + readable)[:limit - len(digest) - 1].rstrip('-')
    return f'{head}-{digest}'


def _encode(secret: Dict[str, str]) -> str:
    return json.dumps({str(k): str(v) for k, v in secret.items()}, separators=(',', ':'))


def _decode(value) -> Dict[str, str]:
    if isinstance(value, bytes):
        value = value.decode()
    try:
        data = json.loads(value or '{}')
    except ValueError:
        raise SecretBackendError('A stored secret has an invalid payload; restore it from backup') from None
    if not isinstance(data, dict):
        raise SecretBackendError('A stored secret has an invalid payload; restore it from backup')
    return {str(k): str(v) for k, v in data.items()}


class _CloudStore:
    label = 'The secret store'

    def unreachable(self):
        return SecretBackendError(f'Cannot reach {self.label}; check its address and network access')

    def denied(self):
        return SecretBackendError(f'{self.label} refused the platform identity; check its permissions')

    def rejected(self):
        return SecretBackendError(f'{self.label} rejected the request; check its logs')

    def credentials(self, source: str, config: dict) -> Dict[str, str]:
        if config.get('dynamic_role'):
            raise SecretBackendError('Dynamic database credentials require the OpenBao or Vault backend')
        return self.read(datasource_path(source))


class AwsSecretStore(_CloudStore):
    """AWS Secrets Manager. Deleted secrets keep a recovery window unless it is 0; writing a path again
    restores its secret first, so a name can be reused within the window."""

    label = 'AWS Secrets Manager'
    SAFE = re.compile(r'[A-Za-z0-9/_+=.@-]{1,500}')

    def __init__(self, settings: Settings, client=None):
        self.prefix = settings.aws_secrets_prefix
        self.kms_key_id = settings.aws_secrets_kms_key_id
        self.recovery_days = settings.aws_secrets_recovery_window_days
        if self.recovery_days and not 7 <= self.recovery_days <= 30:
            raise ValueError('aws_secrets_recovery_window_days must be 0 or between 7 and 30')
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client('secretsmanager', region_name=settings.aws_secrets_region or None,
                                  endpoint_url=settings.aws_secrets_endpoint_url or None,
                                  config=Config(connect_timeout=5, read_timeout=15,
                                                retries={'max_attempts': 3, 'mode': 'standard'}))
        self.client = client

    def name(self, path: str) -> str:
        name = self.prefix + path
        return name if self.SAFE.fullmatch(name) else provider_name(self.prefix, path, 500, r'A-Za-z0-9/_+=.@-')

    @staticmethod
    def _code(exc) -> str:
        return getattr(exc, 'response', {}).get('Error', {}).get('Code', '')

    def _call(self, operation, missing=None):
        from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError

        try:
            return operation()
        except ClientError as exc:
            code = self._code(exc)
            if missing is not None and code in missing:
                return missing[code]
            if code in ('AccessDeniedException', 'UnrecognizedClientException', 'ExpiredTokenException',
                        'InvalidSignatureException', 'KMSAccessDeniedException'):
                raise self.denied() from None
            raise self.rejected() from None
        except NoCredentialsError:
            raise SecretBackendError(f'{self.label} has no platform identity; configure workload credentials') from None
        except (EndpointConnectionError, OSError):
            raise self.unreachable() from None
        except BotoCoreError:
            raise self.rejected() from None

    def put(self, path: str, secret: Dict[str, str]) -> None:
        name, value = self.name(path), _encode(secret)
        put = lambda: self.client.put_secret_value(SecretId=name, SecretString=value)
        state = self._call(lambda: (put(), 'stored')[1],
                           missing={'ResourceNotFoundException': 'missing', 'InvalidRequestException': 'deleted'})
        if state == 'missing':
            create = {'Name': name, 'SecretString': value, 'Tags': [{'Key': 'managed-by', 'Value': 'fdp'}]}
            if self.kms_key_id:
                create['KmsKeyId'] = self.kms_key_id
            self._call(lambda: self.client.create_secret(**create))
        elif state == 'deleted':
            self._call(lambda: self.client.restore_secret(SecretId=name))
            self._call(put)

    def read(self, path: str) -> Dict[str, str]:
        gone = {'ResourceNotFoundException': None, 'InvalidRequestException': None}
        response = self._call(lambda: self.client.get_secret_value(SecretId=self.name(path)), missing=gone)
        return _decode(response.get('SecretString')) if response else {}

    def delete(self, path: str) -> None:
        options = ({'ForceDeleteWithoutRecovery': True} if not self.recovery_days
                   else {'RecoveryWindowInDays': self.recovery_days})
        self._call(lambda: self.client.delete_secret(SecretId=self.name(path), **options),
                   missing={'ResourceNotFoundException': None, 'InvalidRequestException': None})


class AzureSecretStore(_CloudStore):
    """Azure Key Vault. Soft-deleted secrets are recovered when their path is written again."""

    label = 'Azure Key Vault'
    MAX_BYTES = 25 * 1024

    def __init__(self, settings: Settings, client=None):
        if not settings.azure_key_vault_url:
            raise ValueError('azure_key_vault_url is required for the azure secret backend')
        self.prefix = settings.azure_secrets_prefix
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
            client = SecretClient(vault_url=settings.azure_key_vault_url, credential=DefaultAzureCredential())
        self.client = client

    def name(self, path: str) -> str:
        return provider_name(self.prefix, path, 127)

    DELETED = object()   # 409: deleted but recoverable under the same name

    def _call(self, operation, missing=False, deleted=False):
        from azure.core.exceptions import (ClientAuthenticationError, HttpResponseError, ResourceNotFoundError,
                                           ServiceRequestError)

        try:
            return operation()
        except ResourceNotFoundError:
            if missing:
                return None
            raise self.rejected() from None
        except ClientAuthenticationError:
            raise self.denied() from None
        except ServiceRequestError:
            raise self.unreachable() from None
        except HttpResponseError as exc:
            if deleted and exc.status_code == 409:
                return self.DELETED
            if exc.status_code in (401, 403):
                raise self.denied() from None
            raise self.rejected() from None
        except OSError:
            raise self.unreachable() from None

    def put(self, path: str, secret: Dict[str, str]) -> None:
        name, value = self.name(path), _encode(secret)
        if len(value.encode()) > self.MAX_BYTES:
            raise SecretBackendError(f'The secret exceeds the {self.label} size limit of 25 KB')
        store = lambda: self.client.set_secret(name, value, content_type='application/json',
                                               tags={'managed-by': 'fdp'})
        if self._call(store, deleted=True) is self.DELETED:
            self._call(lambda: self.client.begin_recover_deleted_secret(name).wait())
            self._call(store)

    def read(self, path: str) -> Dict[str, str]:
        secret = self._call(lambda: self.client.get_secret(self.name(path)), missing=True)
        return _decode(secret.value) if secret is not None else {}

    def delete(self, path: str) -> None:
        self._call(lambda: self.client.begin_delete_secret(self.name(path)), missing=True)


class GcpSecretStore(_CloudStore):
    """Google Secret Manager. Only the latest version of each secret stays enabled; older ones are destroyed."""

    label = 'Google Secret Manager'

    def __init__(self, settings: Settings, client=None):
        if not settings.gcp_secrets_project:
            raise ValueError('gcp_secrets_project is required for the gcp secret backend')
        self.parent = f'projects/{settings.gcp_secrets_project}'
        self.prefix = settings.gcp_secrets_prefix
        self.locations = [l.strip() for l in settings.gcp_secrets_locations.split(',') if l.strip()]
        if client is None:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
        self.client = client

    def secret(self, path: str) -> str:
        return f'{self.parent}/secrets/{provider_name(self.prefix, path, 255, "A-Za-z0-9_-")}'

    def _call(self, operation, missing=False):
        from google.api_core import exceptions as gexc
        from google.auth.exceptions import DefaultCredentialsError, RefreshError

        try:
            return operation()
        except (gexc.NotFound, gexc.FailedPrecondition):
            if missing:
                return None
            raise self.rejected() from None
        except (gexc.PermissionDenied, gexc.Unauthenticated, DefaultCredentialsError, RefreshError):
            raise self.denied() from None
        except (gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.RetryError, OSError):
            raise self.unreachable() from None
        except gexc.GoogleAPICallError:
            raise self.rejected() from None

    def put(self, path: str, secret: Dict[str, str]) -> None:
        name = self.secret(path)
        payload = {'data': _encode(secret).encode()}
        version = self._call(lambda: self.client.add_secret_version(request={'parent': name, 'payload': payload}),
                             missing=True)
        if version is None:
            replication = ({'user_managed': {'replicas': [{'location': l} for l in self.locations]}}
                           if self.locations else {'automatic': {}})
            self._call(lambda: self.client.create_secret(request={
                'parent': self.parent, 'secret_id': name.rsplit('/', 1)[1],
                'secret': {'replication': replication, 'labels': {'managed-by': 'fdp'}}}))
            version = self._call(lambda: self.client.add_secret_version(request={'parent': name, 'payload': payload}))
        # Secret values are write-only in the platform: keep no readable history of former values.
        versions = self._call(lambda: list(self.client.list_secret_versions(
            request={'parent': name, 'filter': 'state:ENABLED'})))
        for old in versions:
            if old.name != version.name:
                self._call(lambda: self.client.destroy_secret_version(request={'name': old.name}), missing=True)

    def read(self, path: str) -> Dict[str, str]:
        response = self._call(lambda: self.client.access_secret_version(
            request={'name': self.secret(path) + '/versions/latest'}), missing=True)
        return _decode(response.payload.data) if response is not None else {}

    def delete(self, path: str) -> None:
        self._call(lambda: self.client.delete_secret(request={'name': self.secret(path)}), missing=True)


def make(settings: Settings):
    return {'aws': AwsSecretStore, 'azure': AzureSecretStore, 'gcp': GcpSecretStore}[settings.secrets_backend](settings)

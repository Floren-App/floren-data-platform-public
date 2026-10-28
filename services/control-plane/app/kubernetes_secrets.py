"""Persistent application secrets in a dedicated, server-configured namespace.

Application IAM remains authoritative. Executions get neither this service account
nor permission to access the namespace. Cluster encryption-at-rest is an operator
prerequisite, rather than a property of Kubernetes' base64 encoding.
"""
import base64
import hashlib
import json

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as TransportError

from .secrets import SecretBackendError, datasource_path


class KubernetesSecretStore:
    def __init__(self, settings, core=None):
        if core is None:
            config.load_incluster_config()
            core = client.CoreV1Api()
        self.core = core
        self.namespace = settings.k8s_secret_namespace

    @staticmethod
    def name(path):
        return 'dc-' + hashlib.sha256(path.encode()).hexdigest()[:48]

    def _get(self, path):
        try:
            return self.core.read_namespaced_secret(self.name(path), self.namespace, _request_timeout=15)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise SecretBackendError('Kubernetes secret storage is unavailable; check its namespace and service-account permissions') from None
        except TransportError:
            raise SecretBackendError('Kubernetes secret storage is unavailable; retry when the API server is reachable') from None

    def read(self, path):
        row = self._get(path)
        if row is None:
            return {}
        try:
            value = json.loads(base64.b64decode((row.data or {})['payload'], validate=True))
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                raise ValueError()
            return value
        except (KeyError, ValueError, TypeError):
            raise SecretBackendError('Stored Kubernetes secret has an invalid payload; restore it from backup') from None

    def put(self, path, secret):
        if not isinstance(secret, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in secret.items()):
            raise SecretBackendError('Secret payload must contain string keys and values')
        payload = json.dumps(secret, ensure_ascii=False).encode()
        if len(payload) > 768 * 1024:
            raise SecretBackendError('Secret payload exceeds the Kubernetes storage limit')
        for _ in range(4):
            previous = self._get(path)
            body = client.V1Secret(type='Opaque', data={'payload':base64.b64encode(payload).decode()},
                metadata=client.V1ObjectMeta(name=self.name(path), namespace=self.namespace,
                    resource_version=previous.metadata.resource_version if previous else None,
                    labels={'fdp.floren.app/managed-secret':'true'}))
            try:
                if previous:
                    self.core.replace_namespaced_secret(body.metadata.name, self.namespace, body, _request_timeout=15)
                else:
                    self.core.create_namespaced_secret(self.namespace, body, _request_timeout=15)
                return
            except ApiException as exc:
                if exc.status in (404, 409):
                    continue
                raise SecretBackendError('Could not persist the Kubernetes secret; check storage permissions and availability') from None
            except TransportError:
                raise SecretBackendError('Could not reach Kubernetes secret storage; retry the update') from None
        raise SecretBackendError('Secret changed concurrently; retry the update')

    def delete(self, path):
        try:
            self.core.delete_namespaced_secret(self.name(path), self.namespace, _request_timeout=15)
        except ApiException as exc:
            if exc.status != 404:
                raise SecretBackendError('Could not delete the Kubernetes secret') from None
        except TransportError:
            raise SecretBackendError('Could not reach Kubernetes secret storage; retry the deletion') from None

    def credentials(self, source, source_config):
        if source_config.get('dynamic_role'):
            raise SecretBackendError('Dynamic database credentials require the OpenBao backend')
        return self.read(datasource_path(source))

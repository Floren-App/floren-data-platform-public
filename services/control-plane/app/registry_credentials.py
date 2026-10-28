"""Private registry access for the pods the platform starts.

The release namespace holds one kubernetes.io/dockerconfigjson Secret (Helm registryCredentials). The
control plane keeps a copy under the same name in the runtime namespace and every managed layer namespace,
and every workspace, job, app and layer helper pod references it. A Secret of that name the platform did
not write is never touched.
"""
import hashlib
import logging

log = logging.getLogger(__name__)


def digest(config: str) -> str:
    return hashlib.sha256(config.encode()).hexdigest()[:32]


def sync_once(db, orch) -> list:
    """Write the pull secret where it is missing or outdated; returns the namespaces written."""
    from .config import get_settings
    from .layer_infrastructure import managed_namespaces
    s = get_settings()
    if not s.image_pull_secret or not s.image_pull_config or not hasattr(orch, "ensure_pull_secret"):
        return []
    wanted = digest(s.image_pull_config)
    written = []
    for namespace in [s.k8s_namespace, *managed_namespaces(db)]:
        if orch.ensure_pull_secret(namespace, s.image_pull_secret, s.image_pull_config, wanted):
            written.append(namespace)
    return written

"""Pod exec must never hijack the shared Kubernetes client used by concurrent calls."""
from types import SimpleNamespace

from kubernetes import client

from app.orchestrator import kubernetes as k8s


def test_exec_streams_on_a_private_client_and_leaves_the_shared_one_alone(monkeypatch):
    shared = client.ApiClient()
    # kubernetes.stream swaps the client's request method while it runs: `request` in older client
    # versions, `call_api` in newer ones. Either way the shared client must keep its own.
    swapped = 'request' if hasattr(shared, 'request') else 'call_api'
    original_request = getattr(shared, swapped)
    orch = k8s.KubernetesOrchestrator.__new__(k8s.KubernetesOrchestrator)
    orch.core, orch.ns = client.CoreV1Api(api_client=shared), 'fdp-runtime'
    seen = {}

    def fake_stream(method, name, namespace, **kwargs):
        # The real stream() patches method.__self__.api_client.request while it runs.
        seen['client'] = method.__self__.api_client
        seen['shared_untouched'] = getattr(shared, swapped) == original_request
        return SimpleNamespace(is_open=lambda: False, close=lambda: None, returncode=0)
    monkeypatch.setattr(k8s, 'stream', fake_stream)
    assert orch._exec('ns/pod', ['true']) == (0, '')
    assert seen['client'] is not shared and seen['shared_untouched']
    assert seen['client'].configuration is shared.configuration

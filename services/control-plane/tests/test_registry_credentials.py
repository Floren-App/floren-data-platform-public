"""Private registries: every pod the platform starts pulls with one pull secret, copied where it runs."""
from types import SimpleNamespace

from kubernetes import client as k8s

from app import deps, layers
from app.config import get_settings
from app.db import SessionLocal
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import WorkerPlan, manifests
from app.orchestrator.kubernetes import KubernetesOrchestrator
from app.registry_credentials import digest, sync_once
from .conftest import as_
from .test_layer_controller import CONFIG
from .test_layer_infrastructure import managed
from .test_layer_workspaces import Core, spec

CONFIG_JSON = '{"auths":{"ghcr.io":{"auth":"dXNlcjp0b2tlbg=="}}}'


def test_the_pull_secret_is_kept_in_the_runtime_and_every_layer_namespace(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "image_pull_secret", "ghcr-pull")
    monkeypatch.setattr(s, "image_pull_config", CONFIG_JSON)
    monkeypatch.setattr(s, "layer_namespace_prefix", "fdp-x-layer")
    client.put(f"/v1/admin/layers/{layers.NON_PROD}/infrastructure", headers=as_("admin"),
               json={"expected_generation": 0, "spec": managed()}).raise_for_status()
    orch = deps.get_orchestrator()
    layer_ns = f"fdp-x-layer-{layers.NON_PROD}"
    orch.missing_namespaces.add(layer_ns)  # the controller has not created it yet
    with SessionLocal() as db:
        assert sync_once(db, orch) == [s.k8s_namespace]
        orch.missing_namespaces.clear()
        assert sync_once(db, orch) == [layer_ns]
        assert sync_once(db, orch) == []  # unchanged: nothing rewritten
        monkeypatch.setattr(s, "image_pull_config", CONFIG_JSON.replace("dXNl", "bmV3"))  # a new token
        assert sorted(sync_once(db, orch)) == sorted([s.k8s_namespace, layer_ns])
    stored = orch.layer_secrets[(layer_ns, "ghcr-pull")]
    assert stored[1] == {".dockerconfigjson": CONFIG_JSON.replace("dXNl", "bmV3")}


def test_without_registry_credentials_nothing_is_written(client):
    with SessionLocal() as db:
        assert sync_once(db, deps.get_orchestrator()) == []


def test_kubernetes_never_overwrites_a_pull_secret_it_did_not_write():
    core = Core()
    orch = KubernetesOrchestrator(SimpleNamespace(k8s_namespace="fdp-runtime", k8s_storage_class=None,
                                                  image_pull_secret="ghcr-pull"), core=core, net=object(),
                                  layer_namespaces=lambda: [])
    theirs = k8s.V1Secret(metadata=k8s.V1ObjectMeta(name="ghcr-pull", labels={}), type="kubernetes.io/dockerconfigjson")
    core.objects[("team", "secret", "ghcr-pull")] = theirs
    assert orch.ensure_pull_secret("team", "ghcr-pull", CONFIG_JSON, digest(CONFIG_JSON)) is False
    assert core.objects[("team", "secret", "ghcr-pull")] is theirs
    assert orch.ensure_pull_secret("fdp-runtime", "ghcr-pull", CONFIG_JSON, digest(CONFIG_JSON)) is True
    written = core.objects[("fdp-runtime", "secret", "ghcr-pull")]
    assert written.type == "kubernetes.io/dockerconfigjson" and written.string_data == {".dockerconfigjson": CONFIG_JSON}
    assert orch.ensure_pull_secret("fdp-runtime", "ghcr-pull", CONFIG_JSON, digest(CONFIG_JSON)) is False

    orch.start(spec())  # every pod the platform starts references it
    pod = core.objects[("fdp-runtime", "pod", "ws-abc")]
    assert [ref.name for ref in pod.spec.image_pull_secrets] == ["ghcr-pull"]


def test_layer_pods_pull_with_the_layer_copy():
    from dataclasses import replace
    docs = manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(managed()), replace(CONFIG, image_pull_secret="ghcr-pull"),
                     workers=WorkerPlan("trino", 1, 4, 1))
    pods = [d["spec"]["template"]["spec"] for d in docs if d["kind"] in ("Deployment", "StatefulSet")]
    assert pods and all(p["imagePullSecrets"] == [{"name": "ghcr-pull"}] for p in pods)
    plain = manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(managed()), CONFIG, workers=WorkerPlan("trino", 1, 4, 1))
    assert not any("imagePullSecrets" in d["spec"]["template"]["spec"] for d in plain if d["kind"] == "Deployment")

"""Kubernetes backend — production. Each execution is a Pod (+ Service when it serves HTTP).

Variable load is handled by the cluster autoscaler: pods request exactly their
pool size, so nodes are added when pools are busy and removed when they suspend.
Data egress is locked down by NetworkPolicies keyed on ``fdp.floren.app/ds-<name>``
labels; see deploy/helm/fdp/templates/networkpolicy.yaml.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from ..config import Settings
from .base import CapacityError, Placement, ExecutionHandle, ExecutionSpec, list_files_command, parse_list, parse_read, read_file_command


TRUST_DIR = "/etc/fdp/trust"
# Python (ssl, requests, botocore), OpenSSL/curl (R, CLI), Git and Node (editors) read these.
TRUST_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "AWS_CA_BUNDLE", "GIT_SSL_CAINFO",
             "NODE_EXTRA_CA_CERTS")


IMAGE_PROBLEMS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull")


def _scheduling(message: str) -> str:
    """The scheduler's reason without its preemption details, e.g. "no node has enough memory free"."""
    import re
    resources = sorted({name.rstrip(".,") for name in re.findall(r"Insufficient ([\w./-]+)", message)})
    if resources:
        return "no node has enough " + " and ".join(r.replace("nvidia.com/gpu", "GPU") for r in resources) + " free"
    return message.split(" preemption:")[0].strip().rstrip(".") or "no node can run it yet"


class KubernetesOrchestrator:
    def inventory(self) -> List[dict]:
        items = []
        for namespace in [self.ns, *self.layer_namespaces()]:
            prefix = "" if namespace == self.ns else namespace + "/"
            for p in self.core.list_namespaced_pod(namespace, label_selector="fdp.floren.app/managed=true").items:
                items.append({"ref": prefix + p.metadata.name, "labels": p.metadata.labels or {},
                              "state": {"Running": "running", "Pending": "pending"}.get(p.status.phase, "finished")})
        return items

    def __init__(self, settings: Settings, core=None, net=None, layer_namespaces=None):
        if core is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        self.core = core or client.CoreV1Api()
        self.net = net or client.NetworkingV1Api()
        self.ns = settings.k8s_namespace
        self.pull_secret = getattr(settings, "image_pull_secret", "") or None
        self.storage_class = settings.k8s_storage_class
        self.trust_configmap = getattr(settings, "k8s_trust_configmap", None)
        self.layer_namespaces = layer_namespaces or _managed_layer_namespaces

    def _split(self, ref: str) -> Tuple[str, str]:
        """Refs are "name" in the shared namespace and "namespace/name" in a managed layer."""
        namespace, _, name = ref.rpartition("/")
        return namespace or self.ns, name

    def _ensure_home(self, claim: str, namespace: str, placement: Optional[Placement]) -> None:
        try:
            self.core.read_namespaced_persistent_volume_claim(claim, namespace)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise
        # Shared namespace: ReadWriteMany so the workspace and its compute runs see the same files.
        # A layer volume uses that layer's storage settings and never leaves its namespace.
        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=claim, labels={"fdp.floren.app/managed": "true"}),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=[placement.access_mode if placement else "ReadWriteMany"],
                storage_class_name=placement.storage_class if placement else self.storage_class,
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": placement.volume_size if placement else "20Gi"}),
            ),
        )
        self.core.create_namespaced_persistent_volume_claim(namespace, pvc)

    def start(self, spec: ExecutionSpec) -> ExecutionHandle:
        placement = spec.placement
        ns = placement.namespace if placement else self.ns
        ref = f"{ns}/{spec.name}" if placement else spec.name
        if placement and any(m.volume != placement.artifact_claim for m in spec.mounts):
            # Only the layer's own artifact volume; a shared volume never crosses into a layer.
            raise ValueError("managed layer executions may mount only their layer's artifact volume")
        self.stop(ref)
        labels = {"fdp.floren.app/managed": "true", "fdp.floren.app/execution": spec.name, **spec.labels}
        resources = {"cpu": str(spec.cpu), "memory": f"{int(spec.memory_gb * 1024)}Mi"}
        limits = dict(resources)
        if spec.gpu:
            limits["nvidia.com/gpu"] = str(spec.gpu)

        volumes, mounts = [], []
        env = dict(spec.env)
        trust = placement.trust_configmap if placement else self.trust_configmap
        if trust:
            # The public CA bundle; it includes the public roots, so it replaces the defaults.
            volumes.append(client.V1Volume(name="trust", config_map=client.V1ConfigMapVolumeSource(
                name=trust, items=[client.V1KeyToPath(key="ca-bundle.pem", path="ca-bundle.pem")])))
            mounts.append(client.V1VolumeMount(name="trust", mount_path=TRUST_DIR, read_only=True))
            env.update({name: f"{TRUST_DIR}/ca-bundle.pem" for name in TRUST_ENV})
        if spec.home_volume:
            self._ensure_home(spec.home_volume, ns, placement)
            volumes.append(client.V1Volume(
                name="home",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=spec.home_volume),
            ))
            mounts.append(client.V1VolumeMount(name="home", mount_path="/workspace"))
        claims: dict = {}
        for m in spec.mounts:
            vol_name = claims.setdefault(m.volume, f"data-{len(claims)}")
            mounts.append(client.V1VolumeMount(name=vol_name, mount_path=m.target, sub_path=m.subpath or None))
        for claim, vol_name in claims.items():
            volumes.append(client.V1Volume(
                name=vol_name, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=claim)))

        env_from = None
        if spec.secret_env:
            # Secret values go in a Secret object owned by this execution, not in the pod spec.
            self.core.create_namespaced_secret(ns, client.V1Secret(
                metadata=client.V1ObjectMeta(name=f"{spec.name}-env", labels=labels),
                string_data=spec.secret_env, type="Opaque"))
            env_from = [client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name=f"{spec.name}-env"))]
        container = client.V1Container(
            name="main",
            image=spec.image,
            args=spec.command,
            env=[client.V1EnvVar(name=k, value=v) for k, v in env.items()],
            env_from=env_from,
            ports=[client.V1ContainerPort(container_port=spec.port)] if spec.port else None,
            resources=client.V1ResourceRequirements(requests=resources, limits=limits),
            volume_mounts=mounts,
            working_dir=spec.working_dir or ("/workspace" if spec.home_volume else None),
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                run_as_non_root=spec.run_as_user is not None,
                run_as_user=spec.run_as_user,
                run_as_group=spec.run_as_group,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=spec.name, labels=labels),
            spec=client.V1PodSpec(
                containers=[container],
                volumes=volumes,
                restart_policy="Never" if spec.port is None else "Always",
                image_pull_secrets=[client.V1LocalObjectReference(name=self.pull_secret)] if self.pull_secret else None,
                service_account_name=placement.service_account if placement else None,
                automount_service_account_token=False,
                security_context=client.V1PodSecurityContext(fs_group=1000),
            ),
        )
        try:
            self.core.create_namespaced_pod(ns, pod)
        except ApiException as exc:
            if exc.status == 403 and "exceeded quota" in (exc.body or ""):
                self.stop(ref)  # remove the env Secret created above
                raise CapacityError(f"This layer's resource quota is used up; {spec.cpu:g} vCPU and "
                                    f"{spec.memory_gb:g} GB were requested. Stop other executions or raise the "
                                    "layer quota.") from None
            raise

        url = None
        if spec.port:
            svc = client.V1Service(
                metadata=client.V1ObjectMeta(name=spec.name, labels=labels),
                spec=client.V1ServiceSpec(
                    selector={"fdp.floren.app/execution": spec.name},
                    ports=[client.V1ServicePort(port=spec.port, target_port=spec.port)],
                ),
            )
            self.core.create_namespaced_service(ns, svc)
            url = f"http://{spec.name}.{ns}.svc:{spec.port}"
        return ExecutionHandle(ref=ref, upstream_url=url)

    def stop(self, ref: str, grace_seconds: int = 0) -> None:
        ns, name = self._split(ref)
        for delete, item in ((self.core.delete_namespaced_pod, name), (self.core.delete_namespaced_service, name),
                             (self.core.delete_namespaced_secret, f"{name}-env")):
            try:
                delete(item, ns, grace_period_seconds=grace_seconds)
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def status(self, ref: str) -> str:
        try:
            phase = self.core.read_namespaced_pod(*reversed(self._split(ref))).status.phase
        except ApiException as exc:
            if exc.status == 404:
                return "missing"
            raise
        return {"Pending": "pending", "Running": "running", "Succeeded": "succeeded",
                "Failed": "failed"}.get(phase, "pending")

    def waiting_reason(self, ref: str) -> Optional[str]:
        try:
            pod = self.core.read_namespaced_pod(*reversed(self._split(ref)))
        except ApiException:
            return None
        status = pod.status
        if status is None or status.phase != "Pending":
            return None
        for condition in status.conditions or []:
            if condition.type == "PodScheduled" and condition.status == "False":
                return "waiting for cluster capacity: " + _scheduling(condition.message or "")
        for container in status.container_statuses or []:
            waiting = container.state.waiting if container.state else None
            if waiting and waiting.reason in IMAGE_PROBLEMS:
                return f"its image {container.image} cannot be pulled ({waiting.reason})"
            if waiting and waiting.reason in ("CreateContainerConfigError", "CreateContainerError"):
                return f"its container cannot be created ({waiting.reason})"
        return None

    def logs(self, ref: str, tail: int = 200) -> str:
        try:
            return self.core.read_namespaced_pod_log(*reversed(self._split(ref)), tail_lines=tail)
        except ApiException:
            return ""

    def _exec_api(self):
        """A private client per exec: kubernetes.stream swaps the request method of the client it is given
        for the duration of the stream, so on the shared client every concurrent call (status checks,
        inventory) would be sent as a websocket request and fail."""
        shared = getattr(self.core, "api_client", None)
        if not isinstance(shared, client.ApiClient):
            return self.core, None  # test doubles
        private = client.ApiClient(configuration=shared.configuration)
        return client.CoreV1Api(api_client=private), private

    def _exec(self, ref: str, cmd: List[str]) -> Tuple[int, str]:
        api, private = self._exec_api()
        try:
            try:
                ns, name = self._split(ref)
                resp = stream(api.connect_get_namespaced_pod_exec, name, ns, command=cmd,
                              stderr=True, stdin=False, stdout=True, tty=False, _preload_content=False)
            except ApiException:
                return 1, ""
            out = []
            while resp.is_open():
                resp.update(timeout=5)
                if resp.peek_stdout():
                    out.append(resp.read_stdout())
            resp.close()
            return resp.returncode or 0, "".join(out)
        finally:
            if private is not None:
                private.close()

    def layer_secret_digest(self, namespace: str, name: str) -> str:
        """The content digest a platform-written layer Secret was annotated with ('' when absent)."""
        try:
            secret = self.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return ""
            raise
        return (secret.metadata.annotations or {}).get("fdp.floren.app/digest", "")

    def ensure_pull_secret(self, namespace: str, name: str, config: str, digest: str) -> bool:
        """A registry pull secret written by the platform; True when it was (re)written. One the platform did
        not write is left alone, and a namespace that does not exist yet is skipped until it does."""
        try:
            existing = self.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            existing = None
        if existing is not None:
            meta = existing.metadata
            if (meta.labels or {}).get("app.kubernetes.io/managed-by") != "fdp-control-plane":
                return False
            if (meta.annotations or {}).get("fdp.floren.app/digest") == digest:
                return False
            self.core.delete_namespaced_secret(name, namespace)
        try:
            self.core.create_namespaced_secret(namespace, client.V1Secret(
                metadata=client.V1ObjectMeta(name=name, annotations={"fdp.floren.app/digest": digest},
                                             labels={"app.kubernetes.io/managed-by": "fdp-control-plane"}),
                string_data={".dockerconfigjson": config}, type="kubernetes.io/dockerconfigjson"))
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def read_layer_secret(self, namespace: str, name: str, key: str) -> Optional[str]:
        """One value of a Secret in a layer namespace (None when absent)."""
        import base64
        try:
            secret = self.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        value = (secret.data or {}).get(key)
        return base64.b64decode(value).decode() if value else None

    def write_layer_secret(self, namespace: str, name: str, data: Dict[str, str], digest: str) -> bool:
        """Replace a platform-written Secret in a layer (delete + create); False if the namespace is missing."""
        try:
            self.core.delete_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
        if not data:
            return True
        try:
            self.core.create_namespaced_secret(namespace, client.V1Secret(
                metadata=client.V1ObjectMeta(name=name, annotations={"fdp.floren.app/digest": digest},
                                             labels={"app.kubernetes.io/managed-by": "fdp-control-plane"}),
                string_data=data, type="Opaque"))
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def execution_exec(self, ref: str, cmd: List[str]) -> Tuple[int, str]:
        """A fixed control-plane command inside an execution's own container (e.g. a database health check)."""
        return self._exec(ref, cmd)

    def read_file(self, ref: str, path: str, limit: int) -> Optional[bytes]:
        return parse_read(*self._exec(ref, read_file_command(path, limit)))

    def list_files(self, ref: str, directory: str, name: Optional[str] = None,
                   maxdepth: Optional[int] = None) -> List[Tuple[str, int]]:
        return parse_list(*self._exec(ref, list_files_command(directory, name, maxdepth)))

    # --- layer artifacts: files on a layer's artifact volume, through its holder pod ---------

    def _holder(self, namespace: str, wait_seconds: float = 30) -> str:
        # The holder is replaced (Recreate) when the platform image changes, e.g. during an upgrade;
        # wait briefly for the new one instead of reporting files as missing.
        import time
        deadline = time.monotonic() + wait_seconds
        while True:
            pods = self.core.list_namespaced_pod(namespace, label_selector="app.kubernetes.io/component=artifacts").items
            running = [p for p in pods if p.status.phase == "Running" and not p.metadata.deletion_timestamp]
            if running:
                return f"{namespace}/{running[0].metadata.name}"
            if time.monotonic() >= deadline:
                raise RuntimeError("layer artifact storage is unavailable")
            time.sleep(2)

    def holder_exec(self, namespace: str, cmd: List[str]) -> Tuple[int, str]:
        """A fixed control-plane command next to the layer's artifacts (e.g. a read-only query)."""
        return self._exec(self._holder(namespace), cmd)

    def prepare_artifacts(self, namespace: str, subpaths: List[str]) -> None:
        dirs = [artifact_path(sub) for sub in subpaths]
        code, _ = self._exec(self._holder(namespace), ["sh", "-c", 'mkdir -p "$@" && chmod 0777 "$@"', "prepare", *dirs])
        if code != 0:
            raise RuntimeError("could not prepare layer artifact folders")

    def read_artifact(self, namespace: str, path: str, limit: int) -> Optional[bytes]:
        return parse_read(*self._exec(self._holder(namespace), read_file_command(artifact_path(path), limit)))

    def list_artifacts(self, namespace: str, directory: str) -> List[Tuple[str, int]]:
        return parse_list(*self._exec(self._holder(namespace), list_files_command(artifact_path(directory))))

    def artifact_mtime(self, namespace: str, path: str) -> float:
        code, out = self._exec(self._holder(namespace), ["stat", "-c", "%Y", artifact_path(path)])
        return float(out.strip()) if code == 0 and out.strip().isdigit() else 0.0

    def sync_data_source_egress(self, source: str, cidrs: List[str]) -> None:
        """Execution pods are default-deny egress (Helm chart); this opens one data source to its grantees."""
        name = f"fdp-ds-{source}"
        try:
            self.net.delete_namespaced_network_policy(name, self.ns)
        except ApiException as exc:
            if exc.status != 404:
                raise
        if not cidrs:
            return
        policy = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=name, labels={"fdp.floren.app/managed": "true"}),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(match_labels={f"fdp.floren.app/ds-{source}": "true"}),
                policy_types=["Egress"],
                egress=[client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=c)) for c in cidrs]
                )],
            ),
        )
        self.net.create_namespaced_network_policy(self.ns, policy)


def _managed_layer_namespaces() -> List[str]:
    from ..db import SessionLocal
    from ..layer_infrastructure import managed_namespaces
    with SessionLocal() as db:
        return managed_namespaces(db)


def artifact_path(relative: str) -> str:
    """Absolute path on the holder's volume; relative paths only, never escaping it."""
    import posixpath
    clean = posixpath.normpath("/" + relative).lstrip("/")
    if not relative or relative.startswith("/") or ".." in relative.split("/") or not clean:
        raise ValueError("invalid artifact path")
    return "/artifacts/" + clean

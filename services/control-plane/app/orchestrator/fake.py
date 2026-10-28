from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .base import ExecutionHandle, ExecutionSpec


class FakeOrchestrator:
    """In-memory orchestrator for tests."""

    def __init__(self) -> None:
        self.executions: Dict[str, ExecutionSpec] = {}
        self.states: Dict[str, str] = {}
        # Why a pending execution waits (tests set it), keyed like states.
        self.waiting: Dict[str, str] = {}
        self.egress: Dict[str, List[str]] = {}
        # Simulated per-user volumes: owner volume name -> {absolute path: content}.
        self.volumes: Dict[str, Dict[str, bytes]] = {}
        # Tests can simulate what a container does when it starts (write files, finish, ...).
        self.on_start: Optional[Callable[[ExecutionSpec], Optional[str]]] = None
        # Simulated layer artifact volumes: namespace -> {relative path: content}, plus prepared folders.
        self.artifacts: Dict[str, Dict[str, bytes]] = {}
        self.artifact_dirs: Dict[str, set] = {}
        self.artifact_times: Dict[Tuple[str, str], float] = {}
        # Commands run inside executions: (ref, command) pairs, answered by exec_result.
        self.execs: List[Tuple[str, List[str]]] = []
        # Platform-written layer Secrets: (namespace, name) -> (digest, data); namespaces not created yet.
        self.layer_secrets: Dict[Tuple[str, str], Tuple[str, Dict[str, str]]] = {}
        self.missing_namespaces: set = set()
        self.exec_result: Callable[[str, List[str]], Tuple[int, str]] = lambda ref, cmd: (0, "")

    def inventory(self) -> List[dict]:
        return [{"ref": ref, "state": self.status(ref), "labels": spec.labels} for ref, spec in self.executions.items()]

    def start(self, spec: ExecutionSpec) -> ExecutionHandle:
        self.executions[spec.name] = spec
        self.states[spec.name] = (self.on_start(spec) if self.on_start else None) or "running"
        url = f"http://{spec.name}:{spec.port}" if spec.port else None
        return ExecutionHandle(ref=spec.name, upstream_url=url)

    def stop(self, ref: str, grace_seconds: int = 0) -> None:
        ref = ref.rpartition("/")[2]  # layer refs are "namespace/name"
        self.last_grace = grace_seconds
        self.executions.pop(ref, None)
        self.states.pop(ref, None)

    def status(self, ref: str) -> str:
        return self.states.get(ref.rpartition("/")[2], "missing")

    def waiting_reason(self, ref: str) -> Optional[str]:
        return self.waiting.get(ref.rpartition("/")[2])

    def layer_secret_digest(self, namespace: str, name: str) -> str:
        return self.layer_secrets.get((namespace, name), ("", {}))[0]

    def ensure_pull_secret(self, namespace: str, name: str, config: str, digest: str) -> bool:
        if namespace in self.missing_namespaces or self.layer_secrets.get((namespace, name), ("",))[0] == digest:
            return False
        self.layer_secrets[(namespace, name)] = (digest, {".dockerconfigjson": config})
        return True

    def read_layer_secret(self, namespace: str, name: str, key: str) -> Optional[str]:
        return self.layer_secrets.get((namespace, name), ("", {}))[1].get(key)

    def write_layer_secret(self, namespace: str, name: str, data: Dict[str, str], digest: str) -> bool:
        if namespace in self.missing_namespaces:
            return False
        if data:
            self.layer_secrets[(namespace, name)] = (digest, dict(data))
        else:
            self.layer_secrets.pop((namespace, name), None)
        return True

    def execution_exec(self, ref: str, cmd: List[str]) -> Tuple[int, str]:
        self.execs.append((ref, list(cmd)))
        return self.exec_result(ref, cmd)

    def logs(self, ref: str, tail: int = 200) -> str:
        return ""

    def _files(self, ref: str) -> Dict[str, bytes]:
        spec = self.executions.get(ref)
        return self.volumes.setdefault(spec.home_volume, {}) if spec and spec.home_volume else {}

    def read_file(self, ref: str, path: str, limit: int) -> Optional[bytes]:
        data = self._files(ref).get(path)
        return None if data is None else data[:limit]

    def list_files(self, ref: str, directory: str, name: Optional[str] = None,
                   maxdepth: Optional[int] = None) -> List[Tuple[str, int]]:
        prefix = directory.rstrip("/") + "/"
        out = []
        for p, d in self._files(ref).items():
            if not p.startswith(prefix):
                continue
            rel = p[len(prefix):]
            if name and (rel.rsplit("/", 1)[-1] != name or "/.fdp/" in f"/{rel}"):
                continue
            if maxdepth is not None and rel.count("/") >= maxdepth:
                continue
            out.append((rel, len(d)))
        return sorted(out)

    def sync_data_source_egress(self, source: str, cidrs: List[str]) -> None:
        if cidrs:
            self.egress[source] = list(cidrs)
        else:
            self.egress.pop(source, None)

    def prepare_artifacts(self, namespace: str, subpaths: List[str]) -> None:
        self.artifact_dirs.setdefault(namespace, set()).update(subpaths)

    def write_artifact(self, namespace: str, path: str, data: bytes) -> None:
        """Test helper: what a layer execution would leave on its artifact volume."""
        import time
        self.artifacts.setdefault(namespace, {})[path] = data
        self.artifact_times[(namespace, path)] = time.time()

    def read_artifact(self, namespace: str, path: str, limit: int) -> Optional[bytes]:
        data = self.artifacts.get(namespace, {}).get(path)
        return None if data is None else data[:limit]

    def list_artifacts(self, namespace: str, directory: str) -> List[Tuple[str, int]]:
        prefix = directory.rstrip("/") + "/"
        return sorted((p[len(prefix):], len(d)) for p, d in self.artifacts.get(namespace, {}).items() if p.startswith(prefix))

    def artifact_mtime(self, namespace: str, path: str) -> float:
        return self.artifact_times.get((namespace, path), 0.0)

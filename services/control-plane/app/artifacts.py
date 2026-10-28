"""Where a run's or workload's files live: the shared data volume, or its layer's own volume.

Shared layers keep using the control plane's mount of the data volume. A managed layer's files
stay on that layer's artifact volume; the control plane prepares and reads them through the
layer's artifact holder pod, so no volume or network path crosses the layer boundary.
"""
import os
from typing import List, Optional, Tuple

from .config import get_settings


class SharedArtifacts:
    namespace = None

    def _path(self, relative):
        return os.path.join(get_settings().data_dir, relative)

    def prepare(self, subpaths: List[str]) -> None:
        from .executions import prepare_data_dirs
        prepare_data_dirs(get_settings(), *subpaths)

    def read(self, relative: str, limit: int) -> Optional[bytes]:
        try:
            with open(self._path(relative), "rb") as f:
                return f.read(limit)
        except OSError:
            return None

    def list(self, directory: str) -> List[Tuple[str, int]]:
        base, found = self._path(directory), []
        for root, _dirs, names in os.walk(base):
            for name in names:
                full = os.path.join(root, name)
                found.append((os.path.relpath(full, base), os.path.getsize(full)))
        return sorted(found)

    def mtime(self, relative: str) -> float:
        path = self._path(relative)
        return os.path.getmtime(path) if os.path.exists(path) else 0.0


class LayerArtifacts:
    def __init__(self, orch, namespace):
        self.orch, self.namespace = orch, namespace

    def prepare(self, subpaths: List[str]) -> None:
        if subpaths:
            self.orch.prepare_artifacts(self.namespace, list(subpaths))

    def read(self, relative: str, limit: int) -> Optional[bytes]:
        try:
            return self.orch.read_artifact(self.namespace, relative, limit)
        except (RuntimeError, ValueError):  # unavailable holder, or a path outside the volume
            return None

    def list(self, directory: str) -> List[Tuple[str, int]]:
        try:
            return self.orch.list_artifacts(self.namespace, directory)
        except (RuntimeError, ValueError):  # unavailable holder, or a path outside the volume
            return []

    def mtime(self, relative: str) -> float:
        try:
            return self.orch.artifact_mtime(self.namespace, relative)
        except (RuntimeError, ValueError):  # unavailable holder, or a path outside the volume
            return 0.0


def for_layer(db, layer_id, orch=None):
    from .layer_infrastructure import placement
    placed = placement(db, layer_id)
    if not placed:
        return SharedArtifacts()
    if orch is None:
        from .deps import get_orchestrator
        orch = get_orchestrator()
    return LayerArtifacts(orch, placed.namespace)


def workload_layer(db, pub):
    from .layers import resource_layer
    from .rbac import workload_resource
    return resource_layer(db, workload_resource(db, pub))


def for_workload(db, pub, orch=None):
    return for_layer(db, workload_layer(db, pub), orch)


def for_run(db, run, orch=None):
    return for_layer(db, run.environment_id, orch)


def place(db, spec, layer_id, orch, execution="job"):
    """Bind a job/app spec to its layer: run in the layer namespace with its own artifact volume."""
    from .layer_infrastructure import placement, require_ready
    placed = placement(db, layer_id)
    if not placed:
        if spec.mounts and not get_settings().shared_data_volume:
            from fastapi import HTTPException
            raise HTTPException(409, {"code": "ManagedLayerRequired", "message":
                "This installation runs jobs and apps only in managed layers; enable infrastructure for this layer"})
        return spec
    require_ready(db, layer_id, execution)
    data_volume = get_settings().data_volume
    for mount in spec.mounts:
        if mount.volume != data_volume:
            raise ValueError("unexpected volume for a managed layer execution")
        mount.volume = placed.artifact_claim
    LayerArtifacts(orch, placed.namespace).prepare([m.subpath for m in spec.mounts if m.subpath])
    spec.placement = placed
    return spec

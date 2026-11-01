from __future__ import annotations

from dataclasses import dataclass, field
import base64
import shlex
from typing import Dict, List, Optional, Protocol, Tuple


HOME_SUBPATH = "workspace"


def prepare_home_command(root: str) -> List[str]:
    """Initialize a private home inside a volume without hiding or moving legacy files."""
    return ["sh", "-ec", r'''
root=$1
marker="$root/.fdp-home-layout-v1"
home="$root/workspace"
if [ -L "$marker" ] || [ -L "$home" ]; then
    echo "FDP workspace storage must not use symlinks for its layout." >&2
    exit 1
fi
if [ -f "$marker" ]; then
    test -d "$home"
    exit
fi
for entry in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then continue; fi
    if [ "$entry" = "$root/lost+found" ] && [ -d "$entry" ]; then continue; fi
    echo "FDP workspace volume uses the old layout. Migrate or explicitly reset it before starting; no files were changed." >&2
    exit 1
done
mkdir -m 0700 "$home"
touch "$marker"
''', "fdp-home-init", root]


@dataclass
class VolumeMount:
    """A (sub)directory of a shared volume: Docker named volume or Kubernetes PVC."""

    volume: str
    target: str
    subpath: str = ""


class CapacityError(Exception):
    """The cluster refused an execution for its namespace's quota; safe to show (no secrets)."""


@dataclass
class Placement:
    """Kubernetes-only: run in a controller-managed layer namespace instead of the shared one."""

    namespace: str
    storage_class: Optional[str] = None
    volume_size: str = "20Gi"
    access_mode: str = "ReadWriteMany"
    service_account: str = "fdp-execution"   # created by the controller; no API token mounted
    artifact_claim: str = "fdp-artifacts"   # the layer's own artifact volume (controller-owned)
    trust_configmap: Optional[str] = None         # the layer's public CA bundle (controller-owned)


@dataclass
class ExecutionSpec:
    """A container to run: either a workspace (editor) or a run on a compute pool."""

    name: str                     # DNS-safe, unique, e.g. "ws-3f2a9c01b2de"
    image: str
    cpu: float
    memory_gb: float
    port: Optional[int] = None    # exposed HTTP port, if any (editors, apps)
    gpu: int = 0
    command: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    # Persistent per-workspace volume; only HOME_SUBPATH is mounted at /workspace.
    home_volume: Optional[str] = None
    # None = image default. RStudio's s6 init must start as root and drops to uid 1000 itself.
    run_as_user: Optional[int] = 1000
    run_as_group: Optional[int] = None
    # Extra mounts, e.g. a deployed run's own folder of the shared data volume.
    mounts: List[VolumeMount] = field(default_factory=list)
    working_dir: Optional[str] = None
    # Secret values: kept apart from env so backends can store them as secrets (Kubernetes Secret).
    secret_env: Dict[str, str] = field(default_factory=dict)
    # Managed layer placement; None keeps the backend's shared default (Docker ignores it).
    placement: Optional[Placement] = None


def read_file_command(path: str, limit: int) -> List[str]:
    # base64 keeps binary content intact over exec streams (the Kubernetes one is text).
    q = shlex.quote(path)
    return ["sh", "-c", f"test -f {q} || exit 3; head -c {int(limit)} {q} | base64 | tr -d '\\n'"]


def list_files_command(directory: str, name: Optional[str] = None, maxdepth: Optional[int] = None) -> List[str]:
    cmd = ["find", directory]
    if maxdepth is not None:
        cmd += ["-maxdepth", str(int(maxdepth))]
    # ext4 workspace volumes contain a root-only recovery directory. Descending
    # into it makes find exit nonzero, discarding even readable configs/results.
    # Prune it rather than weakening its permissions or ignoring scan failures.
    cmd += ["-path", directory.rstrip("/") + "/lost+found", "-prune", "-o"]
    if name:
        cmd += ["-name", name, "-not", "-path", "*/.fdp/*"]
    return cmd + ["-type", "f", "-printf", "%P\\t%s\\n"]


def parse_read(code: int, out: str) -> Optional[bytes]:
    return base64.b64decode(out) if code == 0 else None


def parse_list(code: int, out: str) -> List[Tuple[str, int]]:
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        path, _, size = line.rpartition("\t")
        if path:
            rows.append((path, int(size)))
    return sorted(rows)


@dataclass
class ExecutionHandle:
    ref: str                      # orchestrator-specific id
    upstream_url: Optional[str]   # where the gateway forwards traffic


class Orchestrator(Protocol):
    def execution_addresses(self, ref: str) -> List[str]:
        """Current execution IPs, for authenticated proxy source attestation. Never client supplied."""
        ...
    def inventory(self) -> List[dict]:
        """Managed execution states/labels only, without environment variables or secrets."""
        ...
    def start(self, spec: ExecutionSpec) -> ExecutionHandle: ...
    def stop(self, ref: str, grace_seconds: int = 0) -> None:
        """Remove an execution. With grace_seconds, send SIGTERM first and wait that long."""
        ...
    def status(self, ref: str) -> str:
        """One of: pending, running, succeeded, failed, missing."""
        ...
    def logs(self, ref: str, tail: int = 200) -> str: ...
    def waiting_reason(self, ref: str) -> Optional[str]:
        """Why a pending execution has not started yet, in words people can act on (e.g. no node has enough
        memory free, or its image cannot be pulled); None while it is simply starting."""
        ...
    def sync_data_source_egress(self, source: str, cidrs: List[str]) -> None:
        """Allow pods labelled fdp.floren.app/ds-<source> to reach these CIDRs (empty list = remove)."""
        ...
    def read_file(self, ref: str, path: str, limit: int) -> Optional[bytes]:
        """First `limit` bytes of an absolute path inside a running execution, or None if absent."""
        ...
    def list_files(self, ref: str, directory: str, name: Optional[str] = None,
                   maxdepth: Optional[int] = None) -> List[Tuple[str, int]]:
        """(relative path, size) of regular files under a directory inside a running execution."""
        ...

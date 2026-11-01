"""Docker Engine backend — for local development on one machine."""

from __future__ import annotations

from typing import List, Optional, Tuple

import docker
from docker.errors import NotFound, APIError
from docker.types import Mount

from ..config import Settings
from .base import (HOME_SUBPATH, ExecutionHandle, ExecutionSpec, list_files_command,
                   parse_list, parse_read, prepare_home_command, read_file_command)

_STATES = {"created": "pending", "restarting": "pending", "running": "running", "paused": "running"}


class DockerOrchestrator:
    def execution_addresses(self, ref: str) -> List[str]:
        try:
            container = self.client.containers.get(ref)
        except NotFound:
            return []
        if container.status != "running":
            return []
        return [value for network in container.attrs.get("NetworkSettings", {}).get("Networks", {}).values()
                for value in (network.get("IPAddress"), network.get("GlobalIPv6Address")) if value]

    def inventory(self) -> List[dict]:
        return [{"ref": (c.attrs.get("Names") or [c.name or c.id])[0].lstrip("/"), "state": _STATES.get(c.attrs.get("State"), "finished"),
                 "labels": c.attrs.get("Labels") or {}}
                for c in self.client.containers.list(all=True, sparse=True, filters={"label": "fdp.floren.app/managed=true"})]

    def __init__(self, settings: Settings):
        self.client = docker.from_env()
        self.network = settings.docker_network
        for network in self.client.networks.list(filters={"label": "fdp.floren.app/environment"}):
            if network.name.startswith((self.network + "-env-",self.network + "-ws-")):
                self._connect_platform(network)

    def _connect_platform(self, network):
        # Only authenticated platform entry points bridge environment networks.
        # Never connect SQL engines, warehouses or another environment's executions.
        for role in ("control-plane", "gateway", "gitea"):
            for core in self.client.containers.list(filters={"label": [
                    f"com.docker.compose.project={self.network}", f"com.docker.compose.service={role}"]}):
                network.reload()
                if core.id not in network.attrs.get("Containers", {}):
                    try:
                        network.connect(core, aliases=[role])
                    except APIError:
                        network.reload()
                        if core.id not in network.attrs.get("Containers", {}):
                            raise

    def _workload_network(self, environment, workspace=""):
        if not environment and not workspace:
            return self.network
        import re
        if environment and not re.fullmatch(r"[a-f0-9]{12}", environment):
            raise ValueError("invalid environment network identity")
        if workspace and not re.fullmatch(r"[a-f0-9]{12}",workspace):
            raise ValueError("invalid workspace network identity")
        # Other executions must not reach editors that trust gateway-authenticated headers.
        name = f"{self.network}-ws-{workspace}" if workspace else f"{self.network}-env-{environment}"
        try:
            network = self.client.networks.get(name)
        except NotFound:
            try:
                network = self.client.networks.create(name, driver="bridge", labels={"fdp.floren.app/environment": environment,
                    **({"fdp.floren.app/workspace":workspace} if workspace else {})})
            except APIError:
                network = self.client.networks.get(name)
        self._connect_platform(network)
        return name

    def start(self, spec: ExecutionSpec) -> ExecutionHandle:
        try:
            self.client.containers.get(spec.name).remove(force=True)
        except NotFound:
            pass
        mounts = []
        if spec.home_volume:
            # Mount at the image's /workspace for Docker's initial volume ownership copy-up.
            # This credential-free helper exits before the editor/job is launched.
            self.client.containers.run(
                spec.image, entrypoint=prepare_home_command("/workspace"), command=[],
                user="1000:1000", working_dir="/", network_mode="none", remove=True,
                read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges"],
                volumes={spec.home_volume: {"bind": "/workspace", "mode": "rw"}},
            )
            home = Mount(target="/workspace", source=spec.home_volume, type="volume")
            home["VolumeOptions"] = {"Subpath": HOME_SUBPATH}
            mounts.append(home)
        for m in spec.mounts:
            mount = Mount(target=m.target, source=m.volume, type="volume")
            if m.subpath:
                # Engine API >= 1.45: mount only this folder of the volume (per-run isolation).
                mount["VolumeOptions"] = {"Subpath": m.subpath}
            mounts.append(mount)
        container = self.client.containers.run(
            spec.image,
            command=spec.command,
            name=spec.name,
            detach=True,
            environment={**spec.env, **spec.secret_env},
            labels={"fdp.floren.app/managed": "true", **spec.labels},
            nano_cpus=int(spec.cpu * 1e9),
            mem_limit=f"{int(spec.memory_gb * 1024)}m",
            network=self._workload_network(spec.labels.get("fdp.floren.app/environment", ""),spec.labels.get("fdp.floren.app/workspace","")),
            mounts=mounts,
            working_dir=spec.working_dir or ("/workspace" if spec.home_volume else None),
            user=f"{spec.run_as_user or 1000}:{spec.run_as_group}" if spec.run_as_group is not None else None,
        )
        url = f"http://{spec.name}:{spec.port}" if spec.port else None
        return ExecutionHandle(ref=container.name, upstream_url=url)

    def stop(self, ref: str, grace_seconds: int = 0) -> None:
        try:
            container = self.client.containers.get(ref)
            if grace_seconds and container.status == "running":
                container.stop(timeout=grace_seconds)  # SIGTERM, then SIGKILL after the grace period
            container.remove(force=True)
        except NotFound:
            pass

    def status(self, ref: str) -> str:
        try:
            c = self.client.containers.get(ref)
        except NotFound:
            return "missing"
        if c.status == "exited":
            return "succeeded" if c.attrs["State"]["ExitCode"] == 0 else "failed"
        return _STATES.get(c.status, "pending")

    def logs(self, ref: str, tail: int = 200) -> str:
        try:
            return self.client.containers.get(ref).logs(tail=tail).decode(errors="replace")
        except NotFound:
            return ""

    def _exec(self, ref: str, cmd: List[str]) -> Tuple[int, str]:
        try:
            res = self.client.containers.get(ref).exec_run(cmd, demux=True)
        except NotFound:
            return 1, ""
        stdout = (res.output[0] or b"") if res.output else b""
        return res.exit_code, stdout.decode(errors="replace")

    def read_file(self, ref: str, path: str, limit: int) -> Optional[bytes]:
        return parse_read(*self._exec(ref, read_file_command(path, limit)))

    def list_files(self, ref: str, directory: str, name: Optional[str] = None,
                   maxdepth: Optional[int] = None) -> List[Tuple[str, int]]:
        return parse_list(*self._exec(ref, list_files_command(directory, name, maxdepth)))

    def sync_data_source_egress(self, source: str, cidrs: List[str]) -> None:
        # Plain Docker cannot enforce per-pod egress. Local development only.
        return None

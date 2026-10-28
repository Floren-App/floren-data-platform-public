from __future__ import annotations

from ..config import Settings
from .base import Orchestrator, VolumeMount, ExecutionHandle, ExecutionSpec


def make_orchestrator(settings: Settings) -> Orchestrator:
    if settings.orchestrator == "kubernetes":
        from .kubernetes import KubernetesOrchestrator

        return KubernetesOrchestrator(settings)
    if settings.orchestrator == "docker":
        from .docker import DockerOrchestrator

        return DockerOrchestrator(settings)
    from .fake import FakeOrchestrator

    return FakeOrchestrator()


__all__ = ["Orchestrator", "VolumeMount", "ExecutionHandle", "ExecutionSpec", "make_orchestrator"]

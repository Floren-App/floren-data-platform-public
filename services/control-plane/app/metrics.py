"""Prometheus metrics of the control plane (GET /internal/metrics, bearer FDP_METRICS_TOKEN).

Per replica: HTTP requests and their duration, whether the replica leads, and the Trino queries it recorded.
Platform state (workspaces, runs, warehouses, workloads, package imports, the query log, certificates) comes
from the metadata database and is reported by the leader only, so sums across replicas are not doubled.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_requests: Dict[Tuple[str, str, str], int] = defaultdict(int)
_durations: Dict[Tuple[str, str], List[float]] = defaultdict(lambda: [0.0] * (len(BUCKETS) + 2))  # buckets, +Inf count, sum


def observe(method: str, route: str, status: int, seconds: float) -> None:
    _requests[(method, route, str(status))] += 1
    hist = _durations[(method, route)]
    for i, bound in enumerate(BUCKETS):
        if seconds <= bound:
            hist[i] += 1
    hist[len(BUCKETS)] += 1
    hist[len(BUCKETS) + 1] += seconds


def _label(**labels) -> str:
    def escape(v):
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in labels.items()) + "}" if labels else ""


class Out:
    def __init__(self):
        self.lines: List[str] = []
        self.declared = set()

    def metric(self, name: str, kind: str, help_text: str, value, **labels) -> None:
        if name not in self.declared:
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
            self.declared.add(name)
        self.lines.append(f"{name}{_label(**labels)} {value}")


def render(db: Session, leader: bool) -> str:
    from .config import get_settings
    out = Out()
    out.metric("fdp_build_info", "gauge", "Version of this control plane.", 1, version=get_settings().release)
    out.metric("fdp_leader", "gauge", "1 when this replica runs the background work.", int(leader))
    for (method, route, status), count in sorted(_requests.items()):
        out.metric("fdp_http_requests_total", "counter", "HTTP requests served by this replica.", count,
                   method=method, route=route, status=status)
    for (method, route), hist in sorted(_durations.items()):
        name = "fdp_http_request_duration_seconds"
        if name not in out.declared:
            out.lines += [f"# HELP {name} Time to answer an HTTP request.", f"# TYPE {name} histogram"]
            out.declared.add(name)
        for i, bound in enumerate(BUCKETS):
            out.lines.append(f"{name}_bucket{_label(method=method, route=route, le=bound)} {hist[i]}")
        out.lines.append(f"{name}_bucket{_label(method=method, route=route, le='+Inf')} {hist[len(BUCKETS)]}")
        out.lines.append(f"{name}_count{_label(method=method, route=route)} {hist[len(BUCKETS)]}")
        out.lines.append(f"{name}_sum{_label(method=method, route=route)} {hist[len(BUCKETS) + 1]:.6f}")
    from .query_log import completed_total
    for (layer, state), count in sorted(completed_total.items()):
        out.metric("fdp_trino_queries_recorded_total", "counter", "Completed Trino queries this replica recorded.",
                   count, layer=layer, state=state)
    if leader:
        _platform(db, out)
    return "\n".join(out.lines) + "\n"


def _platform(db: Session, out: Out) -> None:
    from .models import ComputePool, PackageImport, QueryLog, Run, Workload, Workspace, utcnow
    for status, count in db.execute(select(Workspace.status, func.count()).group_by(Workspace.status)):
        out.metric("fdp_workspaces", "gauge", "Workspaces by status.", count, status=status)
    active = ("pending", "queued", "running")
    for status, kind, count in db.execute(select(Run.status, Run.kind, func.count()).where(Run.status.in_(active))
                                          .group_by(Run.status, Run.kind)):
        out.metric("fdp_runs_active", "gauge", "Runs waiting or running, by kind.", count, status=status, kind=kind)
    for status, count in db.execute(select(Workload.status, func.count()).group_by(Workload.status)):
        out.metric("fdp_workloads", "gauge", "Deployed workloads by status.", count, status=status)
    for status, count in db.execute(select(PackageImport.status, func.count()).group_by(PackageImport.status)):
        out.metric("fdp_package_imports", "gauge", "Package imports by status.", count, status=status)
    from .capacity import held, queue
    in_use = held(db)
    for pool in db.scalars(select(ComputePool)):
        used, queued = in_use.get(pool.name, 0), len(queue(db, pool.name))
        out.metric("fdp_warehouse_in_use", "gauge", "Warehouse slots in use.", used, warehouse=pool.name)
        out.metric("fdp_warehouse_queued", "gauge", "Work waiting for a warehouse slot.", queued, warehouse=pool.name)
        if pool.max_concurrent:
            out.metric("fdp_warehouse_max_concurrent", "gauge", "Warehouse slot limit.", pool.max_concurrent, warehouse=pool.name)
    since = utcnow() - timedelta(hours=1)
    for layer, state, count, wall, cpu in db.execute(
            select(QueryLog.layer_id, QueryLog.state, func.count(), func.coalesce(func.sum(QueryLog.wall_ms), 0),
                   func.coalesce(func.sum(QueryLog.cpu_ms), 0))
            .where(QueryLog.created_at >= since).group_by(QueryLog.layer_id, QueryLog.state)):
        out.metric("fdp_trino_queries_last_hour", "gauge", "Completed Trino queries in the last hour.", count, layer=layer, state=state)
        out.metric("fdp_trino_query_wall_seconds_last_hour", "gauge", "Elapsed time of those queries.", wall / 1000, layer=layer, state=state)
        out.metric("fdp_trino_query_cpu_seconds_last_hour", "gauge", "CPU time of those queries.", cpu / 1000, layer=layer, state=state)
    from . import cert_monitor
    out.lines.append(cert_monitor.metrics(cert_monitor.rows(db)).rstrip("\n"))


class Timer:
    def __init__(self):
        self.start = time.perf_counter()

    def seconds(self) -> float:
        return time.perf_counter() - self.start

"""Scales each layer's Trino workers between their minimum and maximum with query load.

Load is read from the layer's coordinator (running and waiting queries) by a reserved monitoring
identity that the access policy lets read system information and nothing else. More load adds
workers at once, as far as the worker warehouse has free slots; the minimum is always granted.
Lower load removes one worker at a time, only after it stayed low for scale_down_after_seconds; the
removed worker finishes its running queries first (its preStop drain). The decided count is stored on
the layer, and the layer controller applies it on its next pass, which this nudges.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import ssl
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple

import httpx
from sqlalchemy import select

from .models import LayerOperation, Resource, utcnow

log = logging.getLogger(__name__)

# Reserved Trino identity of the scaler. Platform principal IDs are 12 hex characters, so no person,
# service or job identity can ever be named this.
MONITOR = "fdp-monitor"
METRICS = "/metrics"  # OpenMetrics; the coordinator exports the QueryManager bean there


def monitor_read(body) -> bool:
    """Policy decision: the monitoring identity may read system information (load figures), only that."""
    payload = (body or {}).get("input") or {}
    identity = (payload.get("context") or {}).get("identity") or {}
    return identity.get("user") == MONITOR and (payload.get("action") or {}).get("operation") == "ReadSystemInformation"


def _verify():
    bundle = os.environ.get("SSL_CERT_FILE", "")
    return ssl.create_default_context(cafile=bundle) if bundle else True


def query_load(layer_id: str, tls: bool) -> Tuple[int, int]:
    """(running, queued) queries on the layer's coordinator."""
    from .config import get_settings
    from .layer_infrastructure import namespace_for
    from .sql_access import layer_engine_token
    url = "%s://fdp-trino.%s.svc:%s" % ("https" if tls else "http",
                                        namespace_for(layer_id, get_settings().layer_namespace_prefix), 8443 if tls else 8080)
    response = httpx.get(url + METRICS, verify=_verify() if tls else False, timeout=10,
                         headers={"Authorization": "Bearer " + layer_engine_token(MONITOR, layer_id), "X-Trino-User": MONITOR})
    response.raise_for_status()
    return parse_load(response.text)


def parse_load(text: str) -> Tuple[int, int]:
    """(running, queued) from OpenMetrics lines such as trino_execution_name_QueryManager_RunningQueries 2.0"""
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(" ")
        for metric in ("RunningQueries", "QueuedQueries"):
            if not line.startswith("#") and "QueryManager" in name and name.split("{")[0].endswith("_" + metric):
                values[metric] = int(float(rest.split()[0]))
    if "RunningQueries" not in values:
        raise ValueError("the coordinator did not report its queries")
    return values["RunningQueries"], values.get("QueuedQueries", 0)


def decide(current: int, running: int, queued: int, workers, room: int, low_since: Optional[datetime],
           now: datetime) -> Tuple[int, Optional[datetime]]:
    """The worker count to run now, and since when load has been low (None when it is not)."""
    current = min(workers.max, max(workers.min, current))
    wanted = min(workers.max, max(workers.min, math.ceil((running + queued) / workers.queries_per_worker)))
    if wanted > current:
        return current + min(wanted - current, max(room, 0)), None
    if wanted == current:
        return current, None
    if low_since is None:
        return current, now
    if now - low_since >= timedelta(seconds=workers.scale_down_after_seconds):
        return current - 1, now  # one at a time: the next removal waits a full period again
    return current, low_since


def scale_once(db, load: Callable[[str, bool], Tuple[int, int]] = query_load, now: Optional[datetime] = None) -> list:
    """One pass over the managed, ready layers; returns (layer id, old, new) for every change."""
    from . import capacity
    from .layer_infrastructure import InfrastructureSpec, engine_ready
    from .models import ComputePool
    now = now or utcnow()
    changes = []
    for layer_id in db.scalars(select(Resource.id).where(Resource.kind == "layer")).all():
        layer = db.scalar(select(Resource).where(Resource.id == layer_id).with_for_update()
                          .execution_options(populate_existing=True))
        config = layer.config or {}
        if not config.get("infrastructure", {}).get("enabled") or not engine_ready(db, layer):
            db.rollback()
            continue
        spec = InfrastructureSpec.model_validate(config["infrastructure"])
        workers = spec.trino.workers
        if spec.retired or not workers.warehouse:
            db.rollback()
            continue
        state = dict(config.get("trino_scale") or {})
        current = min(workers.max, max(workers.min, config.get("trino_workers") or workers.min))
        try:
            running, queued = load(layer.id, spec.trino.tls.mode != "disabled")
        except Exception as exc:  # unknown load: keep what runs, say why
            log.warning("Trino load of layer %s unavailable: %s", layer.id, type(exc).__name__)
            layer.config = {**config, "trino_workers": current,
                            "trino_scale": {**state, "error": "load unavailable", "checked_at": now.isoformat()}}
            db.commit()
            continue
        pool = db.scalar(select(ComputePool).where(ComputePool.name == workers.warehouse))
        room = pool.max_concurrent - capacity.held(db).get(pool.name, 0) if pool else 0
        low_since = datetime.fromisoformat(state["low_since"]) if state.get("low_since") else None
        target, low_since = decide(current, running, queued, workers, room, low_since, now)
        wanted = min(workers.max, max(workers.min, math.ceil((running + queued) / workers.queries_per_worker)))
        layer.config = {**config, "trino_workers": target, "trino_scale": {
            "running_queries": running, "queued_queries": queued, "checked_at": now.isoformat(),
            "low_since": low_since.isoformat() if low_since else None,
            # Wanted more workers than the warehouse had slots for.
            "waiting_for_warehouse": workers.warehouse if wanted > target else None}}
        if target != (config.get("trino_workers") or workers.min):
            changes.append((layer.id, current, target))
            operation = db.scalar(select(LayerOperation).where(
                LayerOperation.layer_id == layer.id, LayerOperation.generation == config.get("infrastructure_generation")))
            if operation is not None:
                operation.next_attempt_at = now  # the controller applies the new count on its next pass
        db.commit()
    return changes


async def scale_loop(session_factory, interval_seconds: int = 15) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            with session_factory() as db:
                for layer_id, old, new in await asyncio.to_thread(scale_once, db):
                    log.info("layer %s Trino workers %d -> %d", layer_id, old, new)
        except Exception:
            log.exception("Trino scaling pass failed")

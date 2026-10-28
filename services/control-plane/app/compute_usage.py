"""Pool occupancy includes every active component, regardless of who may view its logs."""
import logging


def inventory(orch):
    try:
        return orch.inventory()
    except Exception:
        logging.getLogger(__name__).exception("compute inventory unavailable")
        return None


def pool_usage(items, pool):
    if items is None:
        return {"usage_available": False, "active_runs": None, "active_services": None,
                "active_executions": None, "active_products": None}
    active = [w for w in items if w["state"] in {"pending", "running"} and w["labels"].get("fdp.floren.app/pool") == pool]
    services = [w for w in active if w["labels"].get("fdp.floren.app/role") == "workload"]
    return {"usage_available": True, "active_runs": len(active) - len(services), "active_services": len(services),
            "active_executions": len(active), "active_products": len({w["labels"].get("fdp.floren.app/workload") for w in services})}

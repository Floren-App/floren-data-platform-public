"""The installation's readiness, as evidence: what is configured, what is protected, what someone else operates, and
what is unknown. Read-only: it enables, schedules and provisions nothing, and claims no protection it cannot see.

States: configured, not_configured, attention (configured but needs action), single_instance (a bundled service:
not highly available), customer_operated (outside Limoon: its protection is the operator's), unknown.
"""
from sqlalchemy import func, select

from .config import get_settings
from .models import AppProfile, ComputePool, PermissionGrant, PlatformBackup, Run, Workspace, utcnow

# What Limoon does not back up; each needs the operator's own arrangement (see platform-recovery).
EXTERNAL = ("Workspace volumes", "Uploaded packages and artifacts", "Kafka messages", "Git repositories",
            "Terraform state")


def report(db) -> dict:
    settings = get_settings()
    bundled = {s for s in settings.bundled_services.split(",") if s}
    items = []

    def add(area, item, state, detail=""):
        items.append({"area": area, "item": item, "state": state, "detail": detail})

    # Deployed revisions.
    from . import schema_migrations
    from .db import engine
    with engine.connect() as connection:
        current, head = schema_migrations.current(connection), schema_migrations.head()
    add("Release", "Control plane", "configured", f"Release {settings.release}")
    add("Release", "Metadata schema", "configured" if current == head else "attention",
        f"At {current}" + ("" if current == head else f"; the release expects {head}: run the migration"))

    # Services the platform depends on.
    add("Dependencies", "Metadata database", "single_instance" if "metadata-postgresql" in bundled else "customer_operated",
        "Bundled PostgreSQL: one instance, not highly available" if "metadata-postgresql" in bundled
        else "Your PostgreSQL: its availability and backups are yours")
    add("Dependencies", "Identity provider", "single_instance" if "keycloak" in bundled else "customer_operated",
        ("Bundled Keycloak: one instance" if "keycloak" in bundled else f"Your OIDC provider at {settings.oidc_issuer}")
        + f"; directory from {'SCIM' if settings.directory_mode == 'scim' else 'sign-in claims'}")
    vault = settings.secrets_backend
    add("Dependencies", "Secret store", "single_instance" if "openbao" in bundled else "customer_operated",
        f"{vault}: " + ("bundled OpenBao, one instance" if "openbao" in bundled else "its availability, backup and recovery are yours"))

    # Layers and capacity: what is requested is not the same as what is ready.
    from .layers import all_layers, default_layer
    from .capacity import default_warehouse
    layers = all_layers(db)
    if not layers:
        add("Layers", "Layers", "not_configured", "No layers yet; work has nowhere to run")
    for layer in layers:
        infra = (layer.config or {}).get("infrastructure") or {}
        managed = bool(infra.get("enabled")) and not infra.get("retired")
        add("Layers", layer.name, "configured" if managed else "attention" if layer.config.get("production") else "configured",
            ("Managed: own namespace and network policies" if managed else "Unmanaged: an authorization boundary only")
            + ("; production" if layer.config.get("production") else "")
            + ("" if default_warehouse(db, layer.id) else "; no default warehouse"))
    add("Layers", "Default layer", "configured" if default_layer(db) else "not_configured")
    from .layer_infrastructure import trino_workers
    for layer, warehouse, count in trino_workers(db):
        add("Capacity", f"Trino workers in {layer.name}", "unknown",
            f"{count} requested on {warehouse}; readiness is reported by the cluster, not confirmed here")
    pools = db.scalar(select(func.count()).select_from(ComputePool)) or 0
    add("Capacity", "Warehouses", "configured" if pools else "not_configured", f"{pools} defined")
    queued_runs = db.scalar(select(func.count()).select_from(Run).where(Run.status == "queued")) or 0
    queued_ws = db.scalar(select(func.count()).select_from(Workspace).where(Workspace.status == "queued")) or 0
    add("Capacity", "Waiting work", "attention" if queued_runs or queued_ws else "configured",
        f"{queued_runs} runs and {queued_ws} workspaces waiting for room")

    # Persona policies: reusable workflow grants exist, or every workspace would need repairs.
    for namespace, label in (("workspaces", "Workspace use"), ("runs", "Starting runs"), ("logs", "Reading logs"), ("artifacts", "Reading outputs")):
        grants = db.scalar(select(func.count()).select_from(PermissionGrant).where(PermissionGrant.namespace == namespace)) or 0
        add("Persona policies", label, "configured" if grants else "not_configured",
            f"{grants} reusable grants" if grants else "No group has this yet")
    profiles = db.scalar(select(func.count()).select_from(AppProfile)) or 0
    add("Persona policies", "App profiles", "configured" if profiles else "not_configured", f"{profiles} defined")

    # Backup and recovery: only what is configured and evidenced.
    from . import backup_plans as plans
    rows = plans.rows(db, plans.PLAN)
    enabled = [r for r in rows if r.value.get("enabled")]
    if not rows:
        add("Recovery", "Backup plans", "not_configured", "No plan: nothing is backed up by Limoon")
    else:
        last = db.scalar(select(func.max(PlatformBackup.finished_at)).where(PlatformBackup.status == "succeeded"))
        add("Recovery", "Backup plans", "configured" if enabled else "attention",
            f"{len(enabled)} of {len(rows)} enabled; " + (f"last completed {last.isoformat()}" if last else "none completed yet"))
    add("Recovery", "Restore drill", "unknown",
        "No drill evidence is recorded here; rehearse a restore in an isolated environment and keep its RPO/RTO results")
    for name in EXTERNAL:
        add("Recovery", name, "customer_operated", "Not in Limoon's backups: needs its own arrangement")

    return {"generated_at": utcnow().isoformat(), "release": settings.release, "items": items,
            "summary": {state: sum(i["state"] == state for i in items) for state in
                        ("configured", "attention", "not_configured", "single_instance", "customer_operated", "unknown")}}

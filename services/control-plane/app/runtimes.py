"""The runtime catalog: which Python, R, dbt and Airflow versions users may choose, and their images."""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import RuntimeImage

KINDS = ("python", "r", "dbt", "airflow")
LABEL = {"python": "Python", "r": "R", "dbt": "dbt", "airflow": "Airflow"}
# The fdp.json key that picks a version, per runtime.
CONFIG_KEY = {"python": "python", "r": "r", "dbt": "dbt_version", "airflow": "airflow"}


class RuntimeUnavailable(Exception):
    pass


INSTALLATION = "installation"


R_OFF = "R is not part of this installation (Helm value r.enabled); ask your administrators to add it"


def r_enabled() -> bool:
    from .config import get_settings
    return get_settings().r_enabled


def require_kind(kind: str) -> None:
    """Refuses R everywhere when the installation leaves it out."""
    if kind == "r" and not r_enabled():
        from fastapi import HTTPException
        raise HTTPException(422, R_OFF)


def installation_runtimes(s: Settings) -> List[tuple]:
    """(kind, version, image) of the versions the installation ships, from the Helm values."""
    shipped = [("python", s.default_python_version, s.image_compute_python), ("r", s.default_r_version, s.image_compute_r),
               ("dbt", s.default_dbt_version, s.image_compute_dbt), ("airflow", s.default_airflow_version, s.image_airflow)]
    return [row for row in shipped if row[0] != "r" or s.r_enabled]


def sync_installation_runtimes(db: Session, s: Settings) -> None:
    """At every start: the installation's versions use the images in the Helm values (so upgrades reach runs).
    Versions added by administrators or OpenTofu are never touched. A version Helm no longer ships stays
    available as an ordinary one; the default moves to its successor only if it was the default."""
    current = {(kind, version) for kind, version, _ in installation_runtimes(s)}
    for kind, version, image in installation_runtimes(s):
        row = db.scalar(select(RuntimeImage).where(RuntimeImage.kind == kind, RuntimeImage.version == version))
        if row is None:
            previous = db.scalar(select(RuntimeImage).where(RuntimeImage.kind == kind, RuntimeImage.is_default.is_(True)))
            take_default = previous is None or previous.managed_by == INSTALLATION
            if previous is not None and take_default:
                previous.is_default = False
            row = RuntimeImage(kind=kind, version=version, is_default=take_default)
            db.add(row)
        row.image, row.managed_by = image, INSTALLATION
    for row in db.scalars(select(RuntimeImage).where(RuntimeImage.managed_by == INSTALLATION)):
        if (row.kind, row.version) not in current:
            row.managed_by = ""
    db.flush()


def offered(db: Session, kind: str) -> List[RuntimeImage]:
    if kind == "r" and not r_enabled():
        return []
    rows = db.scalars(select(RuntimeImage).where(RuntimeImage.kind == kind, RuntimeImage.enabled.is_(True)))
    return sorted(rows, key=lambda r: [int(p) if p.isdigit() else p for p in r.version.replace("-", ".").split(".")],
                  reverse=True)


def resolve(db: Session, kind: str, version: Optional[str]) -> RuntimeImage:
    """The image for a requested version, or the default when none is requested."""
    if kind == "r" and not r_enabled():
        raise RuntimeUnavailable(R_OFF)
    options = offered(db, kind)
    if not options:
        raise RuntimeUnavailable(f"no {LABEL.get(kind, kind)} runtime is configured; ask an admin")
    if version is None:
        return next((o for o in options if o.is_default), options[0])
    for o in options:
        if o.version == version:
            return o
    choices = ", ".join(o.version for o in options)
    raise RuntimeUnavailable(
        f"{LABEL.get(kind, kind)} {version} is not offered here; set \"{CONFIG_KEY[kind]}\" in fdp.json "
        f"to one of: {choices}")

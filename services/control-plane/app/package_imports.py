"""Imports from PyPI and CRAN, on request of a Publisher: the package and its missing dependencies.

Python: `pip download` of built packages (wheels) only, for every Python version and CPU type the platform
runs, so no package code runs during an import. A package that only exists as source is reported; build a
wheel and publish it instead. R: source tarballs from CRAN (the current version, or a given version from
CRAN's archive) and their Depends/Imports/LinkingTo, resolved from CRAN's PACKAGES; packages that come with R
(base and recommended) are skipped. Imports run one at a time in the control plane, which alone talks to
the upstream repositories.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import packages as repo
from .config import get_settings
from .models import PackageImport, RuntimeImage, utcnow

log = logging.getLogger(__name__)
R_SPEC = re.compile(r"([A-Za-z][A-Za-z0-9.]*[A-Za-z0-9])\s*(?:(?:==|@)\s*([0-9]+[.-][0-9]+(?:[.-][0-9]+)*))?")
STALE = timedelta(minutes=30)
PIP_TIMEOUT = 900
PASS_ENV = ("PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT")


class ImportFailed(Exception):
    pass


def validate(kind: str, requirement: str) -> str:
    """The normalized request, or a 422 PackageError."""
    requirement = requirement.strip()
    if kind == "python":
        try:
            req = Requirement(requirement)
        except InvalidRequirement as exc:
            raise repo.PackageError(422, f"not a valid requirement: {exc}") from None
        if req.url:
            raise repo.PackageError(422, "import packages by name and version from PyPI, not from a URL")
        return str(req)
    if kind == "r":
        m = R_SPEC.fullmatch(requirement)
        if not m:
            raise repo.PackageError(422, "use a CRAN package name, optionally with a version: dplyr, or dplyr==1.1.4")
        if m.group(1) in repo.R_BASE:
            raise repo.PackageError(422, f"{m.group(1)} comes with R")
        return m.group(1) + (f"=={m.group(2)}" if m.group(2) else "")
    raise repo.PackageError(422, "kind must be python or r")


# --- upstream lookups -------------------------------------------------------------------------------------

_cran_index: Tuple[float, Dict[str, Dict[str, str]]] = (0.0, {})


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True)


def cran_index(max_age: float = 3600) -> Dict[str, Dict[str, str]]:
    global _cran_index
    if time.time() - _cran_index[0] < max_age and _cran_index[1]:
        return _cran_index[1]
    with _client() as client:
        r = client.get(f"{get_settings().package_upstream_cran.rstrip('/')}/src/contrib/PACKAGES")
        r.raise_for_status()
    index = {}
    for stanza in re.split(r"\n\s*\n", r.text):
        fields = repo.parse_dcf(stanza)
        if fields.get("Package"):
            index[fields["Package"]] = fields
    _cran_index = (time.time(), index)
    return index


def upstream_has(kind: str, name: str) -> bool:
    """Whether PyPI/CRAN has a package of this name. Unreachable upstream: False (nothing to shadow there)."""
    s = get_settings()
    try:
        if kind == "python":
            if not s.package_upstream_pypi:
                return False
            with _client() as client:
                r = client.get(f"{s.package_upstream_pypi.rstrip('/')}/{canonicalize_name(name)}/",
                               headers={"Accept": "application/vnd.pypi.simple.v1+json"})
            return r.status_code == 200
        if not s.package_upstream_cran:
            return False
        if name in cran_index():
            return True
        with _client() as client:
            return client.get(f"{s.package_upstream_cran.rstrip('/')}/src/contrib/Archive/{name}/").status_code == 200
    except (httpx.HTTPError, OSError):
        return False


# --- targets --------------------------------------------------------------------------------------------

def python_versions(db: Session) -> List[str]:
    s = get_settings()
    found = {v.strip() for v in s.package_python_versions.split(",") if v.strip()}
    for image in db.scalars(select(RuntimeImage).where(RuntimeImage.kind == "python", RuntimeImage.enabled.is_(True))):
        m = re.match(r"(3\.\d+)", image.version)
        if m:
            found.add(m.group(1))
    return sorted((v for v in found if re.fullmatch(r"3\.\d+", v)), key=lambda v: int(v.split(".")[1]))


def r_versions(db: Session) -> List[str]:
    found = {get_settings().default_r_version}
    found.update(i.version for i in db.scalars(select(RuntimeImage).where(RuntimeImage.kind == "r", RuntimeImage.enabled.is_(True))))
    return sorted(v for v in found if v)


def platform_tags(arch: str, glibc: str) -> List[str]:
    """pip does not widen --platform: list every manylinux tag a glibc up to `glibc` runs."""
    top = int(glibc.split(".")[1])
    first = 5 if arch in ("x86_64", "i686") else 17
    tags = [f"manylinux_2_{minor}_{arch}" for minor in range(top, first - 1, -1)]
    tags.append(f"manylinux2014_{arch}")
    if arch in ("x86_64", "i686"):
        tags += [f"manylinux2010_{arch}", f"manylinux1_{arch}"]
    return tags


# --- running imports --------------------------------------------------------------------------------------

def request(db: Session, user, kind: str, requirement: str) -> PackageImport:
    from . import rbac
    rbac.require(db, user, "import", rbac.packages_root(db))
    s = get_settings()
    if not (s.package_upstream_pypi if kind == "python" else s.package_upstream_cran):
        raise repo.PackageError(409, f"importing from {'PyPI' if kind == 'python' else 'CRAN'} is turned off in this installation")
    normalized = validate(kind, requirement)
    from . import package_allowlist
    name = Requirement(normalized).name if kind == "python" else R_SPEC.fullmatch(normalized).group(1)
    if not package_allowlist.permits(db, kind, name):
        raise repo.PackageError(403, f"{name} is not on the package allowlist; ask an administrator to add it "
                                     "(Packages → Allowed packages, or fdp_package_allowlist)")
    job = PackageImport(kind=kind, requirement=normalized, requested_by=user.username)
    db.add(job)
    db.flush()
    return job


def claim(session_factory) -> Optional[str]:
    with session_factory() as db:
        db.execute(update(PackageImport).where(PackageImport.status == "running", PackageImport.started_at < utcnow() - STALE)
                   .values(status="queued", started_at=None))
        job = db.scalar(select(PackageImport).where(PackageImport.status == "queued").order_by(PackageImport.created_at).limit(1))
        if job is None:
            db.commit()
            return None
        taken = db.execute(update(PackageImport).where(PackageImport.id == job.id, PackageImport.status == "queued")
                           .values(status="running", started_at=utcnow()))
        db.commit()
        return job.id if taken.rowcount == 1 else None


def run(session_factory, job_id: str) -> None:
    lines: List[str] = []
    with session_factory() as db:
        job = db.get(PackageImport, job_id)
        work = repo.root_dir() / ".imports" / job.id
        try:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            added = (import_python if job.kind == "python" else import_cran)(db, job, work, lines)
            job.status, job.added = "done", added
            lines.append(f"Added {len(added)} file(s)." if added else "Nothing new: everything was already in the repository.")
        except (ImportFailed, repo.PackageError) as exc:
            db.rollback()
            job = db.get(PackageImport, job_id)
            job.status = "failed"
            lines.append(str(exc))
        except Exception:
            log.exception("package import %s failed", job_id)
            db.rollback()
            job = db.get(PackageImport, job_id)
            job.status = "failed"
            lines.append("The import failed unexpectedly; see the control plane log.")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        job.log, job.finished_at = "\n".join(lines)[-20000:], utcnow()
        from .routes.common import audit
        audit(db, job.requested_by, "package.import", f"{job.kind}:{job.requirement}", status=job.status, files=len(job.added or []))
        db.commit()


async def import_loop(session_factory, interval: float = 3.0) -> None:
    while True:
        try:
            job_id = await asyncio.to_thread(claim, session_factory)
            if job_id:
                await asyncio.to_thread(run, session_factory, job_id)
                continue
        except Exception:
            log.exception("package import loop")
        await asyncio.sleep(interval)


def _received(path: Path) -> repo.Received:
    import hashlib
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
    return repo.Received(path, sha.hexdigest(), md5.hexdigest(), path.stat().st_size)


def pip_download(requirement: str, version: str, arch: str, out: Path, home: Path) -> subprocess.CompletedProcess:
    s = get_settings()
    command = [sys.executable, "-m", "pip", "download", "--no-cache-dir", "--disable-pip-version-check", "--no-input",
               "--only-binary=:all:", "--index-url", s.package_upstream_pypi,
               "--python-version", version, "--implementation", "cp", "--abi", "cp" + version.replace(".", ""),
               *[arg for tag in platform_tags(arch, s.package_glibc) for arg in ("--platform", tag)],
               "--dest", str(out), requirement]
    if s.package_upstream_pypi.startswith("http://"):   # an internal mirror without TLS: pip ignores it otherwise
        from urllib.parse import urlsplit
        command[3:3] = ["--trusted-host", urlsplit(s.package_upstream_pypi).netloc]
    env = {k: v for k, v in os.environ.items() if k in PASS_ENV}
    env.update(HOME=str(home), PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1")
    return subprocess.run(command, capture_output=True, text=True, timeout=PIP_TIMEOUT, env=env)


def import_python(db: Session, job: PackageImport, work: Path, lines: List[str]) -> List[str]:
    out = work / "files"
    arches = [a.strip() for a in get_settings().package_platforms.split(",") if a.strip()]
    succeeded = 0
    for version in python_versions(db):
        for arch in arches:
            try:
                done = pip_download(job.requirement, version, arch, out, work)
            except subprocess.TimeoutExpired:
                lines.append(f"Python {version} · {arch}: timed out")
                continue
            if done.returncode == 0:
                succeeded += 1
                lines.append(f"Python {version} · {arch}: resolved")
            else:
                reason = [l for l in done.stderr.splitlines() if l.startswith("ERROR")][-2:] or done.stderr.splitlines()[-2:]
                lines.append(f"Python {version} · {arch}: not available as a built package ({' '.join(reason)[:400]})")
    if not succeeded:
        raise ImportFailed("No built package (wheel) matches the platform's Python versions and CPU types. "
                           "If the package only exists as source, build a wheel and publish it.")
    files = sorted(p for p in out.iterdir() if p.is_file()) if out.exists() else []
    names = {}
    for path in files:
        name = parse_wheel_filename(path.name)[0] if path.name.endswith(".whl") else parse_sdist_filename(path.name)[0]
        names[path] = canonicalize_name(str(name))
    clash = sorted({n for n in names.values() if (p := repo.find(db, "python", n)) is not None and p.origin == "internal"})
    if clash:
        raise ImportFailed(f"{', '.join(clash)} {'is a package' if len(clash) == 1 else 'are packages'} published here; "
                           "the upstream package of the same name is not imported")
    added = []
    for path in files:
        row, created = repo.store_imported(db, "python", "pypi", path.name, _received(path), job.requested_by)
        db.commit()
        if created:
            added.append(row.filename)
    return added


def _download(client: httpx.Client, url: str, target: Path) -> bool:
    with client.stream("GET", url) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    return True


def import_cran(db: Session, job: PackageImport, work: Path, lines: List[str]) -> List[str]:
    cran = get_settings().package_upstream_cran.rstrip("/")
    m = R_SPEC.fullmatch(job.requirement)
    try:
        index = cran_index(max_age=0)
    except (httpx.HTTPError, OSError) as exc:
        raise ImportFailed(f"CRAN could not be reached ({type(exc).__name__})") from None
    targets = r_versions(db)
    queue = deque([(m.group(1), m.group(2), "")])
    seen, added = set(), []
    with _client() as client:
        while queue:
            name, version, constraint = queue.popleft()
            if name in repo.R_BASE or name in seen:
                continue
            seen.add(name)
            existing = repo.find(db, "r", name.lower())
            if existing is not None and existing.origin == "internal":
                raise ImportFailed(f"{name} is a package published here; the CRAN package of that name is not imported")
            have = [v for v, files in repo.versions(db, existing) if not all(f.yanked for f in files)] if existing else []
            entry = index.get(name)
            if version is None:
                if any(repo.r_satisfies(v, constraint) for v in have) and name != m.group(1):
                    continue
                if entry is None:
                    raise ImportFailed(f"{name} is not on CRAN" + (f" (needed by {m.group(1)})" if name != m.group(1) else ""))
                if entry.get("Priority") == "recommended" and name != m.group(1):
                    lines.append(f"{name}: comes with R (recommended package), skipped")
                    continue
                version = entry["Version"]
                if not repo.r_satisfies(version, constraint):
                    raise ImportFailed(f"{name} {constraint} is needed, but CRAN has {version}; import a fitting version first")
            if version in have:
                lines.append(f"{name} {version}: already in the repository")
                continue
            filename = f"{name}_{version}.tar.gz"
            target = work / filename
            current = entry is not None and entry.get("Version") == version
            urls = ([f"{cran}/src/contrib/{filename}"] if current else []) + [f"{cran}/src/contrib/Archive/{name}/{filename}"]
            try:
                if not any(_download(client, url, target) for url in urls):
                    raise ImportFailed(f"CRAN has no {name} {version}")
            except httpx.HTTPError as exc:
                raise ImportFailed(f"downloading {filename} failed ({type(exc).__name__})") from None
            row, created = repo.store_imported(db, "r", "cran", filename, _received(target), job.requested_by)
            db.commit()
            lines.append(f"{name} {version}: imported")
            if created:
                added.append(row.filename)
            needs_r = next((r for r in row.requires if r.startswith("R (")), None)
            if needs_r and targets and not any(repo.r_satisfies(t, needs_r[3:-1]) for t in targets):
                lines.append(f"  warning: {name} {version} needs {needs_r}; the platform runs R {', '.join(targets)}")
            for dep in row.requires:
                dm = re.match(r"([A-Za-z][A-Za-z0-9.]*)(?: \((.*)\))?$", dep)
                if dm and dm.group(1) != "R":
                    queue.append((dm.group(1), None, dm.group(2) or ""))
    return added

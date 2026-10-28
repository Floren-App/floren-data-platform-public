"""The platform's package repository: one repository with Python and R packages, every version kept.

Python is served as a simple index (PEP 503 HTML and PEP 691 JSON) that pip and uv install from; R as a
CRAN-style repository (src/contrib/PACKAGES with the newest version of each package, every version under
src/contrib/Archive/<package>/, the layout renv uses for "pkg@version"). Workspaces, runs and deployed programs
install only from here: they have no route to PyPI or CRAN.

Everyone may read. Publishing is RBAC on the "packages" resource: Publishers publish new packages (their
group becomes the package's Owner) and import packages from PyPI/CRAN; a package's Owner and Maintainers
publish its new versions. Published files are never overwritten; a version can be yanked. A package name
is either internal or imported, never both, so a published package cannot shadow an upstream one.
"""
from __future__ import annotations

import email.parser
import gzip
import hashlib
import html
import os
import re
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from packaging.utils import (InvalidSdistFilename, InvalidWheelFilename, canonicalize_name,
                             parse_sdist_filename, parse_wheel_filename)
from packaging.version import InvalidVersion, Version
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import rbac
from .auth import User
from .config import get_settings
from .models import Package, PackageFile, Resource

KINDS = ("python", "r")
ORIGIN_LABEL = {"internal": "published here", "pypi": "PyPI", "cran": "CRAN"}
# R packages that come with R itself; never imported or published.
R_BASE = {"R", "base", "compiler", "datasets", "grDevices", "graphics", "grid", "methods", "parallel", "splines",
          "stats", "stats4", "tcltk", "tools", "utils"}
# DESCRIPTION fields repeated in PACKAGES (what install.packages needs to resolve dependencies).
R_FIELDS = ("Package", "Version", "Priority", "Depends", "Imports", "LinkingTo", "Suggests", "Enhances",
            "License", "License_is_FOSS", "License_restricts_use", "OS_type", "Archs", "MD5sum", "NeedsCompilation")
R_FILENAME = re.compile(r"([A-Za-z][A-Za-z0-9.]*[A-Za-z0-9])_([0-9]+[.-][0-9]+(?:[.-][0-9]+)*)\.tar\.gz")
MAX_METADATA = 1 << 20


class PackageError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Parsed:
    kind: str
    name: str
    key: str
    version: str
    summary: str = ""
    requires_python: Optional[str] = None
    requires: List[str] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)


def key_of(kind: str, name: str) -> str:
    return canonicalize_name(name) if kind == "python" else name.lower()


def root_dir() -> Path:
    return Path(get_settings().package_dir)


def file_path(kind: str, key: str, filename: str) -> Path:
    if "/" in filename or "\\" in filename or filename.startswith(".") or "/" in key or key.startswith("."):
        raise PackageError(422, "invalid file name")
    return root_dir() / kind / key / filename


# --- reading package metadata (never extracting or running anything) ------------------------------------

def _read_member(read, limit: int = MAX_METADATA) -> str:
    data = read()
    if len(data) > limit:
        raise PackageError(422, "package metadata is too large")
    return data.decode("utf-8", errors="replace")


def parse_python(filename: str, path: Path) -> Parsed:
    try:
        if filename.endswith(".whl"):
            name, version, _, _ = parse_wheel_filename(filename)
            with zipfile.ZipFile(path) as z:
                wanted = [n for n in z.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
                if len(wanted) != 1:
                    raise PackageError(422, "the wheel has no .dist-info/METADATA")
                text = _read_member(lambda: z.read(wanted[0]))
        elif filename.endswith((".tar.gz", ".zip")):
            name, version = parse_sdist_filename(filename)
            text = _sdist_pkg_info(filename, path)
        else:
            raise PackageError(422, "Python packages are wheels (.whl) or source distributions (.tar.gz, .zip)")
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion) as exc:
        raise PackageError(422, f"not a valid package file name: {exc}") from None
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError):
        raise PackageError(422, "the file is not a readable package archive") from None
    meta = email.parser.HeaderParser().parsestr(text)
    if not meta.get("Name") or canonicalize_name(meta["Name"]) != canonicalize_name(str(name)):
        raise PackageError(422, "the package metadata names a different package than the file name")
    try:
        if Version(meta.get("Version", "")) != version:
            raise PackageError(422, "the package metadata has a different version than the file name")
    except InvalidVersion:
        raise PackageError(422, "the package metadata has no valid version") from None
    return Parsed("python", meta["Name"], canonicalize_name(meta["Name"]), str(version),
                  summary=(meta.get("Summary") or "")[:500], requires_python=meta.get("Requires-Python") or None,
                  requires=[r for r in meta.get_all("Requires-Dist") or []][:500])


def _sdist_pkg_info(filename: str, path: Path) -> str:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            wanted = [n for n in z.namelist() if n.count("/") == 1 and n.endswith("/PKG-INFO")]
            if not wanted:
                raise PackageError(422, "the source distribution has no PKG-INFO")
            return _read_member(lambda: z.read(wanted[0]))
    with tarfile.open(path, "r:gz") as t:
        for member in t:
            if member.isfile() and member.name.count("/") == 1 and member.name.endswith("/PKG-INFO"):
                return _read_member(lambda: t.extractfile(member).read(MAX_METADATA + 1))
    raise PackageError(422, "the source distribution has no PKG-INFO")


def parse_dcf(text: str) -> Dict[str, str]:
    """Debian control format, as in DESCRIPTION and PACKAGES (one stanza)."""
    fields: Dict[str, str] = {}
    last = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and last:
            fields[last] += " " + line.strip()
        elif ":" in line:
            last, value = line.split(":", 1)
            last = last.strip()
            fields[last] = value.strip()
    return fields


def r_dependencies(fields: Dict[str, str], which: Iterable[str] = ("Depends", "Imports", "LinkingTo")) -> List[Tuple[str, str]]:
    """(name, constraint) pairs, e.g. ("dplyr", ">= 1.1.0"); R itself and base packages excluded."""
    result = []
    for key in which:
        for part in (fields.get(key) or "").split(","):
            m = re.match(r"\s*([A-Za-z][A-Za-z0-9.]*)\s*(?:\(\s*([^)]*)\))?\s*$", part)
            if m and m.group(1) not in R_BASE:
                result.append((m.group(1), (m.group(2) or "").strip()))
    return result


def r_version(text: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.split(r"[.-]", text.strip()) if p.isdigit())


def r_satisfies(version: str, constraint: str) -> bool:
    m = re.match(r"(>=|<=|==|>|<|!=)\s*([0-9][0-9.-]*)$", constraint.strip())
    if not m:
        return True
    a, b = r_version(version), r_version(m.group(2))
    return {">=": a >= b, "<=": a <= b, "==": a == b, ">": a > b, "<": a < b, "!=": a != b}[m.group(1)]


def parse_r(filename: str, path: Path, md5: str) -> Parsed:
    m = R_FILENAME.fullmatch(filename)
    if not m:
        raise PackageError(422, "R packages are source tarballs named <package>_<version>.tar.gz (R CMD build)")
    name, version = m.groups()
    try:
        with tarfile.open(path, "r:gz") as t:
            member = next((x for x in t if x.isfile() and x.name == f"{name}/DESCRIPTION"), None)
            if member is None:
                raise PackageError(422, f"the tarball has no {name}/DESCRIPTION")
            fields = parse_dcf(_read_member(lambda: t.extractfile(member).read(MAX_METADATA + 1)))
    except (tarfile.TarError, OSError, EOFError):
        raise PackageError(422, "the file is not a readable R source tarball") from None
    if fields.get("Package") != name or fields.get("Version") != version:
        raise PackageError(422, "DESCRIPTION names a different package or version than the file name")
    if "Built" in fields:
        raise PackageError(422, "this is a built (binary) R package; publish the source tarball from R CMD build")
    if name in R_BASE:
        raise PackageError(422, f"{name} is part of R itself")
    meta = {k: fields[k] for k in R_FIELDS if k in fields}
    meta["MD5sum"] = md5
    requires = [f"{n} ({c})" if c else n for n, c in r_dependencies(fields)]
    needs_r = re.search(r"(?:^|,)\s*R\s*\(([^)]*)\)", fields.get("Depends", ""))
    if needs_r:
        requires.append(f"R ({needs_r.group(1).strip()})")
    return Parsed("r", name, name.lower(), version, summary=fields.get("Title", "")[:500], meta=meta, requires=requires)


# --- storing -----------------------------------------------------------------------------------------------

@dataclass
class Received:
    path: Path
    sha256: str
    md5: str
    size: int


def receive(chunks: Iterable[bytes]) -> Received:
    """Streams an upload to a temporary file next to the repository, enforcing the size limit."""
    limit = get_settings().package_max_upload_mb * 1024 * 1024
    staging = root_dir() / ".incoming"
    staging.mkdir(parents=True, exist_ok=True)
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0  # md5 only for R's PACKAGES MD5sum field
    fd, name = tempfile.mkstemp(dir=staging)
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise PackageError(413, f"packages may be at most {get_settings().package_max_upload_mb} MB")
                sha.update(chunk)
                md5.update(chunk)
                out.write(chunk)
    except BaseException:
        os.unlink(name)
        raise
    return Received(Path(name), sha.hexdigest(), md5.hexdigest(), size)


def parse(kind: str, filename: str, received: Received) -> Parsed:
    if kind == "python":
        return parse_python(filename, received.path)
    if kind == "r":
        return parse_r(filename, received.path, received.md5)
    raise PackageError(422, "kind must be python or r")


def find(db: Session, kind: str, key: str) -> Optional[Package]:
    return db.scalar(select(Package).where(Package.kind == kind, Package.key == key))


def add_file(db: Session, package: Package, parsed: Parsed, filename: str, received: Received, by: str) -> Tuple[PackageFile, bool]:
    """(file, created). The same file again is a no-op; a different file under an existing name never replaces it."""
    existing = db.scalar(select(PackageFile).where(PackageFile.package_id == package.id, PackageFile.filename == filename))
    if existing:
        received.path.unlink(missing_ok=True)
        if existing.sha256 != received.sha256:
            raise PackageError(409, f"{filename} already exists with different content; published files never change, publish a new version")
        return existing, False
    if package.kind == "r" and db.scalar(select(PackageFile.id).where(PackageFile.package_id == package.id,
                                                                      PackageFile.version == parsed.version)):
        received.path.unlink(missing_ok=True)
        raise PackageError(409, f"{package.name} {parsed.version} already exists; publish a new version")
    target = file_path(package.kind, package.key, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(received.path, target)
    row = PackageFile(package_id=package.id, version=parsed.version, filename=filename, sha256=received.sha256,
                      size=received.size, requires_python=parsed.requires_python, requires=parsed.requires,
                      meta=parsed.meta, uploaded_by=by)
    if parsed.summary:
        package.summary = parsed.summary
    db.add(row)
    db.flush()
    return row, True


def publish(db: Session, user: User, kind: str, filename: str, received: Received) -> Tuple[PackageFile, bool]:
    """A person's or program's own package. New names need Publisher on the repository; new versions of an
    existing package need publish on that package (its Owner group or a Maintainer)."""
    try:
        parsed = parse(kind, filename, received)
        package = find(db, kind, parsed.key)
        if package is not None and package.origin != "internal":
            raise PackageError(409, f"{package.name} is imported from {ORIGIN_LABEL[package.origin]}; it cannot be "
                                    "published here. Choose another name for your own package")
        if package is None:
            rbac.require(db, user, "create", rbac.packages_root(db))
            from .package_imports import upstream_has
            if upstream_has(kind, parsed.name):
                raise PackageError(409, f"a package named {parsed.name} exists on {'PyPI' if kind == 'python' else 'CRAN'}; "
                                        "import it instead, or choose another name for your own package")
            package = Package(kind=kind, key=parsed.key, name=parsed.name, origin="internal", created_by=user.username)
            db.add(package)
            db.flush()
            resource = Resource(kind="package", name=parsed.name, parent_id=rbac.packages_root(db).id,
                                object_id=package.id, config={"package_kind": kind})
            db.add(resource)
            db.flush()
            package.resource_id = resource.id
            rbac.own(db, user, resource)
        else:
            rbac.require(db, user, "publish", db.get(Resource, package.resource_id))
        return add_file(db, package, parsed, filename, received, user.username)
    finally:
        received.path.unlink(missing_ok=True)


def store_imported(db: Session, kind: str, origin: str, filename: str, received: Received, by: str) -> Tuple[PackageFile, bool]:
    parsed = parse(kind, filename, received)
    package = find(db, kind, parsed.key)
    if package is not None and package.origin == "internal":
        received.path.unlink(missing_ok=True)
        raise PackageError(409, f"{parsed.name} is a package published here; an upstream package of that name is not imported")
    if package is None:
        package = Package(kind=kind, key=parsed.key, name=parsed.name, origin=origin, created_by=by)
        db.add(package)
        db.flush()
    return add_file(db, package, parsed, filename, received, by)


def remove_file_data(row: PackageFile, package: Package) -> None:
    file_path(package.kind, package.key, row.filename).unlink(missing_ok=True)


# --- read keys for executions ----------------------------------------------------------------------------
# pip and R get the repository address with a short key in its path instead of the execution's token: R names
# cache files after the address (a token is too long) and both print it in errors. A key only reads packages,
# and only while its workspace (w), run (r) or deployed workload (p) is active.

def _key_mac(scope: str, ident: str, secret: str) -> str:
    import base64
    import hmac
    digest = hmac.new(secret.encode(), f"fdp-package-key:{scope}:{ident}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()[:22]


def package_key(scope: str, ident: str) -> str:
    from .auth import session_keys
    return f"{scope}{ident}.{_key_mac(scope, ident, session_keys()[0])}"


def key_claims(db: Session, key: str) -> Optional[dict]:
    """The execution claims a valid key stands for (not yet checked for being active), or None."""
    import hmac
    from .auth import session_keys
    from .models import Run, Workload, Workspace
    head, _, mac = key.partition(".")
    scope, ident = head[:1], head[1:]
    if scope not in ("w", "r", "p") or not ident or not any(
            hmac.compare_digest(mac, _key_mac(scope, ident, k)) for k in session_keys()):
        return None
    if scope == "w":
        ws = db.get(Workspace, ident)
        return ws and {"sub": ws.owner, "ws": ws.id, "pid": ws.principal_id or ""}
    if scope == "r":
        run = db.get(Run, ident)
        return run and {"sub": run.owner, "run": run.id, "ws": run.workspace_id or "", "pid": run.principal_id or ""}
    pub = db.get(Workload, ident)
    return pub and {"sub": pub.identity, "pub": pub.id, "sha": pub.deployed_sha or ""}


# --- permissions per package -----------------------------------------------------------------------------

def guard(db: Session, package: Package) -> Resource:
    """The resource whose grants govern this package: its own (internal) or the repository's (imported)."""
    resource = db.get(Resource, package.resource_id) if package.resource_id else None
    return resource or rbac.packages_root(db)


def actions(db: Session, user: User, package: Optional[Package] = None) -> List[str]:
    resource = guard(db, package) if package else rbac.packages_root(db)
    return sorted(a for a in ("create", "import", "publish", "yank", "delete", "assign") if rbac.allowed(db, user, a, resource))


# --- versions --------------------------------------------------------------------------------------------

def sort_key(kind: str, version: str):
    if kind == "python":
        try:
            return (0, Version(version))
        except InvalidVersion:
            return (-1, Version("0"))
    return r_version(version)


def files_of(db: Session, package: Package) -> List[PackageFile]:
    return list(db.scalars(select(PackageFile).where(PackageFile.package_id == package.id)))


def versions(db: Session, package: Package) -> List[Tuple[str, List[PackageFile]]]:
    """Newest first."""
    by_version: Dict[str, List[PackageFile]] = {}
    for f in files_of(db, package):
        by_version.setdefault(f.version, []).append(f)
    return sorted(by_version.items(), key=lambda item: sort_key(package.kind, item[0]), reverse=True)


def latest(db: Session, package: Package) -> Optional[str]:
    for version, files in versions(db, package):
        if not all(f.yanked for f in files):
            return version
    return None


# --- the indexes -------------------------------------------------------------------------------------------

def python_project_list(db: Session) -> List[Package]:
    return sorted(db.scalars(select(Package).where(Package.kind == "python")), key=lambda p: p.key)


def python_index_html(db: Session) -> str:
    links = "\n".join(f'<a href="{html.escape(p.key)}/">{html.escape(p.name)}</a><br>' for p in python_project_list(db))
    return f"<!DOCTYPE html>\n<html><head><meta name=\"pypi:repository-version\" content=\"1.0\"><title>Simple index</title></head><body>\n{links}\n</body></html>\n"


def python_index_json(db: Session) -> dict:
    return {"meta": {"api-version": "1.0"}, "projects": [{"name": p.name} for p in python_project_list(db)]}


def _file_url(package: Package, f: PackageFile) -> str:
    return f"../../files/{package.key}/{f.filename}"


def python_project_html(db: Session, package: Package) -> str:
    rows = []
    for f in sorted(files_of(db, package), key=lambda f: (sort_key("python", f.version), f.filename)):
        attrs = ""
        if f.requires_python:
            attrs += f' data-requires-python="{html.escape(f.requires_python)}"'
        if f.yanked:
            attrs += f' data-yanked="{html.escape(f.yanked_reason)}"'
        rows.append(f'<a href="{html.escape(_file_url(package, f))}#sha256={f.sha256}"{attrs}>{html.escape(f.filename)}</a><br>')
    body = "\n".join(rows)
    return (f"<!DOCTYPE html>\n<html><head><meta name=\"pypi:repository-version\" content=\"1.0\">"
            f"<title>Links for {html.escape(package.name)}</title></head><body>\n<h1>Links for {html.escape(package.name)}</h1>\n{body}\n</body></html>\n")


def python_project_json(db: Session, package: Package) -> dict:
    files = []
    for f in sorted(files_of(db, package), key=lambda f: (sort_key("python", f.version), f.filename)):
        item = {"filename": f.filename, "url": _file_url(package, f), "hashes": {"sha256": f.sha256}, "size": f.size}
        if f.requires_python:
            item["requires-python"] = f.requires_python
        if f.yanked:
            item["yanked"] = f.yanked_reason or True
        files.append(item)
    return {"meta": {"api-version": "1.1"}, "name": package.key, "files": files,
            "versions": [v for v, _ in reversed(versions(db, package))]}


def r_packages_index(db: Session) -> str:
    """PACKAGES: the newest version of each R package that is not yanked."""
    stanzas = []
    for package in sorted(db.scalars(select(Package).where(Package.kind == "r")), key=lambda p: p.key):
        for version, files in versions(db, package):
            current = [f for f in files if not f.yanked]
            if current:
                stanzas.append("\n".join(f"{k}: {current[0].meta[k]}" for k in R_FIELDS if current[0].meta.get(k)))
                break
    return "\n\n".join(stanzas) + ("\n" if stanzas else "")


def r_packages_gz(db: Session) -> bytes:
    return gzip.compress(r_packages_index(db).encode(), mtime=0)


def r_file(db: Session, filename: str) -> Optional[Tuple[Package, PackageFile]]:
    m = R_FILENAME.fullmatch(filename)
    package = find(db, "r", m.group(1).lower()) if m else None
    row = package and db.scalar(select(PackageFile).where(PackageFile.package_id == package.id, PackageFile.filename == filename))
    return (package, row) if row else None


def mark_yanked(db: Session, package: Package, version: str, reason: Optional[str]) -> List[PackageFile]:
    rows = [f for f in files_of(db, package) if f.version == version]
    if not rows:
        raise PackageError(404, f"{package.name} has no version {version}")
    for f in rows:
        f.yanked, f.yanked_reason = reason is not None, (reason or "")[:300]
    return rows


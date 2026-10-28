"""The package repository: the app's API, the Python simple index (pip, uv) with twine uploads, and the
CRAN-style R repository. Every identity may read; publishing follows the packages resource's roles."""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import package_imports, packages as repo, rbac
from ..auth import User
from ..db import SessionLocal, get_db
from ..models import AppGroup, Package, PackageFile, PackageImport, Resource
from .common import audit

router = APIRouter(prefix="/v1/packages", tags=["packages"])
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
CHALLENGE = {"WWW-Authenticate": 'Basic realm="Floren Data Platform packages"'}


def package_user(request: Request, authorization: Optional[str] = Header(default=None)) -> User:
    """A package key in the path (/x/<key>/..., reads by executions), Bearer, or HTTP Basic (twine: the token is
    the password). A workspace or run token keeps working after its expiry while the workspace or run is still
    active, as for the token renewal endpoint."""
    key = request.path_params.get("key")
    if key is not None:
        with SessionLocal() as db:
            claims = repo.key_claims(db, key)
        user = _active_execution(claims) if claims else None
        if user is None:
            raise HTTPException(401, "this package key is not valid, or its workspace or run is no longer active")
        return user
    token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "basic":
            try:
                token = base64.b64decode(value.strip()).decode().partition(":")[2]
            except (binascii.Error, UnicodeDecodeError):
                token = ""
        elif scheme.lower() == "bearer":
            token = value.strip()
    if not token:
        raise HTTPException(401, "sign in with a Floren Data Platform token (as the password)", headers=CHALLENGE)
    claims = _execution_claims(token)
    if claims is not None:
        user = _active_execution(claims)
        if user is None:
            raise HTTPException(401, "this workspace or run is no longer active", headers=CHALLENGE)
        return user
    from ..deps import get_actor
    try:
        return get_actor(authorization=f"Bearer {token}", x_fdp_active_group=None)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(401, exc.detail, headers=CHALLENGE) from None
        raise


def _execution_claims(token: str) -> Optional[dict]:
    """A platform execution token's claims, expired or not; None for any other token."""
    import jwt
    from ..auth import decode_session
    try:
        claims = decode_session(token, verify_exp=False)
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "execution" else None


def _active_execution(claims: dict) -> Optional[User]:
    from ..auth import validate_execution
    try:
        validate_execution(claims)
    except HTTPException:
        return None
    from ..environments import execution_user
    from ..rbac import resolve_user
    with SessionLocal() as db:
        user = resolve_user(db, execution_user(db, claims))
        db.commit()
        return user


def _fail(exc: repo.PackageError):
    raise HTTPException(exc.status, exc.message) from None


def _package(db: Session, kind: str, name: str) -> Package:
    if kind not in repo.KINDS:
        raise HTTPException(404, "kind must be python or r")
    package = repo.find(db, kind, repo.key_of(kind, name))
    if package is None:
        raise HTTPException(404, f"no {kind} package {name}")
    return package


def _owner(db: Session, package: Package) -> Optional[str]:
    resource = db.get(Resource, package.resource_id) if package.resource_id else None
    group = db.get(AppGroup, (resource.config or {}).get("owner_group_id")) if resource else None
    return group.name if group else None


def package_out(db: Session, package: Package) -> dict:
    versions = repo.versions(db, package)
    return {"id": package.id, "kind": package.kind, "name": package.name, "key": package.key, "origin": package.origin,
            "summary": package.summary, "latest": repo.latest(db, package), "versions": len(versions),
            "owner_group": _owner(db, package), "resource_id": package.resource_id, "created_by": package.created_by,
            "created_at": package.created_at}


def file_out(f: PackageFile) -> dict:
    return {"filename": f.filename, "size": f.size, "sha256": f.sha256, "requires_python": f.requires_python,
            "yanked": f.yanked, "yanked_reason": f.yanked_reason, "uploaded_by": f.uploaded_by, "created_at": f.created_at}


def install_hint(package: Package, version: Optional[str] = None) -> str:
    if package.kind == "python":
        return f"pip install {package.name}" + (f"=={version}" if version else "")
    return f'renv::install("{package.name}@{version}")' if version else f'install.packages("{package.name}")'


def import_out(job: PackageImport) -> dict:
    return {"id": job.id, "kind": job.kind, "requirement": job.requirement, "status": job.status,
            "requested_by": job.requested_by, "log": job.log, "added": job.added or [], "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at}


# --- the app's API ---------------------------------------------------------------------------------------

@router.get("")
def list_packages(kind: Optional[Literal["python", "r"]] = None, q: str = "", db: Session = Depends(get_db),
                  user: User = Depends(package_user)):
    query = select(Package).order_by(Package.kind, Package.key)
    if kind:
        query = query.where(Package.kind == kind)
    rows = [p for p in db.scalars(query) if q.lower() in p.key or q.lower() in p.name.lower()]
    from ..config import get_settings
    s = get_settings()
    root = rbac.packages_root(db)
    db.commit()
    return {"packages": [package_out(db, p) for p in rows], "actions": repo.actions(db, user), "resource_id": root.id,
            "upstream": {"python": bool(s.package_upstream_pypi), "r": bool(s.package_upstream_cran) and s.r_enabled},
            "kinds": ["python", "r"] if s.r_enabled else ["python"], "max_upload_mb": s.package_max_upload_mb}


class AllowedPackageIn(BaseModel):
    name: str = Field(min_length=1, max_length=200, description='"pandas", or "scikit-*" for a family')
    description: str = Field(default="", max_length=200)


class PackageAllowlistIn(BaseModel):
    enabled: bool = Field(default=True, description="Off: Publishers may import any public package")
    python: List[AllowedPackageIn] = Field(default_factory=list, max_length=5000)
    r: List[AllowedPackageIn] = Field(default_factory=list, max_length=5000)


@router.get("/allowlist")
def package_allowlist(db: Session = Depends(get_db), _: User = Depends(package_user)):
    """Which public packages may be imported (everyone may read it, to know what to ask for)."""
    from .. import package_allowlist as allow
    return allow.get(db)


@router.put("/allowlist")
def set_package_allowlist(body: PackageAllowlistIn, db: Session = Depends(get_db), user: User = Depends(package_user)):
    from .. import package_allowlist as allow
    if not (user.is_admin and not user.execution_context):
        raise HTTPException(403, "administrators manage the package allowlist")
    entries = {}
    for kind in ("python", "r"):
        seen, rows = set(), []
        for item in getattr(body, kind):
            name = item.name.strip()
            if not allow.NAME[kind].fullmatch(name):
                raise HTTPException(422, f"{name!r} is not a {'Python' if kind == 'python' else 'R'} package name")
            key = allow.normalize(kind, name)
            if key in seen:
                raise HTTPException(422, f"{name} is listed twice")
            seen.add(key)
            rows.append({"name": allow.stored(kind, name), "description": item.description.strip()})
        entries[kind] = rows
    result = allow.save(db, body.enabled, entries)
    audit(db, user.username, "packages.allowlist", "packages", enabled=body.enabled,
          python=len(entries["python"]), r=len(entries["r"]))
    db.commit()
    return result


@router.delete("/allowlist", status_code=204)
def reset_package_allowlist(db: Session = Depends(get_db), user: User = Depends(package_user)):
    """Off and empty: Publishers may import any public package."""
    from ..models import PlatformSetting
    from .. import package_allowlist as allow
    if not (user.is_admin and not user.execution_context):
        raise HTTPException(403, "administrators manage the package allowlist")
    row = db.get(PlatformSetting, allow.KEY)
    if row:
        db.delete(row)
    audit(db, user.username, "packages.allowlist.reset", "packages")
    db.commit()


class ImportIn(BaseModel):
    kind: Literal["python", "r"]
    requirement: str = Field(min_length=1, max_length=300, description="pandas==2.2.3, requests>=2.32, dplyr, dplyr==1.1.4")


@router.post("/imports", status_code=202)
def request_import(body: ImportIn, db: Session = Depends(get_db), user: User = Depends(package_user)):
    """Brings a package and its missing dependencies in from PyPI or CRAN; runs in the background."""
    from ..runtimes import require_kind
    require_kind(body.kind)
    try:
        job = package_imports.request(db, user, body.kind, body.requirement)
    except repo.PackageError as exc:
        _fail(exc)
    audit(db, user.username, "package.import.request", f"{job.kind}:{job.requirement}")
    db.commit()
    from ..config import get_settings
    if get_settings().package_imports_inline:
        while (claimed := package_imports.claim(SessionLocal)):
            package_imports.run(SessionLocal, claimed)
        db.refresh(job)
    return import_out(job)


@router.get("/imports")
def list_imports(db: Session = Depends(get_db), _: User = Depends(package_user)):
    return [import_out(j) for j in db.scalars(select(PackageImport).order_by(PackageImport.created_at.desc()).limit(50))]


@router.get("/imports/{job_id}")
def get_import(job_id: str, db: Session = Depends(get_db), _: User = Depends(package_user)):
    job = db.get(PackageImport, job_id)
    if job is None:
        raise HTTPException(404, "import not found")
    return import_out(job)


def _publish(user: User, kind: str, filename: str, chunks) -> dict:
    with SessionLocal() as db:
        try:
            row, created = repo.publish(db, user, kind, Path(filename or "").name, repo.receive(chunks))
        except repo.PackageError as exc:
            db.rollback()
            _fail(exc)
        package = db.get(Package, row.package_id)
        if created:
            audit(db, user.username, "package.publish", f"{kind}:{package.name}", version=row.version, file=row.filename)
        db.commit()
        return {"package": package_out(db, package), "file": file_out(row), "created": created}


@router.post("/{kind}/upload", status_code=201)
def upload(kind: Literal["python", "r"], file: UploadFile = File(...), user: User = Depends(package_user)):
    """Publish a wheel, a Python source distribution or an R source tarball."""
    from ..runtimes import require_kind
    require_kind(kind)
    return _publish(user, kind, file.filename or "", iter(lambda: file.file.read(1 << 20), b""))


@router.get("/{kind}/{name}")
def get_package(kind: str, name: str, db: Session = Depends(get_db), user: User = Depends(package_user)):
    package = _package(db, kind, name)
    versions = [{"version": v, "yanked": all(f.yanked for f in files), "requires": files[0].requires,
                 "install": install_hint(package, v), "files": [file_out(f) for f in sorted(files, key=lambda f: f.filename)]}
                for v, files in repo.versions(db, package)]
    return {**package_out(db, package), "versions": versions, "version_count": len(versions),
            "actions": repo.actions(db, user, package), "install": install_hint(package)}


class YankIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/{kind}/{name}/versions/{version}/yank")
def yank(kind: str, name: str, version: str, body: YankIn, db: Session = Depends(get_db), user: User = Depends(package_user)):
    """Installs no longer pick this version unless it is asked for exactly; nothing is removed."""
    package = _package(db, kind, name)
    rbac.require(db, user, "yank", repo.guard(db, package))
    try:
        repo.mark_yanked(db, package, version, body.reason)
    except repo.PackageError as exc:
        _fail(exc)
    audit(db, user.username, "package.yank", f"{kind}:{package.name}", version=version, reason=body.reason)
    db.commit()
    return get_package(kind, name, db, user)


@router.delete("/{kind}/{name}/versions/{version}/yank")
def unyank(kind: str, name: str, version: str, db: Session = Depends(get_db), user: User = Depends(package_user)):
    package = _package(db, kind, name)
    rbac.require(db, user, "yank", repo.guard(db, package))
    try:
        repo.mark_yanked(db, package, version, None)
    except repo.PackageError as exc:
        _fail(exc)
    audit(db, user.username, "package.unyank", f"{kind}:{package.name}", version=version)
    db.commit()
    return get_package(kind, name, db, user)


@router.delete("/{kind}/{name}/versions/{version}", status_code=204)
def delete_version(kind: str, name: str, version: str, db: Session = Depends(get_db), user: User = Depends(package_user)):
    """Removes the files for good; programs pinned to this version stop installing. Prefer yanking."""
    package = _package(db, kind, name)
    rbac.require(db, user, "delete", repo.guard(db, package))
    rows = [f for f in repo.files_of(db, package) if f.version == version]
    if not rows:
        raise HTTPException(404, f"{package.name} has no version {version}")
    for f in rows:
        repo.remove_file_data(f, package)
        db.delete(f)
    db.flush()
    audit(db, user.username, "package.delete", f"{kind}:{package.name}", version=version)
    if not repo.files_of(db, package):
        if package.resource_id:
            rbac.delete_resource_assignments(db, package.resource_id)
            resource = db.get(Resource, package.resource_id)
            if resource:
                db.delete(resource)
        db.delete(package)
    db.commit()


# --- Python: the simple index (PEP 503 / 691) and twine's upload API ---------------------------------------

def _wants_json(accept: str) -> bool:
    return SIMPLE_JSON in (accept or "")


@router.get("/python/simple/", include_in_schema=False)
def simple_index(db: Session = Depends(get_db), _: User = Depends(package_user), accept: str = Header(default="")):
    if _wants_json(accept):
        return JSONResponse(repo.python_index_json(db), media_type=SIMPLE_JSON)
    return HTMLResponse(repo.python_index_html(db))


@router.get("/python/simple/{name}/", include_in_schema=False)
def simple_project(name: str, db: Session = Depends(get_db), _: User = Depends(package_user), accept: str = Header(default="")):
    package = repo.find(db, "python", repo.key_of("python", name))
    if package is None:
        raise HTTPException(404, f"no package {name} in the repository; ask a Publisher to import it")
    if _wants_json(accept):
        return JSONResponse(repo.python_project_json(db, package), media_type=SIMPLE_JSON)
    return HTMLResponse(repo.python_project_html(db, package))


def _serve(package: Package, row: Optional[PackageFile]) -> FileResponse:
    path = repo.file_path(package.kind, package.key, row.filename) if row else None
    if not path or not path.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=row.filename)


@router.get("/python/files/{package_key}/{filename}", include_in_schema=False)
def python_file(package_key: str, filename: str, db: Session = Depends(get_db), _: User = Depends(package_user)):
    package = repo.find(db, "python", package_key)
    row = package and db.scalar(select(PackageFile).where(PackageFile.package_id == package.id, PackageFile.filename == filename))
    if not package or not row:
        raise HTTPException(404, "file not found")
    return _serve(package, row)


@router.post("/python/legacy/", include_in_schema=False)
async def twine_upload(request: Request, user: User = Depends(package_user)):
    """twine upload / uv publish (the upload API PyPI uses): TWINE_REPOSITORY_URL is set in workspaces."""
    form = await request.form()
    if form.get(":action") != "file_upload":
        raise HTTPException(400, "unsupported action")
    content = form.get("content")
    if content is None or not hasattr(content, "file"):
        raise HTTPException(400, "no file in the upload")
    result = await run_in_threadpool(_publish, user, "python", content.filename or "",
                                     iter(lambda: content.file.read(1 << 20), b""))
    return PlainTextResponse("OK" if result["created"] else "File already exists (identical)")


# --- R: a CRAN-style repository --------------------------------------------------------------------------

@router.get("/r/src/contrib/PACKAGES", include_in_schema=False)
def r_packages(db: Session = Depends(get_db), _: User = Depends(package_user)):
    return PlainTextResponse(repo.r_packages_index(db))


@router.get("/r/src/contrib/PACKAGES.gz", include_in_schema=False)
def r_packages_gz(db: Session = Depends(get_db), _: User = Depends(package_user)):
    return Response(repo.r_packages_gz(db), media_type="application/x-gzip")


@router.get("/r/src/contrib/Archive/{name}/{filename}", include_in_schema=False)
def r_archive_file(name: str, filename: str, db: Session = Depends(get_db), _: User = Depends(package_user)):
    found = repo.r_file(db, filename)
    if not found or found[0].name != name:
        raise HTTPException(404, "file not found")
    return _serve(*found)


@router.get("/r/src/contrib/{filename}", include_in_schema=False)
def r_source_file(filename: str, db: Session = Depends(get_db), _: User = Depends(package_user)):
    found = repo.r_file(db, filename)   # PACKAGES.rds and the like: 404, R falls back to PACKAGES.gz
    if not found:
        raise HTTPException(404, "file not found")
    return _serve(*found)


# The same reading endpoints under /x/<key>/, for executions (PIP_INDEX_URL, FDP_CRAN).
for _path, _endpoint in (("/python/simple/", simple_index), ("/python/simple/{name}/", simple_project),
                         ("/python/files/{package_key}/{filename}", python_file), ("/r/src/contrib/PACKAGES", r_packages),
                         ("/r/src/contrib/PACKAGES.gz", r_packages_gz),
                         ("/r/src/contrib/Archive/{name}/{filename}", r_archive_file), ("/r/src/contrib/{filename}", r_source_file)):
    router.add_api_route("/x/{key}" + _path, _endpoint, methods=["GET"], include_in_schema=False)

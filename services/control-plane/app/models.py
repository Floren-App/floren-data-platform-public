from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class DataSource(Base):
    """An admin-registered data source. Secrets live in OpenBao, never in this table."""

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # postgres, mssql, snowflake, s3, ...
    description: Mapped[str] = mapped_column(String(500), default="")
    # Non-secret connection settings: host, port, database, bucket, warehouse...
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # CIDRs executions may reach for this source (enforced by NetworkPolicy on Kubernetes).
    egress: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    grants: Mapped[list["DataGrant"]] = relationship(back_populates="data_source", cascade="all, delete-orphan")


class DataGrant(Base):
    __tablename__ = "data_grants"
    __table_args__ = (UniqueConstraint("data_source_id", "group"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    group: Mapped[str] = mapped_column(String(128))

    data_source: Mapped[DataSource] = relationship(back_populates="grants")


class ComputePool(Base):
    """A 'warehouse': a named size that runs start on, suspended when idle."""

    __tablename__ = "compute_pools"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    cpu: Mapped[float] = mapped_column(Float)
    memory_gb: Mapped[float] = mapped_column(Float)
    gpu: Mapped[int] = mapped_column(Integer, default=0)
    # Running containers at a time on this warehouse: jobs, deployed apps, Airflow components and
    # Trino workers. Work beyond it queues.
    max_concurrent: Mapped[int] = mapped_column(Integer, default=10)
    # Dedicated to its allowed groups (e.g. a team's business-critical Airflow): not one of the shared
    # on-demand warehouses, so it has no slot limit, never queues and counts toward nothing else.
    # Administrators make sure the cluster has nodes for it.
    dedicated: Mapped[bool] = mapped_column(default=False)
    auto_suspend_seconds: Mapped[int] = mapped_column(Integer, default=600)
    # Empty list = every user may use this pool.
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)
    # Scope: the layers (ids) and kinds of work (script, notebook, app, dbt, airflow) this warehouse is for.
    # Empty = every layer / every kind. Admission checks both, next to allowed_groups.
    layer_ids: Mapped[list] = mapped_column(JSON, default=list)
    workload_kinds: Mapped[list] = mapped_column(JSON, default=list)
    owner_group_id: Mapped[Optional[str]] = mapped_column(ForeignKey("app_groups.id"), nullable=True)


class CertificateCheck(Base):
    """The last observed state of one served certificate (gateway or layer endpoint); no key material."""

    __tablename__ = 'certificate_checks'

    key: Mapped[str] = mapped_column(String(160), primary_key=True)  # e.g. "layer:<id>:trino", "gateway:public"
    scope: Mapped[str] = mapped_column(String(16))
    layer_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    hostname: Mapped[str] = mapped_column(String(253))
    state: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(String(500), default='')
    not_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    renewal_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    serial: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSetting(Base):
    """Administrator settings that are not tied to one object (e.g. workspace limits)."""

    __tablename__ = 'platform_settings'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class LayerOperation(Base):
    """Durable reconciliation status. Desired infrastructure stays on its layer resource."""

    __tablename__ = 'layer_operations'
    __table_args__ = (UniqueConstraint('layer_id', 'generation'),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    layer_id: Mapped[str] = mapped_column(ForeignKey('rbac_resources.id'), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(24), default='Pending')
    reason: Mapped[str] = mapped_column(String(64), default='ConfigurationAccepted')
    message: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Short renewable database lease; Kubernetes calls never hold a row lock.
    lease_owner: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    namespace: Mapped[Optional[str]] = mapped_column(String(63), nullable=True)


class WorkspaceTemplate(Base):
    """A group's default workspace: every member gets their own private copy, created stopped."""
    __tablename__ = "workspace_templates"
    __table_args__ = (UniqueConstraint("group_id", "name"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[str] = mapped_column(ForeignKey("app_groups.id", ondelete="CASCADE"))
    editor: Mapped[str] = mapped_column(String(16))
    layer_id: Mapped[str] = mapped_column(String(12))
    description: Mapped[str] = mapped_column(String(500), default="")
    repositories: Mapped[list] = mapped_column(JSON, default=list)  # repository IDs cloned at first start
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspaceTemplateDismissal(Base):
    """A member deleted their workspace from this template: it is not created again for them."""
    __tablename__ = "workspace_template_dismissals"
    __table_args__ = (UniqueConstraint("template_id", "principal_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(ForeignKey("workspace_templates.id", ondelete="CASCADE"))
    principal_id: Mapped[str] = mapped_column(String(12))


class Package(Base):
    """A Python or R package in the platform's repository. internal: published here (its RBAC resource names
    who may publish new versions); pypi / cran: imported from upstream on request."""
    __tablename__ = "packages"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(8))           # python | r
    key: Mapped[str] = mapped_column(String(200))          # normalized name (PEP 503; lowercase for R)
    name: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(8))         # internal | pypi | cran
    summary: Mapped[str] = mapped_column(String(500), default="")
    resource_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PackageFile(Base):
    """One file of a package version (Python: a wheel or sdist; R: the source tarball). Never overwritten."""
    __tablename__ = "package_files"
    __table_args__ = (UniqueConstraint("package_id", "filename"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    requires_python: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    requires: Mapped[list] = mapped_column(JSON, default=list)       # dependencies as declared
    meta: Mapped[dict] = mapped_column(JSON, default=dict)           # R: DESCRIPTION fields for PACKAGES
    yanked: Mapped[bool] = mapped_column(default=False)
    yanked_reason: Mapped[str] = mapped_column(String(300), default="")
    uploaded_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PackageImport(Base):
    """A request to bring a package (and its missing dependencies) in from PyPI or CRAN."""
    __tablename__ = "package_imports"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(8))
    requirement: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="queued")   # queued | running | done | failed
    requested_by: Mapped[str] = mapped_column(String(128))
    log: Mapped[str] = mapped_column(Text, default="")
    added: Mapped[list] = mapped_column(JSON, default=list)             # filenames stored by this import
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GitCredential(Base):
    """A person's own Git token for a host (several per host are fine, e.g. work and personal), with the name
    and email to commit as. The token itself lives in the secret store, never in a workspace."""
    __tablename__ = "git_credentials"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    host: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(128))
    commit_name: Mapped[str] = mapped_column(String(128), default="")
    commit_email: Mapped[str] = mapped_column(String(254), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    owner_group_id: Mapped[Optional[str]] = mapped_column(ForeignKey("app_groups.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    editor: Mapped[str] = mapped_column(String(16))  # vscode | rstudio | sql (CloudBeaver)
    status: Mapped[str] = mapped_column(String(16), default="stopped")  # stopped | queued | starting | running | failed
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def queue_position(self) -> Optional[int]:
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if self.status != "queued" or session is None:
            return None
        from .capacity import workspace_queue
        return next((i for i, ws in enumerate(workspace_queue(session), start=1) if ws.id == self.id), None)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    startup_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Why a starting editor has not started yet (no room in the cluster, image cannot be pulled).
    waiting_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    upstream_url: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Last fdp.json read from the running workspace, so programs stay visible while it's stopped.
    principal_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    environment_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    volume_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Git for VS Code and RStudio: which of the owner's credentials this workspace uses. git_asked: the owner
    # chose (a credential, or none); until then the app asks at the first start.
    git_credential_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    git_asked: Mapped[bool] = mapped_column(default=False)
    config_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config_read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Created for a member of a group from this template (see WorkspaceTemplate); None for one a person made.
    template_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)

    @property
    def layer_id(self):
        from sqlalchemy.orm import object_session
        from .layers import layer_of
        return layer_of(object_session(self), self.environment_id)

    @property
    def layer_name(self):
        from sqlalchemy.orm import object_session
        layer = object_session(self).get(Resource, self.layer_id)
        return layer.name if layer else None

    @property
    def owner_group_name(self):
        from sqlalchemy.orm import object_session
        group = object_session(self).get(AppGroup, self.owner_group_id) if self.owner_group_id else None
        return group.name if group else None


class Run(Base):
    """A unit of work on a compute pool: a script, a notebook, or a Streamlit/Shiny app."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    principal_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    initiator_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    initiated_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    access_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    primary_create_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    primary_group_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    environment_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    # A run comes from a private workspace, or from a deployed program (code at a commit).
    workspace_id: Mapped[Optional[str]] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True)
    workload_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("workloads.id", ondelete="SET NULL"), nullable=True, index=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    pool_id: Mapped[str] = mapped_column(ForeignKey("compute_pools.id"))
    kind: Mapped[str] = mapped_column(String(16))  # script | notebook | app
    entrypoint: Mapped[str] = mapped_column(String(512))
    program: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # name in fdp.json
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    runtime: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)          # python | r | dbt | airflow
    runtime_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # OpenBao paths of the secrets given to this run: their values are masked in its console.
    secret_paths: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # queued|pending|running|succeeded|failed|stopped
    # Why a pending run has not started yet (no room in the cluster, image cannot be pulled), from the orchestrator.
    waiting_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # A queued run's launch: its plan, groups and granted source names. Secret *paths* only, never values.
    launch: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    @property
    def queue_position(self) -> Optional[int]:
        """1-based place in its warehouse's queue while queued."""
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if self.status != "queued" or session is None or self.pool is None:
            return None
        from .capacity import position
        return position(session, "run", self.id, self.pool.name)
    execution_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    upstream_url: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    pool: Mapped[ComputePool] = relationship()


class ServiceAccount(Base):
    """A non-human identity for a data product (pipelines, deployed apps, external tools).

    Its data access comes from `groups` (data sources are granted to groups as usual);
    `allowed_groups` are the people who may run things as it and manage its tokens and secrets.
    """

    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    groups: Mapped[list] = mapped_column(JSON, default=list)
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)
    disabled: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServiceToken(Base):
    """An API token of a service account. Only its hash is stored; the token is shown once."""

    __tablename__ = "service_tokens"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    service_account_id: Mapped[str] = mapped_column(ForeignKey("service_accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))          # shown in lists, e.g. dcs_3fa1…
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(default=False)
    stored_as: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)  # secret ref, if saved there

    service_account: Mapped[ServiceAccount] = relationship()


class SecretEntry(Base):
    """Metadata of a secret value in OpenBao. Scope: a user (private), a group or a service account."""

    __tablename__ = "secret_entries"
    __table_args__ = (UniqueConstraint("scope", "owner", "name"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(8))            # user | group | sa
    owner: Mapped[str] = mapped_column(String(128))          # user name, group name or service account name
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[Optional[str]] = mapped_column(String(16), default="string", nullable=True)  # string | certificate
    # Certificates: public facts for display and expiry (subject, issuer, validity...); never key material.
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeImage(Base):
    """A runtime version admins offer (python 3.12, r 4.4, dbt 1.8, airflow 2.10) and its image."""

    __tablename__ = "runtime_images"
    __table_args__ = (UniqueConstraint("kind", "version"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))      # python | r | dbt | airflow
    version: Mapped[str] = mapped_column(String(16))
    image: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String(200), default="")
    is_default: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    # "installation": a version the Helm installation ships; its image follows the Helm values at every start
    # and is not edited here. Empty: added by an administrator or OpenTofu (fdp_runtime); Helm never touches it.
    managed_by: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GitRepository(Base):
    """A git repository admins registered for deploying. Read credentials live in OpenBao."""

    __tablename__ = "git_repositories"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(String(512))
    default_branch: Mapped[str] = mapped_column(String(200), default="main")
    description: Mapped[str] = mapped_column(String(500), default="")
    # Who may deploy from it; empty = everyone.
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)
    owner_group_id: Mapped[Optional[str]] = mapped_column(ForeignKey("app_groups.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workload(Base):
    """A program from a repo branch, kept in sync with that branch (GitOps).

    Apps run permanently at /p/<slug>/ with the deployer's data access. Programs (scripts,
    notebooks) are run on demand by viewers, each with their own data access.
    """

    __tablename__ = "workloads"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    repo_id: Mapped[str] = mapped_column(ForeignKey("git_repositories.id"))
    branch: Mapped[str] = mapped_column(String(200))
    program: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    runtime: Mapped[str] = mapped_column(String(16))
    runtime_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    owner_principal_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    owner_groups: Mapped[list] = mapped_column(JSON, default=list)  # data identity for apps, dbt docs, airflow
    # Run as a service account instead of the deployer (production pipelines, shared dashboards).
    run_as: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    service_principal_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(16), default="caller")
    # Production environment: plain variables and secret references, over fdp.json's.
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets: Mapped[dict] = mapped_column(JSON, default=dict)
    allowed_groups: Mapped[list] = mapped_column(JSON, default=list)  # viewers; empty = everyone
    warehouse: Mapped[str] = mapped_column(String(64))
    auto_update: Mapped[bool] = mapped_column(default=True)
    # live | deploying | failed | stopped — "live" for a program means: runnable.
    status: Mapped[str] = mapped_column(String(16), default="deploying")
    # Waiting for warehouse capacity to deploy this commit (status "queued" until it first goes live).
    queued_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_demand: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # warehouse name -> containers

    @property
    def queue_position(self) -> Optional[int]:
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if not self.queued_sha or session is None:
            return None
        from .capacity import position
        return position(session, "workload", self.id, next(iter(self.queued_demand or {}), None))
    deployed_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deployed_commit: Mapped[dict] = mapped_column(JSON, default=dict)  # subject, author, date
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last commit that could not be deployed; not retried until the branch moves on.
    failed_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    latest_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    access_issue: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    program_def: Mapped[dict] = mapped_column(JSON, default=dict)  # Program at deployed_sha
    execution_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    upstream_url: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # Every container of a multi-container deployment (distributed Airflow), including execution_ref.
    execution_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    repo: Mapped[GitRepository] = relationship()

    @property
    def identity(self) -> str:
        """Who its executions act as: the service account, or the deployer."""
        return f"sa:{self.run_as}" if self.run_as else f"service:{self.service_principal_id}"

    events: Mapped[list["WorkloadEvent"]] = relationship(
        back_populates="workload", cascade="all, delete-orphan", order_by="WorkloadEvent.id.desc()")


class WorkloadEvent(Base):
    __tablename__ = "workload_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # deployed | update_found | deployed | sync_failed | ...
    sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")

    workload: Mapped[Workload] = relationship(back_populates="events")


class AuditEvent(Base):
    """Who touched which data source, from which execution. Append-only."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class SiemCursor(Base):
    """How far each SIEM destination has received the audit trail, and the tip of its hash chain."""

    __tablename__ = "siem_cursors"

    sink: Mapped[str] = mapped_column(String(32), primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, default=0)
    query_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    query_id: Mapped[str] = mapped_column(String(12), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), default="")
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DirectoryPrincipal(Base):
    """Stable identity facts. SCIM-managed identities never refresh from stale token claims."""
    __tablename__ = "directory_principals"
    __table_args__ = (UniqueConstraint("provider", "kind", "external_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(256), default="local")
    kind: Mapped[str] = mapped_column(String(16))  # user | group | role | service
    external_id: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(256))
    subject: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    managed: Mapped[bool] = mapped_column(default=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


class DirectoryMembership(Base):
    __tablename__ = "directory_memberships"
    __table_args__ = (UniqueConstraint("group_id", "member_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(ForeignKey("directory_principals.id"))
    member_id: Mapped[str] = mapped_column(ForeignKey("directory_principals.id"))


class AppGroup(Base):
    __tablename__ = "app_groups"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    owners: Mapped[list] = mapped_column(JSON, default=list)  # stable principal IDs


class UserPreference(Base):
    __tablename__ = "user_preferences"
    principal_id: Mapped[str] = mapped_column(ForeignKey("directory_principals.id"), primary_key=True)
    active_group_id: Mapped[Optional[str]] = mapped_column(ForeignKey("app_groups.id"), nullable=True)


class IdentityBinding(Base):
    __tablename__ = "identity_bindings"
    __table_args__ = (UniqueConstraint("app_group_id", "principal_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    app_group_id: Mapped[str] = mapped_column(ForeignKey("app_groups.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("directory_principals.id"))


class Resource(Base):
    __tablename__ = "rbac_resources"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # root | folder | service | source | schema
    name: Mapped[str] = mapped_column(String(256))
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("rbac_resources.id"), nullable=True, index=True)
    object_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (UniqueConstraint("resource_id", "app_group_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(ForeignKey("rbac_resources.id"), index=True)
    app_group_id: Mapped[str] = mapped_column(ForeignKey("app_groups.id"))
    role: Mapped[str] = mapped_column(String(16))


class ServiceRoleAssignment(Base):
    """Direct grants to managed identities; existing app-group grants stay independent."""
    __tablename__ = "service_role_assignments"
    __table_args__ = (UniqueConstraint("resource_id", "principal_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(ForeignKey("rbac_resources.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("directory_principals.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))


class CatalogAsset(Base):
    """Metadata only. The owning RBAC resource is always the authorization boundary."""
    __tablename__ = "catalog_assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("rbac_resources.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(2048), default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    governance: Mapped[dict] = mapped_column(JSON, default=dict)
    sla: Mapped[dict] = mapped_column(JSON, default=dict)
    search_text: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LineageEdge(Base):
    __tablename__ = "catalog_lineage_edges"
    __table_args__ = (UniqueConstraint("producer_id", "source_id", "target_id", "relation", "origin"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    producer_id: Mapped[str] = mapped_column(ForeignKey("rbac_resources.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32))
    revision: Mapped[str] = mapped_column(String(128), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogImport(Base):
    __tablename__ = "catalog_imports"
    __table_args__ = (UniqueConstraint("resource_id", "origin", "scope"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(ForeignKey("rbac_resources.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(256), default="")
    revision: Mapped[str] = mapped_column(String(128), default="")
    nodes: Mapped[int] = mapped_column(Integer, default=0)
    edges: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogRunOutput(Base):
    __tablename__ = "catalog_run_outputs"
    __table_args__ = (UniqueConstraint("run_id", "asset_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"))


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = (UniqueConstraint("asset_id", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(16))  # kpi | quality
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(64), default="")
    origin: Mapped[str] = mapped_column(String(16), default="manual")
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(12), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricDefinitionRevision(Base):
    __tablename__ = "metric_definition_revisions"
    __table_args__ = (UniqueConstraint('metric_id', 'version'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric_id: Mapped[str] = mapped_column(ForeignKey('metric_definitions.id', ondelete='CASCADE'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    changed_by: Mapped[str] = mapped_column(String(12), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricObservation(Base):
    __tablename__ = "metric_observations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric_id: Mapped[str] = mapped_column(ForeignKey("metric_definitions.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    definition_version: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(String(12), default="")
    revision: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class DatasetObservation(Base):
    __tablename__ = "dataset_observations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # refresh | checked | watermark
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    origin: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(String(12), default="")
    revision: Mapped[str] = mapped_column(String(128), default="")


class SlaEvaluation(Base):
    __tablename__ = "sla_evaluations"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class SlaIncident(Base):
    __tablename__ = "sla_incidents"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("catalog_assets.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(12), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class CredentialLease(Base):
    __tablename__ = "credential_leases"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    username: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(256))
    object_owner_role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class SqlQuery(Base):
    """Durable admission and audit context; SQL text and values are deliberately not stored."""
    __tablename__ = "sql_queries"
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(12), index=True)
    actor: Mapped[str] = mapped_column(String(256))
    initiator_id: Mapped[str] = mapped_column(String(12), default="")
    binding: Mapped[str] = mapped_column(String(256))
    run_id: Mapped[str] = mapped_column(String(12), default="")
    workload_id: Mapped[str] = mapped_column(String(12), default="")
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    lineage: Mapped[dict] = mapped_column(JSON, default=dict)
    workspace_id: Mapped[str] = mapped_column(String(12), default="")
    engine_query_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Managed layer whose Trino runs this query; "" is the shared legacy engine.
    engine_layer: Mapped[str] = mapped_column(String(12), default="")
    engine_scheme: Mapped[Optional[str]] = mapped_column(String(5), default="http", nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QueryLog(Base):
    """One completed Trino query in a layer: who or what ran it, on which tables, how long and with what result.
    SQL text only when administrators turn it on (it can contain literal values)."""
    __tablename__ = "query_log"
    __table_args__ = (UniqueConstraint("layer_id", "query_id"),)
    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    layer_id: Mapped[str] = mapped_column(String(12), index=True)
    query_id: Mapped[str] = mapped_column(String(128))
    sql_query_id: Mapped[str] = mapped_column(String(12), default="")     # the gateway's admission record
    principal_id: Mapped[str] = mapped_column(String(12), default="", index=True)
    actor: Mapped[str] = mapped_column(String(256), default="")          # person or service identity
    initiator_id: Mapped[str] = mapped_column(String(12), default="")
    run_id: Mapped[str] = mapped_column(String(12), default="")
    workload_id: Mapped[str] = mapped_column(String(12), default="")
    workspace_id: Mapped[str] = mapped_column(String(12), default="")
    trino_user: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    query_type: Mapped[str] = mapped_column(String(32), default="")
    state: Mapped[str] = mapped_column(String(16), default="")            # FINISHED | FAILED
    error: Mapped[str] = mapped_column(String(600), default="")
    sql_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tables: Mapped[list] = mapped_column(JSON, default=list)             # "catalog.schema.table" read
    output_table: Mapped[str] = mapped_column(String(400), default="")
    rows: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    cpu_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    wall_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    queued_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiDefinition(Base):
    """An API in API management. internal: outside callers start deployed programs here (never data);
    external: an outside service that programs call through Floren Data Platform with their own identity."""

    __tablename__ = "api_definitions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(16))      # internal | external
    protocol: Mapped[str] = mapped_column(String(8))        # rest | soap
    description: Mapped[str] = mapped_column(String(500), default="")
    # internal: caller authentication (JWT, mTLS) and SOAP settings; external: backend, auth, headers, TLS.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    resource_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    principal_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)   # internal: managed identity
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiOperation(Base):
    """One endpoint of an API, optionally in an endpoint group. Each is its own permission scope."""

    __tablename__ = "api_operations"
    __table_args__ = (UniqueConstraint("api_id", "name"),)

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    api_id: Mapped[str] = mapped_column(String(12), ForeignKey("api_definitions.id"))
    name: Mapped[str] = mapped_column(String(64))
    group: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(8), default="GET")
    path: Mapped[str] = mapped_column(String(256), default="/")
    soap_action: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(String(500), default="")
    # internal: workload, mode (async|sync), timeout, argument mapping, pool; external: extra headers, timeout.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    resource_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)


class ApiCall(Base):
    """A call to an internal API: which caller started which run (status checks are scoped to the caller)."""

    __tablename__ = "api_calls"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=new_id)
    api_id: Mapped[str] = mapped_column(String(12), index=True)
    operation_id: Mapped[str] = mapped_column(String(12))
    run_id: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    caller: Mapped[str] = mapped_column(String(512), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

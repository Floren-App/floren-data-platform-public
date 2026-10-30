from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

NAME = r"^[a-z][a-z0-9-]{1,40}$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DataSourceIn(BaseModel):
    name: str = Field(pattern=NAME)
    kind: Literal["postgres", "s3", "iceberg"]  # see storage_types.TYPES for each type's required settings
    description: str = ""
    config: Dict[str, object] = Field(default_factory=dict)
    egress: List[str] = Field(default_factory=list, max_length=128, description="CIDRs, e.g. 10.20.0.15/32")

    @field_validator('egress')
    @classmethod
    def normalize_egress(cls, values):
        from ipaddress import ip_network
        return sorted({str(ip_network(value, strict=True)) for value in values})
    # Write-only. Stored in OpenBao, never returned by the API.
    secret: Optional[Dict[str, str]] = None
    provisioner_secret: Optional[Dict[str, str]] = None


class DataSourceOut(ORM):
    id: str
    name: str
    kind: str
    description: str
    config: Dict[str, object]
    egress: List[str]
    groups: List[str] = Field(default_factory=list)
    resource_id: Optional[str] = Field(default=None, description="The data source as an RBAC object (role assignments)")


class GrantIn(BaseModel):
    group: str


class PoolIn(BaseModel):
    name: str = Field(pattern=NAME)
    cpu: float = Field(gt=0, le=128)
    memory_gb: float = Field(gt=0, le=1024)
    gpu: int = Field(default=0, ge=0, le=8)
    max_concurrent: int = Field(default=10, ge=1)
    auto_suspend_seconds: int = Field(default=600, ge=60)
    allowed_groups: List[str] = Field(default_factory=list)
    dedicated: bool = Field(default=False, description="Dedicated to allowed_groups: no slot limit (max_concurrent does not apply), never queues")
    layer_ids: List[str] = Field(default_factory=list, max_length=50,
                                 description="Layers whose work may use this warehouse; empty: every layer")
    workload_kinds: List[Literal["script", "notebook", "app", "dbt", "airflow", "stream", "kafka"]] = Field(
        default_factory=list, description="Kinds of work that may use this warehouse; empty: every kind")

    @field_validator("allowed_groups", "layer_ids", "workload_kinds", mode="before")
    @classmethod
    def _unscoped(cls, value):
        return [] if value is None else value  # warehouses from before scoping: every layer, every kind


class PoolOut(ORM, PoolIn):
    id: str
    usage_available: bool = False
    active_runs: Optional[int] = None
    active_services: Optional[int] = None
    active_executions: Optional[int] = None
    active_products: Optional[int] = None
    in_use: Optional[int] = Field(default=None, description="Containers counted against max_concurrent now")
    queued: Optional[int] = Field(default=None, description="Jobs and deploys waiting for this warehouse")
    default: bool = Field(default=False, description="Work without a warehouse runs here")
    trino_layers: List[str] = Field(default_factory=list, description="Layers whose Trino workers run here; only Trino uses it")
    layer_names: List[str] = Field(default_factory=list, description="Names of layer_ids, in the same order")
    default_for: List[Dict[str, Optional[str]]] = Field(default_factory=list,
        description="Scoped defaults that point here: {layer_id, layer_name, workload_kind}")


class WorkspaceIn(BaseModel):
    layer_id: Optional[str] = None
    name: str = Field(min_length=1, max_length=64)
    editor: Literal["vscode", "rstudio", "sql"]
    environment_id: Optional[str] = None


class WorkspaceOut(ORM):
    started_at: Optional[datetime] = None
    startup_error: Optional[str] = None
    owner_group_id: Optional[str] = None
    owner_group_name: Optional[str] = None
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    environment_id: Optional[str] = None
    id: str
    owner: str
    name: str
    editor: str
    status: str
    last_activity_at: datetime
    created_at: datetime
    queue_position: Optional[int] = Field(default=None, description="Place in the workspace queue while queued")
    waiting_reason: Optional[str] = Field(default=None, description="Why a starting editor has not started yet")
    template_id: Optional[str] = Field(default=None, description="The group template it was created from, if any")
    git_credential_id: Optional[str] = Field(default=None, description="Which of the owner's Git credentials it uses")
    git_asked: bool = Field(default=False, description="The owner chose a Git credential (or none) for it")


class RunIn(BaseModel):
    """Either a program from fdp.json (program + args) or an ad-hoc file (kind + entrypoint)."""

    workspace_id: str
    program: Optional[str] = Field(default=None, description="Program name from fdp.json")
    args: Dict[str, Any] = Field(default_factory=dict, description="Values for the program's arguments")
    pool: Optional[str] = Field(default=None, description="Warehouse; defaults to the program's or config's warehouse")
    kind: Optional[Literal["script", "notebook", "app"]] = None
    entrypoint: Optional[str] = Field(default=None, description="Ad-hoc runs: path relative to the workspace")

    @model_validator(mode="after")
    def _one_mode(self) -> "RunIn":
        if self.program is None and not (self.kind and self.entrypoint):
            raise ValueError("give either 'program' or both 'kind' and 'entrypoint'")
        return self


class RunOut(ORM):
    initiated_by: Optional[str] = None
    id: str
    owner: str
    workspace_id: Optional[str]
    workload_id: Optional[str] = None
    commit_sha: Optional[str] = None
    kind: str
    entrypoint: str
    program: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    runtime: Optional[str] = None
    runtime_version: Optional[str] = None
    status: str
    pool_name: str
    created_at: datetime
    finished_at: Optional[datetime]
    queue_position: Optional[int] = Field(default=None, description="Place in the warehouse's queue while queued")
    waiting_reason: Optional[str] = Field(default=None, description="Why a pending run has not started yet")


class ConfigOut(BaseModel):
    exists: bool
    live: bool = Field(description="Read just now from the running workspace (false = last cached copy)")
    read_at: Optional[datetime] = None
    config: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


class RepoIn(BaseModel):
    name: str = Field(pattern=NAME)
    url: str = Field(description="https://… clone URL")
    default_branch: str = "main"
    description: str = ""
    allowed_groups: List[str] = Field(default_factory=list, description="Who may deploy from it; empty = everyone")
    # Write-only, stored in OpenBao: {"username": "...", "token": "..."} with read access to the repository.
    secret: Optional[Dict[str, str]] = None


class RepoOut(ORM):
    owner_group_id: Optional[str] = None
    id: str
    name: str
    url: str
    default_branch: str
    description: str
    allowed_groups: List[str]
    has_credentials: bool = False


class BranchOut(BaseModel):
    name: str
    sha: str


class CommitOut(BaseModel):
    sha: str
    subject: str
    author: str
    date: str


class PullOut(BaseModel):
    commit: CommitOut
    programs: Dict[str, Any]
    errors: List[str]


class DeployIn(BaseModel):
    layer_id: Optional[str] = None
    model_config = ConfigDict(extra="forbid")
    folder_id: str = "workloads"
    execution_mode: Literal["caller", "fixed"] = "caller"
    repo_id: str
    branch: str
    program: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$", description="URL name: /p/<slug>/")
    title: str = Field(default="", max_length=128)
    warehouse: Optional[str] = None
    allowed_groups: List[str] = Field(default_factory=list, description="Viewers; empty = everyone with the app")
    auto_update: bool = True
    run_as: Optional[str] = Field(default=None, description="Service account to run as (instead of you)")
    env: Dict[str, str] = Field(default_factory=dict, description="Production variables over fdp.json's")
    secrets: Dict[str, str] = Field(default_factory=dict, description="Production secret references over fdp.json's")


class WorkloadPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: Optional[Literal["caller", "fixed"]] = None
    title: Optional[str] = Field(default=None, max_length=128)
    warehouse: Optional[str] = None
    allowed_groups: Optional[List[str]] = None
    auto_update: Optional[bool] = None
    run_as: Optional[str] = Field(default=None, description='Service account name, or "" to run as the deployer')
    env: Optional[Dict[str, str]] = None
    secrets: Optional[Dict[str, str]] = None


class OrchestrateIn(BaseModel):
    """From Airflow (or any execution / service token): run a program or a deployed program."""

    program: Optional[str] = Field(default=None, description="A program in the caller's own repo/workspace")
    workload: Optional[str] = Field(default=None, description="A deployed program, by its address name")
    args: Dict[str, Any] = Field(default_factory=dict)
    warehouse: Optional[str] = None

    @model_validator(mode="after")
    def _one(self) -> "OrchestrateIn":
        if bool(self.program) == bool(self.workload):
            raise ValueError("give exactly one of 'program' or 'workload'")
        return self


class WorkloadEventOut(ORM):
    at: datetime
    kind: str
    sha: Optional[str]
    message: str


class WorkloadOut(BaseModel):
    owner_group_id: Optional[str] = None
    owner_group_name: Optional[str] = None
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    execution_state: str = "on_demand"
    resource_id: Optional[str] = None
    folder_id: Optional[str] = None
    service_principal_id: Optional[str] = None
    execution_mode: str = "caller"
    actions: List[str] = Field(default_factory=list)
    id: str
    slug: str
    title: str
    kind: str
    runtime: str
    program: str
    repo: str
    branch: str
    owner: str
    allowed_groups: List[str]
    warehouse: str
    auto_update: bool
    status: str
    deployed_sha: Optional[str]
    deployed_commit: Dict[str, Any]
    deployed_at: Optional[datetime]
    latest_sha: Optional[str]
    last_checked_at: Optional[datetime]
    queue_position: Optional[int] = Field(default=None, description="Place in the warehouse queue while a deploy waits")
    queued_sha: Optional[str] = None
    sync_error: Optional[str]
    access_issue: Optional[Dict[str, Any]] = None
    program_def: Dict[str, Any]
    runtime_version: Optional[str] = None
    run_as: Optional[str] = None
    identity: str = ""
    env: Dict[str, str] = Field(default_factory=dict)
    secrets: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = Field(default=None, description="Apps and Airflow: the UI; dbt: the docs")
    can_manage: bool = False
    created_at: datetime
    events: Optional[List[WorkloadEventOut]] = None


class DeployedRunIn(BaseModel):
    args: Dict[str, Any] = Field(default_factory=dict)
    pool: Optional[str] = None


class OutputFile(BaseModel):
    path: str
    size: int


class OutputContent(BaseModel):
    path: str
    size: int
    truncated: bool
    content_base64: str


class OpenOut(BaseModel):
    url: str


class Me(BaseModel):
    principal_id: str = ""
    active_group_id: Optional[str] = None
    available_groups: List[Dict[str, Any]] = Field(default_factory=list)
    username: str
    groups: List[str]
    is_admin: bool
    data_sources: List[DataSourceOut]
    pools: List[PoolOut]
    features: Dict[str, bool] = Field(default_factory=dict, description='Optional parts of this installation, e.g. {"r": false}')


class ActiveGroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(min_length=1, max_length=12)


class ConnectOut(BaseModel):
    name: str
    kind: str
    config: Dict[str, object]
    credentials: Dict[str, str]
    access: Dict[str, Any] = Field(default_factory=dict)

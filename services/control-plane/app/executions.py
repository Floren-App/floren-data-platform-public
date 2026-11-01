"""Turns workspaces and runs into concrete container specs."""

from __future__ import annotations

import os
import re
from copy import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from fastapi import HTTPException

from .auth import mint_token
from .config import Settings
from .models import ComputePool, DataSource, Run, Workspace
from .orchestrator import VolumeMount, ExecutionSpec
from .programs import (APP_PORT, WORKSPACE, DbtSettings, Program, Scalar, argv_for, coerce_args,
                       fetch_source_step, is_safe_relpath, run_script)
from .programs import runtime_for as _runtime_for


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")[:40] or "user"


def home_volume(owner: str) -> str:
    return f"fdp-home-{slug(owner)}"


def safe_entrypoint(path: str) -> str:
    """Entrypoints are relative to /workspace and may not escape it."""
    if not is_safe_relpath(path):
        raise HTTPException(422, "entrypoint must be a relative path inside the workspace")
    return path


def runtime_for(entrypoint: str) -> str:
    rt = _runtime_for(entrypoint)
    if rt is None:
        raise HTTPException(422, f"unsupported file type: {entrypoint}")
    return rt


def _labels(owner: str, sources: List[DataSource], role: str) -> Dict[str, str]:
    labels = {"fdp.floren.app/owner": slug(owner), "fdp.floren.app/role": role}
    # NetworkPolicies allow egress to a data source only for pods carrying its label.
    for ds in sources:
        from .config import get_settings
        from .sql_access import SQL_KINDS
        if get_settings().query_executor == "trino" and ds.kind in SQL_KINDS:
            continue
        labels[f"fdp.floren.app/ds-{ds.name}"] = "true"
        from .layer_egress import source_label
        if ds.id:
            labels[source_label(ds.id)] = "true"
    return labels


def _env(s: Settings, owner: str, groups: List[str], workspace_id: str,
         workload_id: Optional[str] = None, ttl: Optional[int] = None, commit_sha: str = "", principal_id: str = "") -> Dict[str, str]:
    claims: Dict[str, object] = {"groups": [], "ws": workspace_id, "sha": commit_sha, "pid": principal_id or (owner[8:] if owner.startswith("service:") else "")}
    if workload_id:
        claims["pub"] = workload_id  # lets this execution download the deployed source
    token = mint_token("execution", owner, ttl or s.session_token_ttl_seconds, **claims)
    env = {
        "FDP_API": s.internal_url,
        "FDP_SESSION_TOKEN": token,
        "FDP_WORKSPACE_ID": workspace_id,
        "FDP_USER": owner,
    }
    if workload_id:
        env["FDP_WORKLOAD_ID"] = workload_id
    from .packages import package_key
    key = package_key("w", workspace_id) if workspace_id else package_key("p", workload_id) if workload_id else ""
    env.update(package_env(s, token, key))
    return env


def package_env(s: Settings, token: str, key: str) -> Dict[str, str]:
    """Installs come only from the platform's package repository (or an administrator's mirror); executions
    have no route to PyPI or CRAN. Reads use the execution's package key, never its token (installers print
    addresses in errors); publishing with twine or uv sends the token as the password."""
    from urllib.parse import urlsplit
    base = urlsplit(s.internal_url)
    repository = f"{base.scheme}://{base.netloc}/v1/packages/x/{key}"
    python, r = s.pip_index_url or f"{repository}/python/simple/", s.cran_mirror or f"{repository}/r"
    env = {"PIP_INDEX_URL": python, "UV_DEFAULT_INDEX": python, "FDP_CRAN": r, "RENV_CONFIG_REPOS_OVERRIDE": r,
           "TWINE_REPOSITORY_URL": f"{base.scheme}://{base.netloc}/v1/packages/python/legacy/",
           "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": token,
           "UV_PUBLISH_URL": f"{base.scheme}://{base.netloc}/v1/packages/python/legacy/",
           "UV_PUBLISH_USERNAME": "__token__", "UV_PUBLISH_PASSWORD": token}
    if base.scheme == "http" and not s.pip_index_url:  # in-cluster address; TLS ends at the gateway
        env["PIP_TRUSTED_HOST"] = env["UV_INSECURE_HOST"] = base.netloc
    return env


def proxy_env(s: Settings, workspace_id: str = "") -> Dict[str, str]:
    """Standard proxy variables (git, pip, R, the VS Code marketplace); platform addresses bypass it."""
    bypass = "localhost,127.0.0.1,.svc,.cluster.local"
    proxy = s.egress_proxy_url
    if workspace_id:
        from urllib.parse import urlsplit, urlunsplit
        from .auth import mint_token
        parts = urlsplit(proxy)
        ticket = mint_token("egress", workspace_id, 30 * 86400)
        proxy = urlunsplit((parts.scheme, "fdp:" + ticket + "@" + parts.netloc, parts.path, parts.query, parts.fragment))
    env = {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "NO_PROXY": bypass}
    return {**env, **{name.lower(): value for name, value in env.items()}}


def workspace_spec(s: Settings, ws: Workspace, groups: List[str], sources: List[DataSource],
                   size: Optional[tuple] = None, clone: Sequence[Tuple[str, str, str]] = (),
                   git_identity: Tuple[str, str] = ("", "")) -> ExecutionSpec:
    """size: (cpu, memory_gb) of the administrator's workspace warehouse; the Helm defaults otherwise.
    clone: (folder, branch, url) of repositories cloned at first start, with the person's own Git token."""
    env = _env(s, ws.owner, groups, ws.id, principal_id=ws.principal_id or "")
    # Git settings and saved credentials live on the volume, so they survive restarts.
    env["GIT_CONFIG_GLOBAL"] = f"{WORKSPACE}/.fdp/git/config"
    run_as_user: Optional[int] = 1000
    if ws.editor == "vscode":
        image, port = s.image_workspace_vscode, 8080
        # Downloads/uploads disabled: data stays on the server.
        command = ["--auth", "none", "--bind-addr", "0.0.0.0:8080",
                   "--disable-file-downloads", "--disable-file-uploads",
                   "--disable-telemetry", "/workspace"]
    elif ws.editor == "rstudio":
        image, port, command = s.image_workspace_rstudio, 8787, None
        env["DISABLE_AUTH"] = "true"  # the gateway authenticates every request
        run_as_user = None
    elif ws.editor == "sql":
        # CloudBeaver; it queries only through the SQL gateway, via a local proxy holding this token.
        image, port, command = s.image_workspace_sql, 8978, None
        run_as_user = None  # volume bootstrap drops to uid 1000 before starting the proxy/editor
        env["FDP_PUBLIC_URL"] = s.public_url
        env["FDP_SQL_USER"] = "fdp-" + (ws.principal_id or ws.id)
    else:
        raise HTTPException(422, f"unknown editor {ws.editor}")
    labels = {**_labels(ws.owner, sources, "workspace"), "fdp.floren.app/workspace": ws.id,
              **({"fdp.floren.app/environment": ws.environment_id, "fdp.floren.app/layer": ws.environment_id} if ws.environment_id else {})}
    if s.egress_proxy_url and ws.editor in ("vscode", "rstudio"):
        # The internet only through the allowlist proxy; the network lets these pods reach nothing else outside.
        env.update(proxy_env(s, ws.id))
        labels["fdp.floren.app/egress"] = "proxy"
    if clone and ws.editor in ("vscode", "rstudio"):
        env["FDP_GIT_CLONE"] = "\n".join("\t".join(repo) for repo in clone)
    if ws.editor in ("vscode", "rstudio"):   # who commits: from the workspace's Git credential
        for name, value in zip(("FDP_GIT_NAME", "FDP_GIT_EMAIL"), git_identity):
            if value:
                env[name] = value
    return ExecutionSpec(
        name=f"ws-{ws.id}",
        image=image,
        cpu=size[0] if size else s.workspace_cpu,
        memory_gb=size[1] if size else s.workspace_memory_gb,
        port=port,
        command=command,
        env=env,
        labels=labels,
        home_volume=home_volume(ws.volume_owner or ws.owner),
        run_as_user=run_as_user,
    )


@dataclass
class RunPlan:
    """Everything needed to start a run, resolved from fdp.json or an ad-hoc request."""

    kind: str
    entrypoint: str
    runtime: str
    argv: List[str] = field(default_factory=list)
    args: Dict[str, Scalar] = field(default_factory=dict)
    requirements: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    label: str = ""
    workdir: str = WORKSPACE                 # folder of the fdp.json (a cloned repo, or the root)
    workload_id: Optional[str] = None     # set: code comes from the deployed commit, not a workspace
    commit_sha: Optional[str] = None
    image_kind: str = "python"               # runtime catalog: python | r | dbt | airflow
    version: Optional[str] = None            # requested version (None = the catalog default)
    home_owner: Optional[str] = None
    image: str = ""                          # resolved from the catalog before starting
    dbt: Optional[DbtSettings] = None
    secret_refs: Dict[str, str] = field(default_factory=dict)   # env var -> secret reference
    secret_env: Dict[str, str] = field(default_factory=dict)    # resolved values (never stored)
    secret_paths: List[str] = field(default_factory=list)


def plan_from_program(program: Program, values: Dict[str, object], granted: Set[str]) -> RunPlan:
    """Validates argument values (raises ArgError) and turns a program into a run plan."""
    args = coerce_args(program, values, granted)
    return RunPlan(
        kind=program.kind, entrypoint=program.entrypoint, runtime=program.runtime or runtime_for(program.entrypoint),
        argv=argv_for(program, args), args=args, requirements=program.requirements, env=dict(program.env),
        image_kind=program.image_kind, version=program.version, dbt=program.dbt, secret_refs=dict(program.secrets),
    )


def with_workload_settings(plan: RunPlan, env: Dict[str, str], secrets: Dict[str, str]) -> RunPlan:
    """A workload's production environment overrides fdp.json's."""
    plan.env = {**plan.env, **env}
    plan.secret_refs = {**plan.secret_refs, **secrets}
    return plan


def dev_schema(settings: DbtSettings, owner: str) -> str:
    return settings.dev_schema.replace("{user}", slug(owner).replace("-", "_"))


def _kind_env(s: Settings, plan: RunPlan, owner: str, deployed: bool) -> Dict[str, str]:
    env = {"FDP_PUBLIC_URL": s.public_url}
    if plan.dbt:
        # Workspace runs build in your own dev schema; deployed runs in the production schema.
        env.update(
            FDP_DBT_SOURCE=plan.dbt.data_source,
            FDP_DBT_TARGET="prod" if deployed else "dev",
            FDP_DBT_SCHEMA=plan.dbt.schema_ if deployed else dev_schema(plan.dbt, owner),
            FDP_DBT_THREADS=str(plan.dbt.threads),
        )
    return env


# Inside deployed executions: their own folder of the data volume, and the per-workload env cache.
PUB_RUN_DIR = "/fdp/run"
PUB_ENVS_DIR = "/fdp/envs"
PUB_SOURCE_DIR = "/app"


def prepare_data_dirs(s: Settings, *subpaths: str) -> None:
    """Docker/Kubernetes sub-path mounts need the folders to exist; executions run as uid 1000."""
    for sub in subpaths:
        path = os.path.join(s.data_dir, sub)
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o777)


def deployed_mounts(s: Settings, workload_id: str, run_subpath: Optional[str]) -> List[VolumeMount]:
    mounts = [VolumeMount(s.data_volume, PUB_ENVS_DIR, f"envs/{workload_id}")]
    if run_subpath:
        mounts.append(VolumeMount(s.data_volume, PUB_RUN_DIR, run_subpath))
    return mounts


def run_spec(s: Settings, run: Run, pool: ComputePool, plan: RunPlan, groups: List[str],
             sources: List[DataSource]) -> ExecutionSpec:
    label = plan.label or f"{plan.kind} {plan.entrypoint} on warehouse {pool.name}"
    common = dict(run_id=run.id, label=label, kind=plan.kind, runtime=plan.runtime, entrypoint=plan.entrypoint,
                  argv=plan.argv, args=plan.args, requirements=plan.requirements)
    deployed = bool(plan.workload_id)
    if deployed:
        prepare_data_dirs(s, f"runs/{run.id}", f"envs/{plan.workload_id}")
        script = run_script(**common, run_dir=PUB_RUN_DIR, envs_dir=PUB_ENVS_DIR, workdir=PUB_SOURCE_DIR,
                            base_path=f"/apps/{run.id}",
                            prelude=fetch_source_step(plan.workload_id, plan.commit_sha or ""))
        mounts = deployed_mounts(s, plan.workload_id, f"runs/{run.id}")
        env = _env(s, run.owner, groups, "", workload_id=plan.workload_id)
        if plan.dbt:
            # Deployed dbt runs refresh the workload's docs (lineage) at /p/<slug>/.
            prepare_data_dirs(s, f"dbt-docs/{plan.workload_id}")
            mounts.append(VolumeMount(s.data_volume, PUB_DOCS_DIR, f"dbt-docs/{plan.workload_id}"))
            env["FDP_DOCS_DIR"] = PUB_DOCS_DIR
        storage = dict(mounts=mounts, working_dir="/tmp")
    else:
        script = run_script(**common, workdir=plan.workdir)
        storage = dict(home_volume=home_volume(plan.home_owner or run.owner))
        env = _env(s, run.owner, groups, run.workspace_id or "")
    env["FDP_SESSION_TOKEN"] = mint_token("execution", run.owner, s.session_token_ttl_seconds,
        groups=[], ws=run.workspace_id or "", pub=plan.workload_id or "", run=run.id, sha=plan.commit_sha or "", pid=run.principal_id or "")
    env["FDP_RUN_ID"] = run.id  # non-secret correlation ID for data/process provenance
    from .packages import package_key
    env.update(package_env(s, env["FDP_SESSION_TOKEN"], package_key("r", run.id)))  # installs follow the run
    return ExecutionSpec(
        name=f"run-{run.id}",
        image=plan.image,
        cpu=pool.cpu,
        memory_gb=pool.memory_gb,
        gpu=pool.gpu,
        port=APP_PORT if plan.kind in ("app", "airflow") else None,
        command=["bash", "-c", script],
        env={**plan.env, **_kind_env(s, plan, run.owner, deployed), **env},
        secret_env=plan.secret_env,
        labels={**_labels(run.owner, sources, "compute"), "fdp.floren.app/pool": pool.name,
                **({"fdp.floren.app/workspace":run.workspace_id} if run.workspace_id else {}),
                **({"fdp.floren.app/environment": run.environment_id, "fdp.floren.app/layer": run.environment_id} if run.environment_id else {})},
        # The Airflow image supports any uid with group 0 (its files are group-writable).
        run_as_group=0 if plan.kind == "airflow" else None,
        **storage,
    )


PUB_DOCS_DIR = "/fdp/docs"
PUB_AIRFLOW_HOME = "/fdp/airflow"
PUB_AIRFLOW_LOGS = "/fdp/airflow-logs"


def airflow_distributed_specs(s: Settings, pub_id: str, slug: str, sha: str, program: Program,
                              pool: ComputePool, worker_pool: ComputePool, image: str, owner: str,
                              owner_groups: List[str], sources: List[DataSource], db_url: str,
                              keys: Dict[str, str], extra_env: Optional[Dict[str, str]] = None,
                              secret_env: Optional[Dict[str, str]] = None,
                              broker_password: Optional[str] = None) -> List[ExecutionSpec]:
    """Broker + scheduler + webserver + N Celery workers for one deployment at one commit.

    All components share the metadata database, Fernet key and a task-log folder on the data volume
    (so the web UI shows logs of every worker, on Docker and Kubernetes alike).
    """
    cfg = program.airflow_settings
    assert cfg is not None
    prepare_data_dirs(s, f"envs/{pub_id}", f"airflow/{pub_id}/logs")
    broker = f"pub-{pub_id}-broker"
    base = deployed_service_spec(s, pub_id, slug, sha, program, pool, image, owner, owner_groups, sources,
                                  extra_env=extra_env, secret_env=secret_env)
    base.mounts = [m for m in base.mounts if m.target != PUB_AIRFLOW_HOME]
    base.mounts.append(VolumeMount(s.data_volume, PUB_AIRFLOW_LOGS, f"airflow/{pub_id}/logs"))
    base.env.pop("FDP_AIRFLOW_HOME", None)
    base.env.update({
        "AIRFLOW__CORE__EXECUTOR": "CeleryExecutor",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE": "2",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_MAX_OVERFLOW": "1",
        "AIRFLOW__CORE__PARALLELISM": str(cfg.parallelism),
        "AIRFLOW__CELERY__WORKER_CONCURRENCY": str(cfg.worker_concurrency),
        **({} if broker_password else {"AIRFLOW__CELERY__BROKER_URL": f"redis://{broker}:6379/0"}),
        "AIRFLOW__LOGGING__BASE_LOG_FOLDER": PUB_AIRFLOW_LOGS,
    })
    base.secret_env = {
        **base.secret_env,
        **({"AIRFLOW__CELERY__BROKER_URL": f"redis://:{quote(broker_password, safe='')}@{broker}:6379/0"}
           if broker_password else {}),
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": db_url,
        "AIRFLOW__CELERY__RESULT_BACKEND": "db+" + db_url.replace("+psycopg2", ""),
        "AIRFLOW__CORE__FERNET_KEY": keys["fernet_key"],
        "AIRFLOW__WEBSERVER__SECRET_KEY": keys["secret_key"],
    }

    def component(role: str, name: str, p: ComputePool, port: Optional[int]) -> ExecutionSpec:
        spec = ExecutionSpec(**{**base.__dict__})
        spec.name, spec.port = name, port
        spec.cpu, spec.memory_gb, spec.gpu = p.cpu, p.memory_gb, p.gpu
        spec.env = {**base.env, "FDP_AIRFLOW_ROLE": role}
        spec.labels = {**base.labels, "fdp.floren.app/airflow-role": role, "fdp.floren.app/pool": p.name}
        spec.mounts = [copy(m) for m in base.mounts]  # placement rewrites each component's mounts
        return spec

    tag = f"pub-{pub_id}-{sha[:7]}"
    specs = [
        component("web", f"{tag}-web", pool, APP_PORT),          # first: the one the gateway routes to
        component("scheduler", f"{tag}-scheduler", pool, None),
        *[component("worker", f"{tag}-worker-{i + 1}", worker_pool, None) for i in range(cfg.workers)],
    ]
    return specs


def airflow_broker_spec(s: Settings, pub_id: str, password: Optional[str] = None) -> ExecutionSpec:
    """Celery broker of one deployment. Not tied to a commit: it survives redeploys.

    In a managed layer it requires a password: other deployments share the layer's network boundary,
    and an open broker would let them queue tasks that run as this deployment's identity."""
    spec = ExecutionSpec(
        name=f"pub-{pub_id}-broker", image=s.image_airflow_broker, cpu=0.25, memory_gb=0.25, port=6379,
        command=["valkey-server", "--save", "", "--appendonly", "no"],
        labels={"fdp.floren.app/managed": "true", "fdp.floren.app/role": "broker", "fdp.floren.app/workload": pub_id},
        run_as_user=999,
    )
    if password:
        spec.command = ["sh", "-c", 'exec valkey-server --save "" --appendonly no --requirepass "$BROKER_PASSWORD"']
        spec.secret_env = {"BROKER_PASSWORD": password}
    return spec


def airflow_metadata_spec(s: Settings, pub_id: str, password: str) -> ExecutionSpec:
    """A distributed deployment's own metadata database inside its managed layer.

    Its data lives on the layer's artifact volume, so it survives redeploys and restarts like the broker."""
    return ExecutionSpec(
        name=f"pub-{pub_id}-metadata", image=s.image_airflow_metadata, cpu=0.25, memory_gb=0.5, port=5432,
        env={"PGDATA": "/var/lib/postgresql/data/pgdata", "POSTGRES_USER": "airflow", "POSTGRES_DB": "airflow"},
        secret_env={"POSTGRES_PASSWORD": password},
        labels={"fdp.floren.app/managed": "true", "fdp.floren.app/role": "airflow-metadata", "fdp.floren.app/workload": pub_id},
        mounts=[VolumeMount(s.data_volume, "/var/lib/postgresql/data", f"airflow/{pub_id}/metadata")],
    )


def deployed_service_spec(s: Settings, pub_id: str, slug: str, sha: str, program: Program, pool: ComputePool,
                           image: str, owner: str, owner_groups: List[str], sources: List[DataSource],
                           extra_env: Optional[Dict[str, str]] = None,
                           secret_env: Optional[Dict[str, str]] = None) -> ExecutionSpec:
    """A long-running deployed app or Airflow deployment at a commit, served at /p/<slug>/.

    `owner` is the identity it acts as: the deployer, or a service account ("sa:<name>").
    Airflow: the runs its DAGs start run as that identity too.
    """
    prepare_data_dirs(s, f"envs/{pub_id}")
    mounts = deployed_mounts(s, pub_id, None)
    env = {**program.env, **(extra_env or {}), "FDP_PUBLIC_URL": s.public_url,
           # Long-lived: the GitOps loop redeploys well before this token expires.
           **_env(s, owner, owner_groups, "", workload_id=pub_id, ttl=30 * 24 * 3600, commit_sha=sha)}
    if program.kind == "airflow":
        # DAG run history survives redeploys.
        prepare_data_dirs(s, f"airflow/{pub_id}")
        mounts.append(VolumeMount(s.data_volume, PUB_AIRFLOW_HOME, f"airflow/{pub_id}"))
        env["FDP_AIRFLOW_HOME"] = PUB_AIRFLOW_HOME
        env["FDP_AIRFLOW_LOCAL_PARALLELISM"] = str(program.airflow_settings.local_parallelism)
    script = run_script(
        run_id=f"{pub_id}-{sha[:7]}", label=f"deployed {program.kind} {slug} at {sha[:12]} on warehouse {pool.name}",
        kind=program.kind, runtime=program.runtime or "python", entrypoint=program.entrypoint, argv=[], args={},
        requirements=program.requirements, run_dir="/tmp/fdp-run", envs_dir=PUB_ENVS_DIR,
        workdir=PUB_SOURCE_DIR, base_path=f"/p/{slug}", prelude=fetch_source_step(pub_id, sha),
    )
    return ExecutionSpec(
        name=f"pub-{pub_id}-{sha[:7]}",
        image=image,
        cpu=pool.cpu, memory_gb=pool.memory_gb, gpu=pool.gpu, port=APP_PORT,
        command=["bash", "-c", script],
        env=env,
        secret_env=secret_env or {},
        labels={**_labels(owner, sources, "workload"), "fdp.floren.app/pool": pool.name, "fdp.floren.app/workload": pub_id},
        mounts=mounts,
        working_dir="/tmp",
        run_as_group=0 if program.kind == "airflow" else None,
    )

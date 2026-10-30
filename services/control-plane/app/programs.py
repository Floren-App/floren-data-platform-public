"""fdp.json: the per-workspace list of runnable programs, their arguments and requirements.

The same pydantic models validate the file at run time and generate the JSON Schema that
editors use for autocompletion (schemas/fdp.schema.json).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Annotated, Any, Dict, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

CONFIG_FILE = "fdp.json"
WORKSPACE = "/workspace"
APP_PORT = 8501

_SAFE_PATH = re.compile(r"^[\w\-./ ]+$")
SECRET_NAME = r"[A-Za-z0-9_.\-]{1,64}"
SECRET_REF = re.compile(rf"^(me|group:[\w.\-]{{1,128}}|sa:[a-z][a-z0-9\-]{{1,40}})/{SECRET_NAME}$")
_PROGRAM_NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ArgType = Literal["string", "text", "integer", "number", "boolean", "choice", "date", "data_source"]
Scalar = Union[bool, int, float, str]
LayerReference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


def is_safe_relpath(path: str) -> bool:
    """Relative to /workspace and unable to escape it."""
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and bool(_SAFE_PATH.match(path))


def runtime_for(entrypoint: str) -> Optional[str]:
    suffix = PurePosixPath(entrypoint).suffix
    if suffix in (".py", ".ipynb"):
        return "python"
    if suffix in (".R", ".r", ""):  # a bare directory is a Shiny app dir
        return "r"
    return None


class Arg(BaseModel):
    """One input of a program. The UI renders a matching form field."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="Identifier; also the key in fdp.args().")
    type: ArgType = Field(default="string", description="Form field type. data_source = pick one of your granted sources.")
    label: Optional[str] = Field(default=None, description="Label shown in the form (defaults to the name).")
    help: Optional[str] = Field(default=None, description="Hint shown under the field.")
    required: bool = False
    default: Optional[Scalar] = None
    choices: Optional[List[str]] = Field(default=None, description="Options for type=choice.")
    min: Optional[float] = Field(default=None, description="Minimum for integer/number.")
    max: Optional[float] = Field(default=None, description="Maximum for integer/number.")
    pattern: Optional[str] = Field(default=None, description="Regular expression a string must match.")
    flag: Optional[str] = Field(
        default=None, pattern=r"^--?[A-Za-z0-9][A-Za-z0-9_-]*$",
        description="Command-line flag, default --<name> with _ replaced by -. Booleans are passed as a bare flag when true.",
    )
    positional: bool = Field(default=False, description="Pass the value as a positional argument instead of a flag.")

    @model_validator(mode="after")
    def _check(self) -> "Arg":
        if self.type == "choice" and not self.choices:
            raise ValueError(f"argument {self.name!r}: type 'choice' needs 'choices'")
        if self.pattern:
            re.compile(self.pattern)
        return self

    @property
    def cli_flag(self) -> str:
        return self.flag or "--" + self.name.replace("_", "-")


VERSION = r"^[0-9][0-9A-Za-z.\-]{0,15}$"
RuntimeKind = Literal["python", "r", "dbt", "airflow"]


class DbtSettings(BaseModel):
    """Where a dbt project reads and writes. Credentials come from the data source (OpenBao)."""

    model_config = ConfigDict(extra="forbid")

    data_source: str = Field(description="Data source to build in (needs write access, e.g. finance-dw-transform).")
    schema_: str = Field(default="analytics", alias="schema", description="Production schema (deployed runs).")
    dev_schema: str = Field(default="dbt_{user}", description="Schema for workspace runs; {user} is your user name.")
    threads: int = Field(default=4, ge=1, le=64)


class AirflowSettings(BaseModel):
    """How an Airflow deployment runs."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["standalone", "distributed"] = Field(
        default="standalone",
        description="standalone: one container, LocalExecutor with PostgreSQL; SQLite/SequentialExecutor "
                    "only for local development. distributed: separate webserver, scheduler and Celery workers.")
    local_parallelism: int = Field(default=2, ge=1, le=32,
                                  description="Concurrent orchestration tasks in a single-container PostgreSQL deployment.")
    workers: int = Field(default=2, ge=1, le=32, description="Celery workers (distributed).")
    worker_concurrency: int = Field(default=8, ge=1, le=64, description="Tasks per worker at a time (distributed).")
    worker_warehouse: Optional[str] = Field(default=None, description="Warehouse for workers (default: the program's).")
    parallelism: int = Field(default=32, ge=1, le=512, description="Max running tasks across the deployment.")


def _dbt_args() -> List["Arg"]:
    """The run form every dbt program gets."""
    return [
        Arg(name="command", type="choice", positional=True, default="build", required=True,
            choices=["build", "run", "test", "seed", "snapshot", "docs", "freshness"], help="dbt command; freshness checks source timestamps; docs only regenerates the docs"),
        Arg(name="select", type="string", label="Select", help="e.g. tag:daily, +orders, path:models/marts"),
        Arg(name="exclude", type="string", label="Exclude"),
        Arg(name="full_refresh", type="boolean", label="Full refresh"),
        Arg(name="vars", type="text", label="Vars", help='YAML or JSON, e.g. {"start_date": "2026-01-01"}'),
    ]


class LineageReference(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source: str = Field(min_length=1, max_length=128)
    schema_: str = Field(alias="schema", min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=512)


class ProgramLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: List[LineageReference] = Field(default_factory=list, max_length=500)
    outputs: List[LineageReference] = Field(default_factory=list, max_length=500)


class Program(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    description: str = ""
    layer: Optional[LayerReference] = Field(default=None, description="Layer name or stable ID. Only available in a matching workspace/workload layer; never grants access. Inherits the file's layer when omitted.")
    lineage: Optional[ProgramLineage] = Field(default=None, description="Optional manual dependencies for access outside Floren Data Platform. SQL, managed API calls, SDK files, dbt and Airflow capture lineage automatically. Does not grant data access.")
    kind: Literal["script", "notebook", "app", "dbt", "airflow", "stream"] = Field(
        default="script",
        description="script = run to completion; notebook = execute with papermill; app = Streamlit/Shiny server; "
                    "dbt = a dbt project (entrypoint = folder with dbt_project.yml); "
                    "airflow = an Airflow deployment (entrypoint = folder with dags/); "
                    "stream = a continuous Python/R consumer with managed identity and readiness.")
    entrypoint: str = Field(description="File or folder, relative to the fdp.json folder.")
    runtime: Optional[Literal["python", "r"]] = Field(default=None, description="Inferred from the entrypoint when omitted.")
    python: Optional[str] = Field(default=None, pattern=VERSION, description="Python version from the runtime catalog, e.g. 3.12.")
    r: Optional[str] = Field(default=None, pattern=VERSION, description="R version from the runtime catalog, e.g. 4.4.")
    dbt_version: Optional[str] = Field(default=None, alias="dbt_version", pattern=VERSION,
                                       description="dbt version from the runtime catalog, e.g. 1.8.")
    airflow: Optional[str] = Field(default=None, pattern=VERSION, description="Airflow version from the runtime catalog, e.g. 2.10.")
    dbt: Optional[DbtSettings] = Field(default=None, description="Required for kind dbt.")
    airflow_settings: Optional[AirflowSettings] = Field(default=None, description="For kind airflow.")
    requirements: Optional[str] = Field(
        default=None,
        description="Python: a requirements.txt. R: a text file with one package per line, or an renv.lock. "
                    "Installed once into a cached environment and reused until the file changes.",
    )
    warehouse: Optional[str] = Field(default=None, description="Default warehouse (compute pool) for this program.")
    data_sources: List[str] = Field(default_factory=list, description="Data sources the program needs; checked before it starts.")
    env: Dict[str, str] = Field(default_factory=dict, description="Extra environment variables (no secret values here).")
    secrets: Dict[str, str] = Field(
        default_factory=dict,
        description='Environment variables filled from Floren Data Platform secrets: {"API_KEY": "<ref>"}, where <ref> is '
                    'me/<name> (yours or the service account running it), group:<group>/<name> or sa:<account>/<name>.')
    args: List[Arg] = Field(default_factory=list)

    @field_validator("entrypoint")
    @classmethod
    def _entry(cls, v: str) -> str:
        if not is_safe_relpath(v):
            raise ValueError("must be a relative path inside the workspace")
        return v

    @field_validator("requirements")
    @classmethod
    def _req(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not is_safe_relpath(v):
            raise ValueError("must be a relative path inside the workspace")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: Dict[str, str]) -> Dict[str, str]:
        for key in v:
            if not _ENV_NAME.match(key) or key.startswith("FDP_"):
                raise ValueError(f"invalid environment variable name {key!r}")
        return v

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, v: Dict[str, str]) -> Dict[str, str]:
        for key, ref in v.items():
            if not _ENV_NAME.match(key) or key.startswith("FDP_"):
                raise ValueError(f"invalid environment variable name {key!r}")
            if not SECRET_REF.match(ref):
                raise ValueError(f"{key}: secret reference must look like me/<name>, group:<group>/<name> or sa:<account>/<name>")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Program":
        if self.kind == "dbt":
            if self.dbt is None:
                raise ValueError("dbt programs need a 'dbt' section with at least 'data_source'")
            if not self.args:
                self.args = _dbt_args()
            if self.dbt.data_source not in self.data_sources:
                self.data_sources = [*self.data_sources, self.dbt.data_source]
            self.runtime = "python"
        elif self.dbt is not None:
            raise ValueError("'dbt' settings only apply to kind dbt")
        if self.kind == "airflow":
            self.runtime = "python"
            self.airflow_settings = self.airflow_settings or AirflowSettings()
        elif self.airflow_settings is not None:
            raise ValueError("'airflow_settings' only apply to kind airflow")
        names = [a.name for a in self.args]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate argument names: {', '.join(sorted(dupes))}")
        rt = self.runtime or runtime_for(self.entrypoint)
        if rt is None:
            raise ValueError(f"cannot tell the runtime of {self.entrypoint!r}; set 'runtime'")
        if self.kind == "notebook" and not self.entrypoint.endswith(".ipynb"):
            raise ValueError("notebook programs need an .ipynb entrypoint")
        if self.kind == "notebook" and rt == "r":
            raise ValueError("R notebooks are not supported yet; use an .R script")
        self.runtime = rt
        return self

    @property
    def image_kind(self) -> RuntimeKind:
        """Which runtime catalog provides this program's container image."""
        if self.kind in ("dbt", "airflow"):
            return self.kind  # type: ignore[return-value]
        return self.runtime or "python"  # type: ignore[return-value]

    @property
    def version(self) -> Optional[str]:
        return {"python": self.python, "r": self.r, "dbt": self.dbt_version, "airflow": self.airflow}[self.image_kind]


class WorkspaceConfig(BaseModel):
    """fdp.json — programs this workspace can run on a warehouse."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="Floren Data Platform workspace config")

    schema_: Optional[str] = Field(default=None, alias="$schema")
    version: Literal[1] = 1
    warehouse: Optional[str] = Field(default=None, description="Default warehouse for programs that don't set one.")
    layer: Optional[LayerReference] = Field(default=None, description="Default layer name or stable ID for programs in this file. Programs can override it. Omitted means use the workspace/workload layer.")
    # Runtime versions for every program in this file that doesn't choose its own.
    python: Optional[str] = Field(default=None, pattern=VERSION, description="Default Python version.")
    r: Optional[str] = Field(default=None, pattern=VERSION, description="Default R version.")
    dbt_version: Optional[str] = Field(default=None, pattern=VERSION, description="Default dbt version.")
    airflow: Optional[str] = Field(default=None, pattern=VERSION, description="Default Airflow version.")
    programs: Dict[str, Program] = Field(default_factory=dict, description="Keyed by program name (lowercase, dashes).")

    @field_validator("programs")
    @classmethod
    def _names(cls, v: Dict[str, Program]) -> Dict[str, Program]:
        for name in v:
            if not re.match(_PROGRAM_NAME, name):
                raise ValueError(f"invalid program name {name!r} (lowercase letters, digits, - and _)")
        return v


def json_schema() -> Dict[str, Any]:
    schema = WorkspaceConfig.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "fdp.schema.json"
    return schema


def _loc(loc: Tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def parse_config(text: str) -> Tuple[Optional[WorkspaceConfig], List[str]]:
    """Returns the config, or None plus human-readable errors."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"{CONFIG_FILE} line {exc.lineno}, column {exc.colno}: {exc.msg}"]
    try:
        return WorkspaceConfig.model_validate(data), []
    except ValidationError as exc:
        return None, [f"{_loc(e['loc']) or CONFIG_FILE}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()]


def _format(exc: ValidationError) -> List[str]:
    return [f"{_loc(e['loc']) or CONFIG_FILE}: {e['msg'].removeprefix('Value error, ')}" for e in exc.errors()]


def parse_programs(text: str) -> Tuple[Optional[str], Dict[str, Program], List[str]]:
    """Like parse_config, but validates each program on its own: one broken program is reported
    without making the others unusable. Returns (default warehouse, valid programs, errors)."""
    cfg, errors = parse_config(text)
    if cfg is not None:
        return cfg.warehouse, _with_defaults(cfg, cfg.programs), []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, {}, errors
    if not isinstance(data, dict):
        return None, {}, errors
    raw_programs = data.get("programs") if isinstance(data.get("programs"), dict) else {}
    top = {k: v for k, v in data.items() if k != "programs"}
    try:
        base = WorkspaceConfig.model_validate({**top, "programs": {}})
    except ValidationError:
        return None, {}, errors      # the file itself is broken, not just a program
    good: Dict[str, Program] = {}
    errors = []
    for name, raw in raw_programs.items():
        try:
            good.update(WorkspaceConfig.model_validate({**top, "programs": {name: raw}}).programs)
        except ValidationError as exc:
            errors += _format(exc)
    return base.warehouse, _with_defaults(base, good), errors


def _with_defaults(cfg: WorkspaceConfig, programs: Dict[str, Program]) -> Dict[str, Program]:
    """File-level layer/runtime defaults apply to programs that don't pick their own."""
    for p in programs.values():
        p.layer = p.layer or cfg.layer
        p.python = p.python or cfg.python
        p.r = p.r or cfg.r
        p.dbt_version = p.dbt_version or cfg.dbt_version
        p.airflow = p.airflow or cfg.airflow
    return programs


@dataclass
class Resolved:
    """A program plus the folder (relative to the workspace or repo root) its fdp.json is in."""

    folder: str
    program: Program


def merge_configs(files: Dict[str, str]) -> Tuple[Dict[str, Resolved], Optional[str], List[str]]:
    """Combines fdp.json files keyed by folder ("" = root, "sales-repo" = a cloned repo).

    Programs from a folder are named "<folder>/<name>"; each one's warehouse falls back to its own
    file's default. One broken file only reports errors; programs from the others stay usable.
    """
    programs: Dict[str, Resolved] = {}
    errors: List[str] = []
    root_warehouse: Optional[str] = None
    for folder in sorted(files):
        warehouse, valid, errs = parse_programs(files[folder])
        where = f"{folder}/{CONFIG_FILE}" if folder else CONFIG_FILE
        for e in errs:
            # Errors either name the file already ("fdp.json line 3 ...") or a field.
            errors.append(e.replace(CONFIG_FILE, where, 1) if e.startswith(CONFIG_FILE) else f"{where}: {e}")
        if folder == "":
            root_warehouse = warehouse
        for name, prog in valid.items():
            if prog.warehouse is None:
                prog.warehouse = warehouse
            programs[f"{folder}/{name}" if folder else name] = Resolved(folder, prog)
    return programs, root_warehouse, errors


class ArgError(ValueError):
    def __init__(self, errors: Dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def coerce_args(program: Program, values: Dict[str, Any], granted: Set[str]) -> Dict[str, Scalar]:
    """Validates submitted form values against the program's argument definitions."""
    defs = {a.name: a for a in program.args}
    errors: Dict[str, str] = {k: "unknown argument" for k in values if k not in defs}
    out: Dict[str, Scalar] = {}
    for a in program.args:
        raw = values.get(a.name, a.default)
        if raw is None or raw == "":
            if a.required:
                errors[a.name] = "required"
            continue
        try:
            out[a.name] = _coerce(a, raw, granted)
        except (TypeError, ValueError) as exc:
            errors[a.name] = str(exc)
    if errors:
        raise ArgError(errors)
    return out


def _coerce(a: Arg, raw: Any, granted: Set[str]) -> Scalar:
    if a.type == "boolean":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes"):
            return True
        if str(raw).lower() in ("false", "0", "no"):
            return False
        raise ValueError("must be true or false")
    if a.type in ("integer", "number"):
        if isinstance(raw, bool):
            raise ValueError(f"must be a{'n integer' if a.type == 'integer' else ' number'}")
        v: Union[int, float]
        try:
            v = int(raw) if a.type == "integer" else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"must be a{'n integer' if a.type == 'integer' else ' number'}") from None
        if a.type == "integer" and isinstance(raw, float) and not raw.is_integer():
            raise ValueError("must be an integer")
        if a.min is not None and v < a.min:
            raise ValueError(f"must be at least {a.min:g}")
        if a.max is not None and v > a.max:
            raise ValueError(f"must be at most {a.max:g}")
        return v
    value = str(raw)
    if a.type == "choice" and value not in (a.choices or []):
        raise ValueError(f"must be one of {', '.join(a.choices or [])}")
    if a.type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("must be a date (YYYY-MM-DD)") from None
    if a.type == "data_source" and value not in granted:
        raise ValueError(f"data source {value!r} is not available to you")
    if a.pattern and not re.fullmatch(a.pattern, value):
        raise ValueError(f"must match {a.pattern}")
    return value


def _str(v: Scalar) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def argv_for(program: Program, args: Dict[str, Scalar]) -> List[str]:
    argv: List[str] = []
    for a in program.args:
        if a.name not in args:
            continue
        v = args[a.name]
        if a.positional:
            argv.append(_str(v))
        elif a.type == "boolean":
            if v:
                argv.append(a.cli_flag)
        else:
            argv += [a.cli_flag, _str(v)]
    return argv


# --- the shell script a compute container runs -------------------------------------------

_R_INSTALL = """
a <- commandArgs(TRUE); req <- a[1]; lib <- a[2]
.libPaths(c(lib, .libPaths()))
repos <- Sys.getenv("FDP_CRAN", unset = "https://cloud.r-project.org")
if (basename(req) == "renv.lock") {
  if (!requireNamespace("renv", quietly = TRUE)) install.packages("renv", lib = lib, repos = repos)
  renv::restore(lockfile = req, library = lib, prompt = FALSE)
} else {
  pk <- trimws(sub("#.*", "", readLines(req)))
  pk <- pk[nzchar(pk)]
  miss <- setdiff(pk, rownames(installed.packages()))
  if (length(miss)) install.packages(miss, lib = lib, repos = repos, Ncpus = parallel::detectCores())
  bad <- setdiff(pk, rownames(installed.packages()))
  if (length(bad)) stop("could not install: ", paste(bad, collapse = ", "))
}
"""


def _setup_requirements(runtime: str, req: str, envs_dir: str) -> str:
    q = shlex.quote(req)
    if runtime == "python":
        fingerprint = f'H=$( (cat {q}; python -V) | sha256sum | cut -c1-16 ); ENV="$ENVS/py-$H"'
        build = (f'rm -rf "$ENV" && python -m venv --system-site-packages "$ENV" '
                 f'&& "$ENV/bin/pip" install --disable-pip-version-check -r {q} && touch "$ENV/.ready"')
        activate = '. "$ENV/bin/activate"'
    else:
        fingerprint = f'H=$( (cat {q}; R --version | head -1) | sha256sum | cut -c1-16 ); ENV="$ENVS/r-$H"'
        build = f'mkdir -p "$ENV" && Rscript -e {shlex.quote(_R_INSTALL.strip())} {q} "$ENV" && touch "$ENV/.ready"'
        activate = 'export R_LIBS_USER="$ENV"'
    return f"""
  if [ ! -f {q} ]; then echo "[fdp] requirements file {req} not found"; return 97; fi
  ENVS={shlex.quote(envs_dir)}; mkdir -p "$ENVS"
  {fingerprint}
  (
    flock 9
    if [ -f "$ENV/.ready" ]; then
      echo "[fdp] using cached environment for {req} ($H)"
    else
      echo "[fdp] installing {req} into a cached environment ($H), first run only..."
      {build}
    fi
  ) 9>"$ENV.lock" || {{ echo "[fdp] installing {req} failed"; return 97; }}
  {activate}"""


_DBT = """
  cd {project} || return 1
  export DBT_PROFILES_DIR=/tmp/dbt-profiles   # credentials stay in the container, never on a volume
  mkdir -p "$DBT_PROFILES_DIR"
  fdp dbt-profile > "$DBT_PROFILES_DIR/profiles.yml" || {{ echo "[fdp] could not build profiles.yml"; return 95; }}
  printf '[fdp] dbt target %s, schema %s\\n' "$FDP_DBT_TARGET" "$FDP_DBT_SCHEMA"
  if [ -f packages.yml ] || [ -f dependencies.yml ]; then dbt deps || return $?; fi
  {main}
  status=$?
  # Preserve the command's results: docs generation can overwrite run_results.json.
  [ ! -f target/run_results.json ] || cp target/run_results.json "$FDP_OUTPUT_DIR/run_results.json"
  # Docs (with lineage) after every run, also failed ones: they show what exists now.
  echo "[fdp] generating dbt docs"
  dbt docs generate --static --target "$FDP_DBT_TARGET" >/dev/null 2>&1 || echo "[fdp] dbt docs generate failed"
  mkdir -p "$FDP_OUTPUT_DIR/docs"
  for f in target/static_index.html; do [ -f "$f" ] && cp "$f" "$FDP_OUTPUT_DIR/docs/index.html"; done
  for f in target/manifest.json target/catalog.json target/sources.json; do [ -f "$f" ] && cp "$f" "$FDP_OUTPUT_DIR/"; done
  if [ -n "${{FDP_WORKLOAD_ID:-}}" ]; then
    python -m fdp.lineage dbt --target-dir target --results "$FDP_OUTPUT_DIR/run_results.json" || echo "[fdp] catalog metadata upload failed; run status is unchanged"
  fi
  if [ -n "${{FDP_DOCS_DIR:-}}" ] && [ -f target/static_index.html ]; then
    cp target/static_index.html "$FDP_DOCS_DIR/index.html.new" && mv "$FDP_DOCS_DIR/index.html.new" "$FDP_DOCS_DIR/index.html"
  fi
  return $status"""

_AIRFLOW = """
  export AIRFLOW_HOME="${{FDP_AIRFLOW_HOME:-/tmp/airflow}}"
  mkdir -p "$AIRFLOW_HOME"
  export AIRFLOW__CORE__DAGS_FOLDER="$PWD/{project}/dags"
  export AIRFLOW__CORE__LOAD_EXAMPLES=False
  if [ -n "${{FDP_WORKLOAD_ID:-}}" ] && python -c 'import fdp.airflow_lineage' >/dev/null 2>&1; then
    export AIRFLOW__LINEAGE__BACKEND=fdp.airflow_lineage.FDPLineageBackend
    export FDP_AIRFLOW_LINEAGE=1
  else
    echo "[fdp] update the Airflow runtime SDK to enable native catalog lineage"
  fi
  export AIRFLOW__WEBSERVER__WEB_SERVER_PORT={port}
  export AIRFLOW__WEBSERVER__BASE_URL="$FDP_PUBLIC_URL{base}"
  export AIRFLOW__WEBSERVER__ENABLE_PROXY_FIX=True
  export AIRFLOW__WEBSERVER__EXPOSE_CONFIG=False
  # No Airflow login: the Floren Data Platform gateway already decides who may open this deployment.
  printf 'AUTH_ROLE_PUBLIC = "Admin"\\n' > "$AIRFLOW_HOME/webserver_config.py"
  wait_for_db() {{ until airflow db check-migrations -t 0 >/dev/null 2>&1; do echo "[fdp] waiting for the metadata database..."; sleep 5; done; }}
  role="${{FDP_AIRFLOW_ROLE:-standalone}}"
  start_catalog_lineage() {{
    if [ "${{FDP_AIRFLOW_LINEAGE:-}}" = 1 ]; then
      python -m fdp.airflow_lineage --watch &
      catalog_lineage_pid=$!
    fi
  }}
  echo "[fdp] airflow $role"
  case "$role" in
    standalone)
      database="${{AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-${{AIRFLOW__CORE__SQL_ALCHEMY_CONN:-}}}}"
      case "$database" in
        postgres://*|postgresql://*|postgresql+*)
          export AIRFLOW__CORE__EXECUTOR=LocalExecutor
          export AIRFLOW__CORE__PARALLELISM="${{FDP_AIRFLOW_LOCAL_PARALLELISM:-2}}"
          export AIRFLOW__WEBSERVER__WORKERS="${{AIRFLOW__WEBSERVER__WORKERS:-1}}"
          export AIRFLOW__SCHEDULER__PARSING_PROCESSES="${{AIRFLOW__SCHEDULER__PARSING_PROCESSES:-1}}"
          ;;
        *)
          export AIRFLOW__CORE__EXECUTOR=SequentialExecutor
          echo "[fdp] SQLite/SequentialExecutor is for local development only; configure PostgreSQL for production"
          ;;
      esac
      # AIRFLOW_HOME persists across pods; PID numbers do not. A stale webserver
      # PID can collide with an unrelated process in the replacement container.
      rm -f "$AIRFLOW_HOME/airflow-webserver.pid" "$AIRFLOW_HOME/airflow-scheduler.pid" "$AIRFLOW_HOME/airflow-triggerer.pid"
      airflow db migrate >/dev/null || return $?
      start_catalog_lineage
      if [ "$AIRFLOW__CORE__EXECUTOR" = SequentialExecutor ]; then exec airflow standalone; fi
      # Run the normal services, rather than Airflow's development-only standalone command.
      # Stop siblings if any service exits, so Kubernetes can restart the whole deployment.
      children="${{catalog_lineage_pid:-}}"
      stop_airflow() {{
        trap - TERM INT EXIT
        kill $children 2>/dev/null || true
        wait $children 2>/dev/null || true
      }}
      trap 'stop_airflow; exit 0' TERM INT
      trap stop_airflow EXIT
      for component in scheduler webserver triggerer; do
        airflow "$component" &
        children="$children $!"
      done
      while true; do
        for child in $children; do
          if ! kill -0 "$child" 2>/dev/null; then
            echo "[fdp] Airflow component exited; restarting deployment"
            exit 1
          fi
        done
        sleep 2 & wait $!
      done ;;
    scheduler)   # the only component that migrates the metadata database
      airflow db migrate >/dev/null || return $?
      start_catalog_lineage
      airflow triggerer &
      exec airflow scheduler ;;
    web)
      wait_for_db; exec airflow webserver ;;
    worker)
      wait_for_db; exec airflow celery worker ;;
  esac"""


def program_command(kind: str, runtime: str, entrypoint: str, argv: List[str], args: Dict[str, Scalar],
                    base_path: str) -> List[str]:
    e = shlex.quote(entrypoint)
    rest = " ".join(shlex.quote(a) for a in argv)
    if kind == "dbt":
        command = str(args.get("command", "build"))
        flags = " ".join(shlex.quote(a) for a in argv[1:])  # argv[0] is the command
        main = ('dbt docs generate --static --target "$FDP_DBT_TARGET"' if command == "docs"
                else f'dbt {"source freshness" if command == "freshness" else shlex.quote(command)} --target "$FDP_DBT_TARGET" {flags}')
        return [_DBT.format(project=e, main=main)]
    if kind == "airflow":
        return [_AIRFLOW.format(project=e, port=APP_PORT, base=base_path.rstrip("/"))]
    if kind == "stream":
        command = f"python {e} {rest}" if runtime == "python" else f"Rscript {e} {rest}"
        return [f"python -m fdp.streaming -- {command}"]
    if kind == "script":
        return [f"python {e} {rest}" if runtime == "python" else f"Rscript {e} {rest}"]
    if kind == "notebook":
        # The executed notebook (with all cell outputs) lands in the run's outputs.
        out = shlex.quote(PurePosixPath(entrypoint).name)
        return [f'papermill {e} "$FDP_OUTPUT_DIR"/{out} -y {shlex.quote(json.dumps(args))} --log-output']
    if runtime == "python":
        return [f"streamlit run {e} --server.port={APP_PORT} --server.address=0.0.0.0 --server.headless=true "
                f"--server.baseUrlPath={base_path} -- {rest}"]
    shiny = f"shiny::runApp('{entrypoint}', host='0.0.0.0', port={APP_PORT}, launch.browser=FALSE)"
    return [f"R -e {shlex.quote(shiny)}"]


def fetch_source_step(workload_id: str, sha: str, dest: str = "/app") -> str:
    """Shell step: download the deployed commit from the control plane (git stays server-side)."""
    url = f'"$FDP_API/v1/workloads/{workload_id}/source?sha={sha}"'
    return f"""
  printf '[fdp] fetching source at commit %s\\n' {sha[:12]}
  mkdir -p {dest} && cd {dest} || return 1
  curl -fsS -H "Authorization: Bearer $FDP_SESSION_TOKEN" {url} | tar -xz -C {dest} \\
    || {{ echo "[fdp] could not fetch the deployed source"; return 96; }}"""


def run_script(*, run_id: str, label: str, kind: str, runtime: str, entrypoint: str,
               argv: List[str], args: Dict[str, Scalar], requirements: Optional[str],
               run_dir: Optional[str] = None, envs_dir: str = f"{WORKSPACE}/.fdp/envs",
               workdir: str = WORKSPACE, base_path: Optional[str] = None, prelude: str = "") -> str:
    """Bash script for a run: optional source fetch and requirements setup, the program, console
    tee'd to run_dir/console.log, outputs in run_dir/outputs."""
    run_dir = run_dir or f"{WORKSPACE}/.fdp/runs/{run_id}"
    setup = _setup_requirements(runtime, requirements, envs_dir) if requirements else ""
    (cmd,) = program_command(kind, runtime, entrypoint, argv, args, base_path or f"/apps/{run_id}")
    # Multi-step programs (dbt, airflow) are shell snippets; single commands replace the shell (exec)
    # so the stop signal reaches the program directly.
    if kind in ("dbt", "airflow"):
        shown = f"{kind} {' '.join(argv) or entrypoint}"
        body = cmd
    else:
        shown, body = cmd, f"exec {cmd}"
    return f"""set -u
RUN_DIR={shlex.quote(run_dir)}
mkdir -p "$RUN_DIR/outputs"
export FDP_RUN_ID={run_id}
export FDP_OUTPUT_DIR="$RUN_DIR/outputs"
export FDP_ARGS={shlex.quote(json.dumps(args))}
export PYTHONUNBUFFERED=1
main() {{
  printf '[fdp] %s\\n' {shlex.quote(label)}{prelude}
  cd {shlex.quote(workdir)} || return 1{setup}
  printf '[fdp] $ %s\\n' {shlex.quote(shown)}
  {body}
}}
# Everything below goes to the container log (live console) and to console.log on the volume.
# tee runs outside the program's process group, so stopping the program never cuts the console.
exec > >(tee -a "$RUN_DIR/console.log") 2>&1
TEE_PID=$!
# Job control: the program gets its own process group, so the stop signal reaches it (and its
# children) but not this script, which runs as PID 1 and must stay alive to record the result.
set -m
main &
PID=$!
set +m   # the process group exists now; no "[1]+ Exit ..." job notices in the console
stopping=0
on_stop() {{
  stopping=1
  echo "[fdp] stop requested: asking the program to finish..."
  kill -TERM -- "-$PID" 2>/dev/null
}}
trap on_stop TERM INT
while :; do
  wait "$PID"; status=$?
  kill -0 "$PID" 2>/dev/null || break   # wait was interrupted by the trap; keep waiting
done
if [ "$stopping" = 1 ]; then
  echo stopped > "$RUN_DIR/exit_code"
  echo "[fdp] stopped by user (program exit code $status)"
  status=143
else
  echo "$status" > "$RUN_DIR/exit_code"
  echo "[fdp] finished with exit code $status"
fi
exec 1>&- 2>&-
wait "$TEE_PID" 2>/dev/null   # let tee flush before the container exits
exit "$status"
"""

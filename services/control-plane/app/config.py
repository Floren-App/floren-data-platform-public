from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator


class Settings(BaseSettings):
    """All settings come from FDP_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="FDP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./fdp.db"
    # Split connection fields let Kubernetes inject a password through secretKeyRef
    # without interpolating unescaped credentials into a URL. Host opts into this form.
    database_host: Optional[str] = None
    database_port: int = 5432
    database_name: str = 'fdp'
    database_user: str = 'fdp'
    database_password: str = ''

    @model_validator(mode='after')
    def database_connection(self):
        if self.database_host:
            from sqlalchemy.engine import URL
            self.database_url = URL.create('postgresql+psycopg', username=self.database_user,
                password=self.database_password, host=self.database_host, port=self.database_port,
                database=self.database_name).render_as_string(hide_password=False)
        return self
    # Operational metadata remains in this configurable database. The SQL catalog uses
    # an independent SELECT-only account; validated mutations are handled by the API.
    metadata_sql_enabled: bool = False
    metadata_catalog: str = "fdp_metadata"
    metadata_schema: str = "public"
    metadata_reader_user: str = "fdp_metadata_reader"
    metadata_reader_password: str = ""
    metadata_bootstrap_reader: bool = True
    # URL the desktop app uses to reach the gateway (Traefik).
    public_url: str = "https://localhost:8090"
    # Where outside callers reach internal APIs (/gw/...): a gateway entry point that asks for client certificates.
    api_gateway_url: str = "https://localhost:8091"
    api_gateway_entrypoint: str = "apigateway"
    gateway_config_token: str = ""  # shared with Traefik only; protects its routing/proof configuration
    # The app in the browser (Helm web.enabled): where its static files are served inside the cluster. The
    # gateway routes everything on the public address that is not the API, an editor or an app there.
    web_url: str = ""
    # How often notifications are posted to the configured webhooks (see notify).
    notification_interval_seconds: int = 30
    # Ends justified access, opens renewals, sends reminders, and sets deadlines for access that needs a form.
    compliance_interval_seconds: int = 900
    # URL execution containers use to reach the control plane.
    internal_url: str = "http://control-plane:8000"

    # --- Identity: any OIDC provider (Keycloak, Entra ID, Okta, Google, Authentik...) ---
    oidc_issuer: str = "https://localhost:8443/realms/fdp"
    oidc_audience: str = "fdp-api"
    # Public client the desktop app signs in with (authorization code + PKCE).
    oidc_client_id: str = "fdp-desktop"
    oidc_scopes: str = "openid profile email"
    # Optional bundled IdP backend routed by the platform gateway. Deployment-only.
    identity_proxy_url: Optional[str] = None
    # Override when the control plane reaches the IdP on a different host than clients do.
    oidc_jwks_url: Optional[str] = None
    # Dotted paths into the token claims. Keycloak: realm_access.roles; Entra ID: roles.
    oidc_roles_claim: str = "realm_access.roles"
    oidc_groups_claim: str = "groups"
    oidc_username_claim: str = "preferred_username"
    admin_role: str = "fdp-admin"
    directory_mode: Literal["oidc", "scim"] = "oidc"
    scim_token: str = ""  # separate provisioning bearer credential; empty disables SCIM
    # Bearer for /internal/metrics and /internal/metrics/certificates (Prometheus); empty disables them.
    metrics_token: str = ""
    release: str = "dev"   # the image tag this control plane runs (Helm sets it), for fdp_build_info
    # Gateway certificates to watch: "name=host:port/server-name,..." (set by the Helm chart).
    gateway_tls_endpoints: str = ""
    certificate_check_interval_seconds: int = 600
    # A cert-manager issuance still in progress after this long is reported as a failed renewal.
    certificate_issuing_timeout_seconds: int = 1800
    data_credential_ttl_seconds: int = 900

    # Shared SQL engine. "native" is only for retiring older deployments/adapters.
    query_executor: Literal["trino", "native"] = "trino"
    trino_url: str = "http://trino:8080"
    trino_signing_key: str = ""  # HMAC secret shared only with the infrastructure coordinator
    # EC private key (PEM) for per-layer Trino tokens. Layer Trinos receive only its public JWKS.
    trino_jwt_private_key: str = ""
    # Rotation overlap: former EC private keys (PEM, concatenated) whose public keys stay in the JWKS.
    trino_jwt_previous_keys: str = ""
    # Namespace prefix of controller-managed layers (set by Helm); "" disables layer engines.
    layer_namespace_prefix: str = ""
    # The controller publishes the platform CA bundle to each layer; executions mount it.
    layer_trust_bundle: bool = False
    # Extra buckets unavailable as plain-file sources. Iceberg integrations reserve their warehouse bucket
    # automatically; list other table buckets here (comma-separated).
    sql_lake_buckets: str = ""
    sql_query_ttl_seconds: int = 3600
    sql_max_statement_bytes: int = 1024 * 1024
    sla_evaluation_interval_seconds: int = 60

    # --- Tokens minted for workspace / compute containers and gateway cookies ---
    session_signing_key: str = "dev-only-change-me"
    # Rotation overlap: comma-separated former keys still accepted for verification (never signing).
    # Remove them once tokens they signed have expired (apps' tokens last up to 30 days; redeploy to renew).
    session_signing_previous_keys: str = ""
    session_token_ttl_seconds: int = 8 * 3600

    # --- Orchestration ---
    orchestrator: Literal["docker", "kubernetes", "fake"] = "docker"
    k8s_namespace: str = "fdp-runtime"
    # Private registry: the pull secret's name, and its .dockerconfigjson (from the Helm registryCredentials
    # Secret). The control plane keeps a copy in the runtime and layer namespaces; its pods pull with it.
    image_pull_secret: str = ""
    image_pull_config: str = ""
    # VS Code and RStudio reach the internet only through this proxy (http://host:port), and only the host names
    # administrators allow. Empty: no proxy, so workspaces have no internet access at all.
    egress_proxy_url: str = ""
    # API management calls backends from the control plane: only public addresses unless listed here (CIDRs).
    api_private_networks: str = ""
    # Requests per second (average, burst) per credential on the public API, SQL and API gateway routes; 0 disables.
    rate_limit_average: int = 100
    rate_limit_burst: int = 200
    k8s_storage_class: Optional[str] = None
    # Public CA bundle for executions in the shared namespace (managed layers get their own copy).
    k8s_trust_configmap: Optional[str] = None
    docker_network: str = "fdp"

    # R (RStudio workspaces, R and Shiny programs, R packages) is an optional part of an installation: its images
    # contain GPL/AGPL software (see THIRD_PARTY_LICENSES.md). Off: none of it is offered, and the images are unused.
    r_enabled: bool = True
    image_workspace_vscode: str = "fdp/workspace-vscode:dev"
    image_workspace_rstudio: str = "fdp/workspace-rstudio:dev"
    image_workspace_sql: str = "fdp/workspace-sql:dev"   # CloudBeaver Community (Apache-2.0)
    # Initial runtime catalog (seeded once; admins add/replace versions afterwards).
    image_compute_python: str = "fdp/compute-python:dev"
    image_compute_r: str = "fdp/compute-r:dev"
    image_compute_dbt: str = "fdp/compute-dbt:1.9"
    image_airflow: str = "fdp/airflow:dev"
    image_airflow_broker: str = "valkey/valkey:8.0.1"   # BSD-licensed Redis-compatible broker for Celery
    # Legacy distributed Airflow fallback when no shared metadata server is configured.
    image_airflow_metadata: str = "postgres:16.15-bookworm"
    # Shared PostgreSQL provisioner for standalone and distributed Airflow. Empty preserves
    # legacy standalone SQLite / managed-layer distributed PostgreSQL behavior.
    airflow_metadata_url: Optional[str] = None
    # Split fields keep provisioner credentials in Secret references, never plain Helm URLs.
    airflow_metadata_host: Optional[str] = None
    airflow_metadata_port: int = 5432
    airflow_metadata_user: str = 'airflow_provisioner'
    airflow_metadata_password: str = ''
    airflow_metadata_database: str = 'postgres'

    @model_validator(mode='after')
    def airflow_database_connection(self):
        if self.airflow_metadata_host:
            from sqlalchemy.engine import URL
            self.airflow_metadata_url = URL.create('postgresql', username=self.airflow_metadata_user,
                password=self.airflow_metadata_password, host=self.airflow_metadata_host,
                port=self.airflow_metadata_port, database=self.airflow_metadata_database).render_as_string(hide_password=False)
        return self
    default_python_version: str = "3.12"
    default_r_version: str = "4.4"
    default_dbt_version: str = "1.9"
    default_airflow_version: str = "2.10"
    max_versions_per_runtime: int = 5

    # Workspace tier is deliberately tiny: editing only, heavy work goes to compute pools.
    workspace_cpu: float = 0.5
    workspace_memory_gb: float = 1.0
    workspace_idle_timeout_seconds: int = 1800
    workspace_start_timeout_seconds: int = 300
    # Running (not stopped) workspaces per user; starting another is refused until one is stopped.
    max_running_workspaces_per_user: int = 2
    # Package mirrors for requirements installs. Empty: the platform's own package repository (below), which is
    # the only place workspaces and programs install from; set these only to use an external mirror instead.
    pip_index_url: Optional[str] = None
    cran_mirror: Optional[str] = None
    # The package repository: Python and R packages people publish, or import from upstream on request.
    package_dir: str = "/var/lib/fdp/packages"
    package_max_upload_mb: int = 200
    package_upstream_pypi: str = "https://pypi.org/simple"
    package_upstream_cran: str = "https://cloud.r-project.org"
    # Imports from PyPI fetch built packages (wheels) for these Python versions (plus the runtime catalog's)
    # and CPU types, for Linux with glibc up to package_glibc (Debian bookworm images: 2.36).
    package_python_versions: str = "3.11,3.12"
    package_platforms: str = "x86_64,aarch64"
    package_glibc: str = "2.36"
    # Tests and acceptance only: run an import right away in the request instead of in the leader's queue.
    package_imports_inline: bool = False
    reaper_interval_seconds: int = 60

    # --- Deploying (GitOps) ---
    git_cache_dir: str = "/var/lib/fdp/git"
    allow_file_repos: bool = False           # file:// repository URLs: tests and local experiments only
    gitops_interval_seconds: int = 60        # how often deployed branches are checked for new commits
    # Shared volume for deployed runs' consoles/outputs and environment caches. Each execution only
    # mounts its own folder of it. Docker: named volume; Kubernetes: ReadWriteMany PVC.
    data_volume: str = "fdp-data"
    # False on Kubernetes installs without the shared NFS volume: jobs/apps need a managed layer.
    shared_data_volume: bool = True
    data_dir: str = "/fdp-data"        # where the control plane itself mounts that volume
    app_start_timeout_seconds: int = 600     # a new app version must answer within this to go live

    # --- SIEM: the audit trail and data-access events, streamed to security monitoring ---
    siem_stdout: bool = False                  # JSON lines on stdout, for any log collector
    siem_syslog_address: str = ""              # host:port; RFC 5424
    siem_syslog_transport: Literal["tls", "tcp", "udp"] = "tls"
    siem_syslog_facility: int = 13             # 13: log audit
    siem_syslog_ca_file: Optional[str] = None  # receiver CA; default: system roots plus SSL_CERT_FILE
    siem_syslog_client_cert_file: Optional[str] = None   # mutual TLS to the receiver
    siem_syslog_client_key_file: Optional[str] = None
    siem_http_url: str = ""                    # HTTPS collector
    siem_http_format: Literal["generic", "splunk", "elastic"] = "generic"
    siem_http_token: Optional[str] = None      # bearer, Splunk HEC token or Elastic API key
    siem_http_index: str = "fdp-audit"         # elastic: index or data stream
    siem_otlp_endpoint: str = ""               # OpenTelemetry collector base URL (OTLP/HTTP, JSON)
    siem_otlp_headers: Optional[str] = None    # "key=value,key2=value2", as OTEL_EXPORTER_OTLP_HEADERS
    siem_signing_key: Optional[str] = None     # HMAC-SHA256 over each event's chain hash
    siem_include_queries: bool = True          # data access: every completed SQL query, its identity and tables
    siem_include_sql_text: bool = False        # SQL text only when the query log stores it and this is on
    siem_interval_seconds: int = 10
    siem_batch_size: int = 500
    siem_start: Literal["beginning", "now"] = "beginning"   # where a new destination starts
    siem_gap_wait_seconds: int = 60            # wait this long for an out-of-order audit commit before skipping its id
    siem_query_settle_seconds: int = 60        # export a query log row once it is this old

    # --- Secrets: where secret values live ---
    secrets_backend: Literal["openbao", "vault", "kubernetes", "aws", "azure", "gcp", "memory"] = "openbao"
    k8s_secret_namespace: str = 'fdp-secrets'
    # OpenBao or HashiCorp Vault (same API)
    openbao_addr: str = "http://localhost:8200"
    openbao_kv_mount: str = "fdp"
    openbao_namespace: Optional[str] = None    # OpenBao/Vault Enterprise namespace
    openbao_auth: Literal["token", "kubernetes", "approle"] = "token"
    openbao_token: Optional[str] = None
    openbao_token_file: Optional[str] = None   # read the token from a file (mounted secret)
    openbao_auth_role: str = "fdp-control-plane"   # kubernetes auth role
    openbao_auth_mount: str = ""               # auth mount path; empty: the method name
    openbao_approle_role_id: Optional[str] = None
    openbao_approle_secret_id: Optional[str] = None
    kubernetes_token_file: str = '/var/run/secrets/kubernetes.io/serviceaccount/token'
    # AWS Secrets Manager (credentials from the default chain: IRSA, EKS Pod Identity, environment)
    aws_secrets_region: Optional[str] = None
    aws_secrets_endpoint_url: Optional[str] = None
    aws_secrets_prefix: str = "fdp/"
    aws_secrets_kms_key_id: Optional[str] = None
    aws_secrets_recovery_window_days: int = 7   # 0 deletes immediately; otherwise 7-30
    # Azure Key Vault (credentials from DefaultAzureCredential: workload identity, managed identity)
    azure_key_vault_url: Optional[str] = None
    azure_secrets_prefix: str = "fdp-"
    # Google Secret Manager (credentials from Application Default Credentials: GKE workload identity)
    gcp_secrets_project: Optional[str] = None
    gcp_secrets_prefix: str = "fdp-"
    gcp_secrets_locations: str = ""            # comma-separated replica locations; empty: automatic


@lru_cache
def get_settings() -> Settings:
    return Settings()

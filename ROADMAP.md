## Next (planned 2026-10-29, in no fixed order)

[] Warehouses scoped by layer and workload type: "this warehouse is for dbt projects of the data engineers in Prod".
    - Today: a warehouse can be limited to groups, dedicated to them, or reserved for Trino workers; it is usable in
      every layer and by every kind of work.
    - Plan: optional `layers` and `workload_kinds` (script, notebook, app, dbt, airflow, kafka, model) on a
      warehouse, next to its groups (`fdp_compute_pool`, admin page). Admission checks all three for runs,
      deployments and Airflow tasks, with a clear refusal naming the missing match. Run forms and `fdp.json`
      choices only offer matching warehouses; a default warehouse per layer and kind replaces the single default.
    - The compute overview groups usage by layer and kind. Existing warehouses keep working (empty means any).

[] Semantic layer on Apache Ossie (incubating; formerly Open Semantic Interchange, OSI).
    - Ossie is the vendor-neutral YAML specification for semantic models: datasets, relationships, fields,
      metrics and AI context. Store models as governed catalog objects bound to registered tables, with the
      same IAM, layer and lineage as the tables underneath; current KPI definitions become Ossie metrics.
    - Import and export Ossie YAML in the app, the API and OpenTofu, and from Git (a semantic model in a workload
      repository, deployed with GitOps). Automatic sync from APIs: pull models on a schedule from Ossie-speaking
      sources (dbt's semantic layer, Apache Polaris' Semantic Model API, BI tools) through API management, with
      review before changes replace a published model.
    - Serve metrics: compile metric requests to SQL on the layer's Trino with the caller's permissions, so a
      metric never reveals more than its tables would. Show models in lineage (table -> model -> metric -> app).
    - Pin the supported Ossie version while the specification is incubating; keep vendor extensions intact.

[] Apache Kafka as a workload type.
    - Bring your own Kafka (registered as a storage integration, like PostgreSQL and S3) or an optional Kafka
      per managed layer (KRaft, single broker or three for HA), in the layer's namespace and network boundary.
    - Topics as governed resources: Producer and Consumer roles granted to groups and workload identities,
      translated to Kafka ACLs; no shared credentials in code (SASL/mTLS issued per execution).
    - New program kinds: stream consumers/producers (Python, with the SDK) and Kafka Connect connectors,
      including an Iceberg sink to land streams as governed tables. Lineage between topics, programs and
      tables; consumer lag and throughput in the compute overview and the catalog's SLAs.
    - Kafka, Strimzi-style operators and Kafka Connect are Apache-2.0; no proprietary components.

[] An all-in-one AI agent that knows the platform: bring your own license (Anthropic Claude or OpenAI), or a
   model running in the platform (next item).
    - Not only code: it searches the catalog and lineage, explains data and SLAs, writes and runs SQL, builds
      programs, dbt models and Airflow DAGs in a workspace, deploys with GitOps, and sets up storage, grants and
      warehouses where the person may.
    - Grounded rather than trained: the platform's documentation, API schema, fdp.json schema and live metadata
      as tools and context, so it stays correct across versions. The same tools as an MCP server, so external
      agents (for example Claude Code) can use the platform too.
    - Acts as the person, never more: every action goes through the API with their permissions and layer;
      changes need confirmation; everything is audited (and sent to the SIEM). Production layers stay
      read-only for it unless an administrator allows more.
    - License keys live in the secret store, per installation or per group; data sent to an external model is
      limited by policy (for example no tagged personal data), and the model choice is visible to users.

[] Custom models running inside the platform, for data interpretation, flows and as the agent's model.
    - A `model` workload: serve open-weight models with an OpenAI-compatible endpoint (for example vLLM or
      llama.cpp, both open source) on GPU warehouses in a layer, so data never leaves the layer.
    - Callable from programs, dbt and Airflow through API management (with IAM, quotas and audit), and
      selectable as the agent's model instead of Claude or OpenAI.
    - Uses: classification and tagging suggestions (for example personal data), explaining datasets, anomaly and
      quality checks, and steps in pipelines. Prefer models with open (OSI-approved) licenses; show each model's
      license, as for packages.

Suggested order: warehouses by layer and kind (the base for GPU and Kafka placement), Kafka, the semantic layer,
models in the platform, then the agent, which benefits from all of them.

[] KPI definitions.
[] SLA enformcement, shows risk or violation when the dataset does not fit a certain SLA. For example measure how often a dataset should be updated, and if that actually is the case. But see if thats possible.
[] DBT tests to measure KPI's of data? is this an option.

[] Everything possible within app as code. Terraform.
[] Fully kubernetes helm chartable infra. With netwerk segregated layers. This means trino per layer. And layer seperated warehouses.

[] Trino should use workers, that run on warehouses. Instead of a static trino instance. This is configured by admins or terraform. The trino orchestrator should also be defined on a warehouse of chosen size.
    - Notes: today each layer has one Trino with a fixed size (layer infrastructure `trino`). Within one Trino
      cluster, warehouses can only be soft limits (resource groups). Real isolation means one Trino cluster per
      warehouse per layer (coordinator on a chosen size, workers sized by the warehouse), with SQL admission routing
      each query to its warehouse and auto-suspending idle ones (pools already have `auto_suspend_seconds`).
      Cost: one coordinator per running warehouse. Largest of these items; builds on the two below.
    - Decided (2026-10-23): one Trino per layer. Its workers are sized by one warehouse chosen per layer (all
      workers of a Trino must be the same size), with a minimum (at least 1, always running) and a maximum; the
      layer scales between them with load, and a worker finishes its running queries before it is removed. The
      coordinator has its own size. Each worker takes one slot on its warehouse; the minimum is guaranteed, extra
      workers only start when the warehouse has room. Warehouses may be shared with jobs and apps; give Trino its
      own warehouse to make it predictable. Queries do not count toward warehouse limits; the configuration
      shows exactly how much compute Trino uses (coordinator size, worker warehouse, min/max workers).
    - Built, step 1: `trino.coordinator` (cpu, memory) and `trino.workers` (warehouse, min, max) in the layer
      infrastructure; workers get the warehouse's size and take its slots; heaps are 70% of memory; the
      warehouse is protected while used.
    - Built, step 2: scaling between min and max with load (running + waiting queries per worker), extra workers
      only with free warehouse slots, one-at-a-time scale-down after a quiet period, graceful drain of removed
      workers (preStop), and the live state on the layer infrastructure API. Verified on the k3d lab: 3 concurrent queries
      added a worker within 10 s and it was removed 60 s after the load stopped (91 of 91 queries succeeded); a
      21 s query finished although its only worker was deleted 4 s in.
    - Open: a requested worker the cluster cannot place yet (no node with room; the lab node is full) counts
      as running and takes a warehouse slot. Show requested vs. ready workers, and say when the cluster lacks room.
[] Dedicated warehouses for teams, e.g. data engineers' business-critical Airflow.
    - Decided (2026-10-23): called "dedicated". Created only by administrators and tied to groups; not one of
      the shared on-demand warehouses, so no slot limit, never queues and counts toward nothing else. No
      reservation inside Kubernetes: administrators make sure there are nodes for the warehouses they create.
    - Built: `dedicated` on warehouses (API, desktop admin page, `fdp_compute_pool`), groups required, not usable
      for Trino workers or workspaces; deploying on any group-limited warehouse now requires one of its groups
      (before, only runs checked this).
[] Layers only from configuration: no built-in Prod and Non-Prod.
    - Decided (2026-10-23): like warehouses, layers come only from OpenTofu or an administrator; a fresh install
      has exactly the layers its configuration defines. Next after the full lab test.
    - Needed: a configurable default layer for work without one (today: Non-Prod), and a clear "no layers
      configured yet" state; production as a layer property (e.g. `production = true`) instead of the built-in
      Prod layer, for the rules that depend on it (workspaces disabled, promotion); deletion stays guarded (refused
      while in use, retire first, storage kept until an admin removes it), so `tofu destroy` never drops data;
      existing installs keep Prod and Non-Prod as ordinary layers with the same IDs (import once). The lab and
      e2e configurations then create their layers instead of importing them.
    - Built (2026-10-23): no layers at first start; a default layer (`fdp_default_layer`, the Layers page, or the
      only layer); `production` on layers (personal and group secrets stay out); the default layer cannot be
      deleted; OpenTofu destroy retires a layer with infrastructure and keeps it with its storage; installs with
      the built-in layers keep them (Prod production, Non-Prod default). Lab, e2e and example create their layers.
[] Define warehouses with their max amount of usage. So limit small warehouses to 6. 
    - Decided: everything that uses a warehouse's compute counts toward its maximum, one per running container:
      jobs (including runs started from workspaces), deployed apps, each Airflow component, and Trino queries once
      Trino runs on warehouses. At the limit, new work queues (visible, with its position) and starts automatically
      when a slot frees.
    - Decided: workspaces have their own limits instead (total running workspaces, and per person), and run on one
      warehouse chosen by an administrator, which sizes the editors.
    - Built: `max_concurrent` on every warehouse (desktop admin page, `fdp_compute_pool`), a FIFO queue with visible
      position for jobs, deploys and workspaces, and workspace limits (desktop admin page, `fdp_workspace_settings`).
      Trino queries join the count when Trino runs on warehouses.
[] Make a clear warehouse overview, what resources are using what warehouses within the compute tab
    - Notes: the data exists (runs, published workloads, workspaces, pools). Show per warehouse and layer what is
      running/queued against its limit.
    - Built: Compute tab lists every warehouse with slots in use against its limit, waiting work and use per layer;
      a warehouse page shows what uses it (jobs, apps, Airflow, and workspaces on the workspace warehouse) with
      place in line. Counts include everyone; details only of work the viewer may see (`GET /v1/compute/warehouses`).

[] Rename devcenter to - FlorenDP, Full spelling, Floren Data Platform, shorthand for efficiency, FDP.
    - Notes: do this before the first real installation. "devcenter" is also in Kubernetes labels the layer
      controller uses for ownership, Secret names, `DEVCENTER_*` settings, the Helm chart, the OpenTofu provider and
      resource names (`devcenter_*`), and the SDKs; after real installs each needs a migration.
    - Decided: label/annotation domain `fdp.floren.app` (e.g. `fdp.floren.app/layer`); short technical names `fdp`,
      `FDP_*`, `fdp_layer`. Baseline commit a3d00a5 is the last state before the rename.
[] Rename "publication" to "workload" everywhere.
    - Decided: users see "workload" (Workloads tab, `/v1/workloads`, `fdp_workload`), and the action is "Deploy"
      instead of "Publish" ("deploy a program as a workload"). The existing internal "workload" (the running
      container, its token and service account) is renamed to "execution" in the code, matching the existing
      "execution identity". Done together with the product rename: both are breaking API/OpenTofu changes
      (`/v1/publications`, `devcenter_publication`, the `publications` table needs a migration).
    - Suggested order: both renames together, then warehouse limits and overview, then Trino on warehouses.



[] Full docs for everything, including infra terraform docs.


[] Column Tagging
[] Table Tagging
[] Schema Tagging
[] Data Object Tagging
[] Trino equivelent of Postgres Anonymizer - Prerequiset for data masking based on tags.
[] Compliance Forms - Every groups access to data or program can have a compliance form attached
[] Policy Definitions - Example: Compliance form reqruired for anything that has this tag. Workspaces should never be ablt to access certain data with tags. Way more, let AI think about a good starting set. Make it a rule or risk etc.
[] Policy Enforcement 

Findings from the hands-on Kubernetes lab test (2026-10-22)
[] Workspaces stay "starting" when their image cannot be pulled, then fail after 5 minutes with a misleading
   "did not become ready". Detect image pull/config failures (ErrImagePull, ImagePullBackOff, InvalidImageName,
   CreateContainerConfigError) and fail within seconds with the reason; same for jobs and deployed apps.
   (In the lab itself workspaces cannot run: their images did not fit on Docker's disk.)
[x] Jobs never finished ("running" forever): kubernetes.stream swaps the shared client's request method during
   a pod exec (used to read layer artifacts), so concurrent status calls failed. Fixed: each exec uses its own client.
[x] A run whose pod cannot be scheduled (e.g. "Insufficient memory") shows as "running". Surface it as waiting for
   capacity, with the reason, and show cluster/layer capacity next to the warehouse overview.
[x] Lab had no Iceberg data, so the catalog showed no tables or columns: `reporting` was registered by OpenTofu
   but never created in the engine. The lab now seeds sample tables in both layers (`make lab-up`, or
   `deploy/lab/devcenter-lab.py seed`).
[] A registered schema that does not exist in the engine (yet) looks simply empty in the catalog. Say so, and offer
   to create it for users who may.
[x] VS Code workspaces in the lab: the image is now loaded (streamed in, kept as .lab/images/*.tar).
[x] Five default warehouses (xsmall..xlarge, up to 16 vCPU/64 GB) are created by the platform at first start, outside
   OpenTofu and regardless of cluster size. Decided: warehouses come only from code or manual configuration, never by
   default (Compose development creates its own through its seed step).

## From the full-lab test (2026-10-23)
[x] Trino's worker warehouse could be chosen for jobs and apps. It is now Trino's alone (refused elsewhere, hidden from
    choices, never the default).
[x] Runs waiting for cluster room showed "running" without a reason; they show "pending" with Kubernetes' reason
    (runs and starting workspaces). An image that can never be pulled fails the run at once.
[x] Program runs defaulted to a "small" warehouse that did not exist (fdp.json template, SDK). There is a default
    warehouse (`fdp_default_warehouse`, Make default); a missing declared warehouse falls back to it.
[x] Recent runs mixed deployed-program runs with workspace runs. Workloads lists deployed runs; Workspaces has its
    own run list; workspace runs are private to their owner and administrators.
[] The desktop loads one list of recent runs for both views; with many runs of one kind, the other view may need
    "Load more" to show its runs. Load each view's runs separately (the API has `scope`).

## Workspaces per group, Git and network allowlist (decided 2026-10-23)
[x] Workspace templates per group: an admin (app or OpenTofu `fdp_workspace_template`) defines templates for a
    group (name, editor, layer, repositories to clone). Every member gets their own private workspace from each
    template, created stopped (no cost until first start), including later members. Workspaces always have
    one user, never shared. Leaving the group removes the workspace (files stay in the home folder); a workspace
    the person deleted is not recreated.
    - Built: templates (API, desktop admin page, `fdp_workspace_template`), per-member workspaces following group
      memberships (also nested directory groups), dismissals, `repositories` cloned at first start.
[x] Network allowlist for workspaces: VS Code and RStudio reach only hosts an admin allowed (Administration →
    Network allowlist, or OpenTofu `fdp_egress_allowlist`), e.g. github.com. Mechanism: an egress proxy in the
    platform namespace (Helm `egressProxy.enabled`) that tunnels HTTPS (port 443) only to allowed host names,
    end to end without inspection, and refuses names resolving to private addresses; the network lets marked
    editors reach only DNS, the platform and the proxy. The SQL editor has no internet access.
[x] Git with personal tokens: people store their own credentials (several per host, e.g. work and personal,
    with the name/email to commit as) in the platform's secret store (Account → Git credentials), never in a
    file in the workspace; each workspace's first start asks which one it uses, then sets it up automatically; a credential helper (git-credential-fdp) asks the platform
    for it each time git in VS Code or RStudio needs one, and only the person's own running editor gets it.
    Template repositories are cloned at first start with it (in the background; a failed clone leaves a note and
    is retried at the next start). Service accounts only read from Git (deployments fetch), never write: register
    repositories with read-only credentials. Volumes from before switch from git's plaintext store to the helper.

## Package repository (decided 2026-10-24)
[x] One package repository for Python and R in the platform (Packages in the app), with every version of every
    package. Everyone may install everything; RBAC on the repository (`resource_id = "packages"`, app or
    `fdp_role_assignment`) decides who publishes: Publishers publish new packages (their group becomes the
    package's Owner) and import from PyPI/CRAN; a package's Owner and Maintainers publish its new versions.
    - Python: a simple index (PEP 503/691) for pip and uv; uploads from the app, `twine upload` or `uv publish`
      (set up in every workspace). R: a CRAN-style repository (PACKAGES with the newest version, every version
      under src/contrib/Archive, so `renv::install("pkg@1.2.3")` works); `install.packages` and renv are
      pointed at it in RStudio and in programs.
    - Imports on request: PyPI as built packages (wheels) only, for the platform's Python versions and CPU
      types, so no package code runs during the import; CRAN source packages with Depends/Imports/LinkingTo,
      older versions from CRAN's archive, base and recommended packages skipped.
    - Published files never change; versions can be yanked (and deleted by Owners). A name is either published
      here or imported, never both, and a new published name that exists on PyPI/CRAN is refused, so packages
      cannot shadow each other.
    - Workspaces and programs have no route to PyPI or CRAN: the network allowlist refuses public package
      indexes, and the egress proxy refuses them whatever the list says.
[] Later: an R helper for installing older versions without renv; PEP 658 metadata files; a cleanup view of
    unused versions; per-layer restrictions (e.g. Prod installs only reviewed packages).

## Open-source and enterprise readiness (decided 2026-10-24)
[x] Configuration split made explicit: Helm installs, OpenTofu (or the app) configures what the platform contains;
    Helm upgrades never change OpenTofu settings (docs/configuration.md). The installation's runtimes follow the
    Helm images on every upgrade; extra versions come from `fdp_runtime`.
[x] Apache-2.0 LICENSE, NOTICE, THIRD_PARTY_LICENSES.md, SECURITY.md, CONTRIBUTING.md (DCO sign-off).
[x] R optional (`r.enabled`, off by default): its images carry AGPL/GPL software (RStudio Server, R, Shiny).
[x] The app in the browser (`web.enabled`): no desktop app needed. Initial release.
[x] Several control plane replicas (`replicas`) with leader election through the metadata database; failover
    in seconds (tested on the floren cluster: 2 s after deleting the leader).
[x] Observability: Prometheus metrics (`/internal/metrics`, optional ServiceMonitor) and a query log of every
    Trino query with who or what ran it (`fdp_query_log_settings`).
[] Versioned migrations of the platform's own metadata database (upgrades across several releases). Customer
    data schemas stay as they are: the platform governs schemas, workloads create and change their own tables.
[] Published artifacts: public, signed images for amd64 and arm64; the Helm chart and a signed OpenTofu
    provider in registries; signed, notarized desktop app builds.
    - Built (2026-10-29): one pipeline (tests on every change; images with SBOM and provenance, signed with
      Sigstore; signed chart releases on version tags) and the OpenTofu provider in Floren-App/terraform-provider-fdp
      with a GPG-signed release v0.2.0. Open: the public pipeline's write access to the image packages, arm64
      images, the OpenTofu and Terraform registry listings, and desktop app builds.
[x] Bring your own secret store, with a bundled OpenBao (Kubernetes-Secret or KMS auto-unseal), and SIEM export of
    the audit trail and data access (stdout, syslog, HTTPS, OpenTelemetry; hash-chained and signed).
[] Later, not needed now: node placement options in the chart, more tested Kubernetes distributions (EKS, AKS,
    GKE, OpenShift), more data connectors, verified Entra ID and Okta, pod security level restricted, SBOMs.


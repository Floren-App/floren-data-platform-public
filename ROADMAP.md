# Roadmap

What is planned for Floren Data Platform. What exists today, and its limits, is in
[feature status](docs/feature-status.md); releases are listed on the
[releases page](https://github.com/Floren-App/floren-data-platform-public/releases). Plans change; open an
issue to discuss one.

## Next

Suggested order: Kafka, the semantic layer, models in the platform, then the agent, which benefits from all of
them. Warehouses scoped by layer and kind of work, and column masks and row filters, are available.

### Apache Kafka as a workload type

- Bring your own Kafka (registered like PostgreSQL and S3) or an optional Kafka per managed layer (KRaft), in
  the layer's namespace and network boundary.
- Topics as governed resources: producer and consumer roles for groups and workload identities, translated to
  Kafka ACLs; credentials issued per execution, never in code.
- Stream programs (Python, with the SDK) and Kafka Connect connectors, including an Iceberg sink that lands
  streams as governed tables; lineage between topics, programs and tables; consumer lag in the compute
  overview and the catalog's SLAs.
- Apache-2.0 components only.

### Models in the platform

- A `model` workload that serves open-weight models behind an OpenAI-compatible endpoint (for example vLLM or
  llama.cpp) on GPU warehouses in a layer, so data never leaves the layer.
- Callable from programs, dbt and Airflow through API management, with IAM, quotas and audit; selectable as the
  agent's model.
- Uses: classification and tagging suggestions (for example personal data), explaining datasets, anomaly and
  quality checks, and steps in pipelines. Each model's license is shown, as for packages.

### An AI agent that knows the platform

- Bring your own Anthropic Claude or OpenAI license, or use a model running in the platform.
- More than code: it searches the catalog and lineage, explains data and SLAs, writes and runs SQL, builds
  programs, dbt models and Airflow DAGs in a workspace, deploys with GitOps, and sets up storage, grants and
  warehouses where the person may.
- Grounded in the platform's documentation, API schema, `fdp.json` schema and live metadata rather than
  trained on them; the same tools as an MCP server for external agents.
- Acts as the person, never more: every action goes through the API with their permissions, changes need
  confirmation, and everything is audited and exported to the SIEM. Production layers stay read-only for it
  unless an administrator allows more.
- License keys in the secret store, per installation or group; policies limit what data may be sent to an
  external model.

## Data governance

- Tags on columns, tables, schemas and other data objects.
- Column masks and row filters assigned by tag (masks and filters per table are available).
- Row filters on identity attributes from the IdP, for example a person's region.
- Compliance forms attached to a group's access to data or programs.
- Policies built on tags, such as "access to this tag needs a compliance form" or "workspaces never read this
  tag", with enforcement and visible risks or violations.
- KPI definitions and SLA checks on how often a dataset is actually updated; dbt tests that measure KPIs;
  delivery of SLA alerts to external incident systems.

## Platform

- Versioned migrations of the platform's own metadata database across several releases.
- arm64 images; signed, notarized desktop app builds.
- Pod security level `restricted` for every platform pod.
- Node placement options in the chart; more tested Kubernetes distributions (EKS, AKS, GKE, OpenShift); verified
  Microsoft Entra ID and Okta; more data connectors.

## Smaller improvements

- Workspaces and deployed apps whose image cannot be pulled fail within seconds with the reason, as runs do.
- A registered schema that does not exist in the engine says so, and offers to create it to those who may.
- The run lists of Workloads and Workspaces load separately, so many runs of one kind never hide the other.
- Trino workers that are requested but cannot be placed yet are shown as such, with the cluster's lack of room.
- Package repository: installing older R versions without renv, PEP 658 metadata files, a cleanup view of
  unused versions, and per-layer restrictions (for example Prod installs only reviewed packages).

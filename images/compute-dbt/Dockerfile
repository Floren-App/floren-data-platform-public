# Compute tier for dbt projects (dbt-core, Apache-2.0). Profiles come from Floren Data Platform data sources.
# Build from the repo root: docker build -f images/compute-dbt/Dockerfile -t fdp/compute-dbt:dev .
# Other versions for the runtime catalog: make runtime KIND=dbt VERSION=1.8
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
ARG DBT_VERSION=1.9
# All governed PostgreSQL and Iceberg models execute through the shared Trino engine.
ARG DBT_ADAPTERS="dbt-trino"

# git: dbt deps (git packages); curl: deployed source download.
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home analyst && mkdir -p /workspace /app && chown 1000:1000 /workspace /app

COPY sdk/python /tmp/sdk
RUN pip install --no-cache-dir "/tmp/sdk[sql,s3]" "dbt-core~=${DBT_VERSION}.0" \
      $(for a in $DBT_ADAPTERS; do printf '%s~=%s.0 ' "$a" "$DBT_VERSION"; done) \
 && rm -rf /tmp/sdk

USER analyst
WORKDIR /workspace
ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false

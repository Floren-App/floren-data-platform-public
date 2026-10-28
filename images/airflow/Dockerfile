# Airflow deployments (Apache-2.0): orchestration only; tasks run on warehouses via
# fdp.airflow.FDPRunOperator.
# Build from the repo root: docker build -f images/airflow/Dockerfile -t fdp/airflow:dev .
# Other versions for the runtime catalog: make runtime KIND=airflow VERSION=2.9
ARG AIRFLOW_VERSION=2.10.3
ARG PYTHON_VERSION=3.12
FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${PYTHON_VERSION}

USER root
# /app: deployed source; group 0 writable so the container can run as uid 1000 (Floren Data Platform's user).
RUN mkdir -p /app && chown 1000:0 /app && chmod 2775 /app
USER airflow

# Distributed mode: Celery workers (Valkey broker) and a PostgreSQL metadata database. Pinned with
# Airflow's own constraints so provider versions match the Airflow release.
ARG AIRFLOW_VERSION
ARG PYTHON_VERSION
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
      apache-airflow-providers-celery apache-airflow-providers-postgres \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

COPY --chown=airflow:root sdk/python /tmp/sdk
RUN pip install --no-cache-dir /tmp/sdk && rm -rf /tmp/sdk
ENV AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__WEBSERVER__WARN_DEPLOYMENT_EXPOSURE=False

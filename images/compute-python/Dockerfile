# Compute tier for Python: scripts, notebooks (papermill) and Streamlit apps.
# Build from the repo root: docker build -f images/compute-python/Dockerfile -t fdp/compute-python:dev .
# Other versions for the runtime catalog: make runtime KIND=python VERSION=3.11
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

RUN useradd --uid 1000 --create-home analyst && mkdir -p /workspace && chown 1000:1000 /workspace

COPY sdk/python /tmp/sdk
RUN pip install --no-cache-dir /tmp/sdk[all] \
      pandas numpy pyarrow polars scikit-learn matplotlib plotly \
      streamlit papermill ipykernel pyiceberg \
 && rm -rf /tmp/sdk

# Deployed programs download their source (from the control plane) into /app.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /app && chown 1000:1000 /app

USER analyst
WORKDIR /workspace
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

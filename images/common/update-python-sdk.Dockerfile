# Local SDK-only refresh for an existing runtime, retaining installed dependencies.
# Full runtime Dockerfiles remain the reproducible build path for clean installations.
ARG BASE_IMAGE=fdp/compute-python:dev
FROM ${BASE_IMAGE}
USER root
COPY sdk/python /tmp/fdp-sdk
RUN python -m pip install --no-cache-dir --no-deps --force-reinstall /tmp/fdp-sdk \
 && rm -rf /tmp/fdp-sdk
ARG RUNTIME_USER=analyst
USER ${RUNTIME_USER}

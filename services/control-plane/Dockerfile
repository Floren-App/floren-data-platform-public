FROM postgres:16-bookworm AS postgres-tools
FROM python:3.12-slim-bookworm

# git: the control plane is the only component that talks to git hosts (deploying / GitOps).
RUN apt-get update && apt-get install -y --no-install-recommends git libpq5 liblz4-1 libzstd1 && rm -rf /var/lib/apt/lists/*
COPY --from=postgres-tools /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres-tools /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
# Keep the tools with their client library rather than Bookworm’s PostgreSQL 15 libpq.
COPY --from=postgres-tools /usr/lib/*/libpq.so.5.* /usr/local/lib/
RUN ldconfig && pg_dump --version && pg_restore --version

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN useradd --uid 10001 --create-home fdp \
 && mkdir -p /var/lib/fdp/git /fdp-data && chown -R fdp /var/lib/fdp /fdp-data
USER fdp
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

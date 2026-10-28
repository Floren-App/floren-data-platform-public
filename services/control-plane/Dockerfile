FROM python:3.12-slim

# git: the control plane is the only component that talks to git hosts (deploying / GitOps).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN useradd --uid 10001 --create-home fdp \
 && mkdir -p /var/lib/fdp/git /fdp-data && chown -R fdp /var/lib/fdp /fdp-data
USER fdp
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

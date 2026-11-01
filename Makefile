# Floren Data Platform itself, with optional example storage for local testing (a PostgreSQL warehouse and
# RustFS with Iceberg). Opt in with EXAMPLE_STORAGE=1 when running the examples.
EXAMPLE_STORAGE ?= 0
COMPOSE := docker compose -f deploy/compose/docker-compose.yml $(if $(filter 1,$(EXAMPLE_STORAGE)),-f deploy/compose/example-storage/docker-compose.yml)
CP := services/control-plane
DOCS_VENV ?= .venv-docs
DOCS_PYTHON ?= $(DOCS_VENV)/bin/python
CP_PYTHON ?= $(CP)/.venv/bin/python

.PHONY: docs-install docs-serve docs-build docs-reference
docs-install:
	python3 -m venv $(DOCS_VENV)
	$(DOCS_PYTHON) -m pip install -r requirements-docs.txt

docs-serve:
	$(DOCS_PYTHON) -m mkdocs serve

docs-build:
	$(DOCS_PYTHON) -m mkdocs build --strict
	$(DOCS_PYTHON) scripts/check_docs.py site

docs-reference:
	$(CP_PYTHON) scripts/permission_reference.py
	$(CP_PYTHON) scripts/docs_reference.py
	cd terraform/provider-fdp && go run ./cmd/docs ../../docs/reference/terraform

.PHONY: certs images runtime dev down seed storage-example desktop desktop-web test test-postgres test-sql test-ui test-helm test-helm-bootstrap test-layer-egress test-layer-tls test-certificate-renewal schema

# JSON Schema for fdp.json, generated from the control plane's models.
schema:
	cd $(CP) && .venv/bin/python -c "import json; from app.programs import json_schema; \
	  open('../../schemas/fdp.schema.json', 'w').write(json.dumps(json_schema(), indent=2) + '\n')"

CERTS := deploy/compose/certs/localhost.pem

# Locally-trusted HTTPS for development (needs `brew install mkcert && mkcert -install` once).
certs: $(CERTS)
$(CERTS):
	mkdir -p deploy/compose/certs
	mkcert -cert-file deploy/compose/certs/localhost.pem -key-file deploy/compose/certs/localhost-key.pem localhost 127.0.0.1 ::1
	chmod 644 deploy/compose/certs/*.pem

images:
	docker build -f images/workspace-vscode/Dockerfile  -t fdp/workspace-vscode:dev .
	docker build -f images/workspace-rstudio/Dockerfile -t fdp/workspace-rstudio:dev .
	docker build -f images/workspace-sql/Dockerfile     -t fdp/workspace-sql:dev .
	docker build -f images/compute-python/Dockerfile    -t fdp/compute-python:dev .
	docker build -f images/compute-r/Dockerfile         -t fdp/compute-r:dev .
	docker build -f images/compute-dbt/Dockerfile       -t fdp/compute-dbt:1.9 .
	docker build -f images/airflow/Dockerfile           -t fdp/airflow:dev .

# Extra versions for the runtime catalog, e.g.
#   make runtime KIND=python VERSION=3.11      make runtime KIND=r VERSION=4.3
#   make runtime KIND=dbt VERSION=1.7          make runtime KIND=airflow VERSION=2.9.3
# then offer it: Administration → Runtimes (image fdp/<image>:<VERSION>).
RUNTIME_IMAGE_python  := compute-python
RUNTIME_IMAGE_r       := compute-r
RUNTIME_IMAGE_dbt     := compute-dbt
RUNTIME_IMAGE_airflow := airflow
RUNTIME_ARG_python    := PYTHON_VERSION
RUNTIME_ARG_r         := R_VERSION
RUNTIME_ARG_dbt       := DBT_VERSION
RUNTIME_ARG_airflow   := AIRFLOW_VERSION
runtime:
	@test -n "$(KIND)" -a -n "$(VERSION)" || (echo "usage: make runtime KIND=python|r|dbt|airflow VERSION=x.y" && exit 1)
	docker build -f images/$(RUNTIME_IMAGE_$(KIND))/Dockerfile --build-arg $(RUNTIME_ARG_$(KIND))=$(VERSION) \
	  -t fdp/$(RUNTIME_IMAGE_$(KIND)):$(VERSION) .
	@echo "built fdp/$(RUNTIME_IMAGE_$(KIND)):$(VERSION) — offer it under Administration → Runtimes"

dev: certs
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

# Setup action: prepare the example storage and register it as three storage integrations.
storage-example:
	bash deploy/compose/example-storage/setup.sh

# Admin-only bootstrap by default. EXAMPLE_STORAGE=1 explicitly opts into the demo seed.
seed:
	EXAMPLE_STORAGE=$(EXAMPLE_STORAGE) bash deploy/compose/seed.sh

desktop:
	cd apps/desktop && npm install && npx tauri dev

desktop-web:
	cd apps/desktop && npm install && npm run dev

test:
	cd $(CP) && (test -d .venv || python3 -m venv .venv) && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/pytest -q -n auto
	cd apps/desktop && npm install --no-fund --no-audit && npm run build
	cd extensions/vscode-fdp && npm install --no-fund --no-audit && npm run compile

test-sql:
	bash services/control-plane/tests/sql-integration/run.sh

test-postgres:
	bash services/control-plane/tests/postgres/run.sh

test-helm:
	cd $(CP) && .venv/bin/python -m pytest -q tests/test_helm.py tests/test_directory_sync.py tests/test_kubernetes_secrets.py tests/test_layer_controller.py tests/test_layer_egress.py tests/test_layer_tls.py tests/test_java_trust.py tests/test_worker_tls.py tests/test_layer_postgres.py

test-helm-bootstrap:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_bootstrap.py

test-layer-egress:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_layer_egress.py

.PHONY: test-layer-postgres
test-layer-postgres:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_layer_postgres.py

test-layer-tls:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_layer_tls.py

.PHONY: test-layer-java-trust
test-layer-java-trust:
	FDP_TEST_JAVA_TRUST=1 $(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_layer_tls.py

test-certificate-renewal:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_certificate_renewal.py

# Check the current (or KUBECONFIG/CONTEXT) cluster before installing: version, permissions,
# storage, DNS, and that the CNI really enforces NetworkPolicy. Creates one temporary namespace.
.PHONY: preflight
preflight:
	python3 deploy/preflight/fdp-preflight.py $(if $(CONTEXT),--context $(CONTEXT),) $(if $(IMAGE),--image $(IMAGE),)

# OpenTofu provider: build, then apply/plan/drift/import/destroy against a temporary control plane.
.PHONY: test-terraform
test-terraform:
	cd terraform/provider-fdp && go vet ./...
	$(CP)/.venv/bin/python terraform/provider-fdp/acceptance/run_acceptance.py

# Real helm install/upgrade, then workspace, job, app and Airflow in a managed layer (~25 min).
# A local Kubernetes lab for trying Floren Data Platform by hand (Helm + OpenTofu); see deploy/lab/fdp-lab.py.
.PHONY: lab-up lab-status lab-tofu lab-down
lab-up:
	$(CP)/.venv/bin/python deploy/lab/fdp-lab.py up
lab-status:
	$(CP)/.venv/bin/python deploy/lab/fdp-lab.py status
lab-tofu:
	$(CP)/.venv/bin/python deploy/lab/fdp-lab.py tofu $(ARGS)
lab-down:
	$(CP)/.venv/bin/python deploy/lab/fdp-lab.py down

.PHONY: test-preflight
test-preflight:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_preflight.py

.PHONY: test-image-trust
test-image-trust:
	$(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_image_trust.py

.PHONY: test-e2e-external
test-e2e-external:
	$(shell command -v caffeinate >/dev/null && echo caffeinate -is) $(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_e2e_external.py

.PHONY: test-e2e-install
test-e2e-install:
	$(shell command -v caffeinate >/dev/null && echo caffeinate -is) $(CP)/.venv/bin/python $(CP)/tests/kubernetes-integration/verify_e2e_install.py

# One-time browser setup: cd apps/desktop && npx playwright install chromium
test-ui:
	cd apps/desktop && npm run test:ui

# Getting started: two isolated layers, a warehouse, one storage integration with a
# governed schema, analysts with read access, a repository and a deployed job.
# Authenticate with the chart's automation client; see terraform/provider-fdp/README.md.

terraform {
  required_providers {
    fdp = { source = "registry.opentofu.org/floren/fdp" }
  }
}

variable "endpoint" { default = "https://fdp.example.com/api" }
variable "token_url" { default = "https://idp.fdp.example.com/realms/fdp/protocol/openid-connect/token" }
variable "warehouse_password" {
  type      = string
  sensitive = true
  ephemeral = true # write-only: never stored in state
}

provider "fdp" {
  endpoint  = var.endpoint
  token_url = var.token_url
  client_id = "fdp-automation"
  # client_secret from FDP_CLIENT_SECRET
}

locals {
  isolated = {
    enabled = true
    # Trino: the coordinator has its own size; workers have the size of one warehouse, each takes one of
    # its slots, and the layer runs between min (always on) and max workers depending on load.
    trino = {
      coordinator = { cpu = 2, memory = "8Gi" }
      workers     = { warehouse = fdp_compute_pool.sql.name, min = 1, max = 4 }
    }
    quota   = { cpu = 32, memory = "128Gi", pods = 100 }
  }
}

# The platform has no built-in layers: name and shape them as your organisation works.
# Upgraded installations that had the built-in Prod and Non-Prod import them once instead:
#   import { to = fdp_layer.prod     id = "1a0000000001" }
#   import { to = fdp_layer.nonprod  id = "1a0000000002" }
resource "fdp_layer" "prod" {
  name           = "Prod"
  production     = true # personal and group secrets are not usable here
  infrastructure = jsonencode(local.isolated)
}

resource "fdp_layer" "nonprod" {
  name           = "Non-Prod"
  infrastructure = jsonencode(local.isolated)
}

# Work that names no layer goes here.
resource "fdp_default_layer" "this" {
  layer_id = fdp_layer.nonprod.id
}

resource "fdp_compute_pool" "small" {
  name      = "small"
  cpu       = 2
  memory_gb = 8
}

# Trino workers of both layers run here; with max = 4 per layer, 8 slots hold them at their largest.
resource "fdp_compute_pool" "sql" {
  name           = "sql"
  cpu            = 4
  memory_gb      = 16
  max_concurrent = 8
}

resource "fdp_data_source" "warehouse" {
  name     = "sales-dw"
  kind     = "postgres"
  layer_id = fdp_layer.nonprod.id
  config   = jsonencode({ host = "pg.internal", port = 5432, database = "sales", catalog = "sales" })
  provisioner_secret_wo  = { username = "governance_admin", password = var.warehouse_password }
  credentials_wo_version = 1 # bump to send a rotated password
}

resource "fdp_schema" "reporting" {
  data_source = fdp_data_source.warehouse.name
  name        = "reporting"
}

data "fdp_principal" "finance" {
  name = "/Finance"   # an IdP group provisioned by SCIM
  kind = "group"
}

resource "fdp_app_group" "analysts" {
  name   = "Finance analysts"
  owners = [data.fdp_principal.finance.id]
}

resource "fdp_app_group_member" "finance" {
  app_group_id = fdp_app_group.analysts.id
  principal_id = data.fdp_principal.finance.id
}

resource "fdp_role_assignment" "analysts_read" {
  resource_id  = fdp_schema.reporting.id
  app_group_id = fdp_app_group.analysts.id
  role         = "Reader"
}

resource "fdp_repository" "analytics" {
  name = "analytics"
  url  = "https://git.example.com/finance/analytics.git"
}

resource "fdp_workload" "sales_report" {
  repository_id = fdp_repository.analytics.id
  branch        = "main"
  program       = "sales-report"
  slug          = "sales-report"
  layer_id      = fdp_layer.nonprod.id
  warehouse     = fdp_compute_pool.small.name
}

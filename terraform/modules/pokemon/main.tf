locals {
  branch = var.environment == "prod" ? "main" : "test"
  jobs   = toset(["bootstrap", "extract", "load", "models", "process-event", "explorer", "monitor", "pipelines"])
  prefix = "pokemon-${var.environment}"
}

resource "fdp_repository" "this" {
  name                   = "pokemon-${var.environment}"
  url                    = var.repository_url
  default_branch         = local.branch
  allowed_groups         = [var.engineers_group_id]
  credentials_wo         = var.repository_credentials
  credentials_wo_version = var.credentials_version
  description            = "Pokemon ${var.environment}: code promoted from test to main; no data/credentials copied."
}

resource "fdp_folder" "this" {
  name     = "Pokemon ${title(var.environment)}"
  layer_id = var.layer_id
  branch   = local.branch
}
resource "fdp_role_assignment" "engineers_folder" {
  resource_id  = fdp_folder.this.id
  app_group_id = var.engineers_group_id
  role         = "Admin"
}

resource "fdp_data_source" "landing" {
  name     = "pokemon-landing-${var.environment}"
  kind     = "s3"
  layer_id = var.layer_id
  config = jsonencode({
    bucket = var.landing_bucket, prefix = "pokemon/${var.environment}", managed_rustfs = true
  })
}
resource "fdp_schema" "landing" {
  data_source = fdp_data_source.landing.name
  name        = "landing"
  prefix      = "landing"
}
resource "fdp_schema" "raw" {
  data_source = var.data_source
  name        = "pokemon_raw"
  append_only = true
}
resource "fdp_schema" "history" {
  data_source = var.data_source
  name        = "pokemon_history"
}
resource "fdp_role_assignment" "engineers_data" {
  for_each = {
    landing = fdp_schema.landing.id, raw = fdp_schema.raw.id, history = fdp_schema.history.id
  }
  resource_id  = each.value
  app_group_id = var.engineers_group_id
  role         = "Admin"
}
resource "fdp_role_assignment" "analysts_product" {
  resource_id  = var.product_schema_id
  app_group_id = var.analysts_group_id
  role         = "Reader"
}

resource "fdp_api" "tcgdex" {
  name        = "TCGdex ${var.environment}"
  slug        = "tcgdex-${var.environment}"
  direction   = "external"
  layer_id    = var.layer_id
  description = "English Pokemon sets and cards. All outbound calls use API management."
  outbound    = jsonencode({ base_url = "https://api.tcgdex.net/v2/en", timeout_seconds = 60 })
}
resource "fdp_api_operation" "get" {
  for_each = { sets = "/sets/{id}", cards = "/cards/{id}" }
  api_id   = fdp_api.tcgdex.id
  name     = each.key
  method   = "GET"
  path     = each.value
}
resource "fdp_role_assignment" "engineers_api" {
  resource_id  = fdp_api.tcgdex.resource_id
  app_group_id = var.engineers_group_id
  role         = "Admin"
}

locals {
  data_grants = {
    bootstrap_raw     = { job = "bootstrap", resource = fdp_schema.raw.id, role = "Admin" }
    bootstrap_history = { job = "bootstrap", resource = fdp_schema.history.id, role = "Admin" }
    bootstrap_product = { job = "bootstrap", resource = var.product_schema_id, role = "Admin" }
    extract_landing   = { job = "extract", resource = fdp_schema.landing.id, role = "Contributor" }
    load_landing      = { job = "load", resource = fdp_schema.landing.id, role = "Reader" }
    load_raw          = { job = "load", resource = fdp_schema.raw.id, role = "Contributor" }
    models_raw        = { job = "models", resource = fdp_schema.raw.id, role = "Reader" }
    models_history    = { job = "models", resource = fdp_schema.history.id, role = "Admin" }
    models_product    = { job = "models", resource = var.product_schema_id, role = "Admin" }
    events_raw        = { job = "process-event", resource = fdp_schema.raw.id, role = "Contributor" }
    explorer_product  = { job = "explorer", resource = var.product_schema_id, role = "Reader" }
    monitor_raw       = { job = "monitor", resource = fdp_schema.raw.id, role = "Reader" }
    monitor_history   = { job = "monitor", resource = fdp_schema.history.id, role = "Reader" }
  }
}
# Each workload owns one built-in managed identity. Inline grants avoid a
# deployment/identity dependency cycle on a clean installation.
resource "fdp_workload" "job" {
  for_each       = setsubtract(local.jobs, ["pipelines"])
  repository_id  = fdp_repository.this.id
  branch         = local.branch
  program        = "${each.key}-${var.environment}"
  slug           = "pokemon-${each.key}-${var.environment}"
  title          = "Pokemon ${title(each.key)} ${title(var.environment)}"
  warehouse      = contains(["explorer", "monitor"], each.key) ? var.warehouses.app : var.warehouses.job
  layer_id       = var.layer_id
  folder_id      = fdp_folder.this.id
  execution_mode = "fixed"
  auto_update    = true
  managed_identity_grants = merge(
    { for grant in local.data_grants : grant.resource => grant.role if grant.job == each.key },
    each.key == "extract" ? { (fdp_api.tcgdex.resource_id) = "Consumer" } : {}
  )
  depends_on = [fdp_api_operation.get]
}
resource "fdp_workload" "pipelines" {
  repository_id  = fdp_repository.this.id
  branch         = local.branch
  program        = "pipelines-${var.environment}"
  slug           = "pokemon-pipelines-${var.environment}"
  title          = "Pokemon Pipelines ${title(var.environment)}"
  warehouse      = var.warehouses.orchestrator
  layer_id       = var.layer_id
  folder_id      = fdp_folder.this.id
  execution_mode = "fixed"
  managed_identity_grants = {
    for name in ["extract", "load", "models", "process-event"] : fdp_workload.job[name].resource_id => "Executor"
  }
}
resource "fdp_role_assignment" "analysts_dashboard" {
  resource_id  = fdp_workload.job["explorer"].resource_id
  app_group_id = var.analysts_group_id
  role         = "Executor"
}

locals {
  governed = merge({
    raw       = fdp_schema.raw.id, history = fdp_schema.history.id, landing = fdp_schema.landing.id,
    product   = var.product_schema_id, api = fdp_api.tcgdex.resource_id,
    pipelines = fdp_workload.pipelines.resource_id
  }, { for name, job in fdp_workload.job : name => job.resource_id })
}
resource "fdp_catalog_governance" "this" {
  for_each       = local.governed
  resource_id    = each.value
  owner_group_id = var.engineers_group_id
  description    = "Pokemon ${var.environment}: ${each.key}. Trace stable record IDs through immutable files, raw observations, SCD2 and products."
  domain         = "Pokemon"
  classification = "internal"
  steward        = "Data engineers"
  tags           = ["Pokemon", var.environment, each.key == "explorer" || each.key == "product" ? "Data product" : "Process flow"]
  status         = "reviewed"
}

output "bootstrap_workload" { value = fdp_workload.job["bootstrap"].slug }
output "pipeline_workload" { value = fdp_workload.pipelines.slug }
output "product_dashboard" { value = fdp_workload.job["explorer"].url }
output "monitoring_dashboard" { value = fdp_workload.job["monitor"].url }

# Column masks and a row filter on the product, for demonstrations. The explorer dashboard runs as its own
# identity, so it shows what analysts see. The pipeline identities that build the product are exempt: they
# must process the real data.
locals {
  demo_policies = var.demo_data_policies ? {
    price = {
      kind        = "column_mask"
      column      = "price_trend_eur"
      method      = "expression"
      expression  = "round(price_trend_eur)"
      description = "Analysts see market prices rounded to whole euros."
    }
    illustrator = {
      kind        = "column_mask"
      column      = "illustrator"
      method      = "partial"
      expression  = ""
      description = "Analysts see the last four characters of the illustrator's name."
    }
    rarity = {
      kind        = "row_filter"
      column      = ""
      method      = ""
      expression  = "rarity <> 'Rare'"
      description = "Analysts do not see rare cards."
    }
  } : {}
}
resource "fdp_data_policy" "demo" {
  for_each          = local.demo_policies
  schema_id         = var.product_schema_id
  table             = "cards_current"
  kind              = each.value.kind
  column            = each.value.column
  method            = each.value.method
  expression        = each.value.expression
  description       = each.value.description
  exempt_groups     = [var.engineers_group_id]
  exempt_principals = [for name in ["bootstrap", "models"] : fdp_workload.job[name].service_principal_id]
}

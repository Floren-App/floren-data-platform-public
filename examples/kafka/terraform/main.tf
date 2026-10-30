terraform {
  required_providers {
    fdp = { source = "floren-app/fdp" }
  }
}

# Module inputs reference existing layer-isolated infrastructure. The module
# creates no identities shared between Test and Prod and contains no credentials.
variable "instances" {
  type = map(object({
    layer_id           = string
    cluster_id         = string
    repository_id      = string
    branch             = string
    folder_id          = string
    warehouse          = string
    api_name           = string
    api_resource_id    = string
    landing_name       = string
    landing_schema_id  = string
    replication_factor = number
  }))
  validation {
    condition = (
      toset(keys(var.instances)) == toset(["test", "prod"]) &&
      var.instances["test"].branch == "test" && var.instances["prod"].branch == "main" &&
      length(toset([for i in var.instances : i.layer_id])) == 2 &&
      length(toset([for i in var.instances : i.cluster_id])) == 2 &&
      length(toset([for i in var.instances : i.api_resource_id])) == 2 &&
      length(toset([for i in var.instances : i.landing_schema_id])) == 2
    )
    error_message = "Use Test/test and Prod/main with distinct layers, clusters, APIs and landing namespaces."
  }
}

resource "fdp_kafka_topic" "cards" {
  for_each           = var.instances
  cluster_id         = each.value.cluster_id
  name               = "pokemon.cards"
  partitions         = 3
  replication_factor = each.value.replication_factor
  cleanup_policy     = "compact,delete"
}

resource "fdp_workload" "publish" {
  for_each       = var.instances
  repository_id  = each.value.repository_id
  branch         = each.value.branch
  program        = "pokemon-publish"
  slug           = "pokemon-events-${each.key}"
  title          = "Pokémon card observations ${each.key}"
  layer_id       = each.value.layer_id
  folder_id      = each.value.folder_id
  warehouse      = each.value.warehouse
  execution_mode = "fixed"
  env = {
    POKEMON_API   = each.value.api_name
    POKEMON_TOPIC = fdp_kafka_topic.cards[each.key].id
    POKEMON_SETS  = "base1,base2"
  }
  managed_identity_grants = {
    (each.value.api_resource_id)         = "Consumer"
    (fdp_kafka_topic.cards[each.key].id) = "Contributor"
  }
}

resource "fdp_workload" "landing" {
  for_each       = var.instances
  repository_id  = each.value.repository_id
  branch         = each.value.branch
  program        = "pokemon-stream-landing"
  slug           = "pokemon-stream-landing-${each.key}"
  title          = "Pokémon stream landing ${each.key}"
  layer_id       = each.value.layer_id
  folder_id      = each.value.folder_id
  warehouse      = each.value.warehouse
  execution_mode = "fixed"
  env = {
    POKEMON_TOPIC   = fdp_kafka_topic.cards[each.key].id
    POKEMON_LANDING = each.value.landing_name
  }
  managed_identity_grants = {
    (fdp_kafka_topic.cards[each.key].id) = "Reader"
    (each.value.landing_schema_id)       = "Contributor"
  }
}

output "topics" { value = { for env, topic in fdp_kafka_topic.cards : env => topic.id } }

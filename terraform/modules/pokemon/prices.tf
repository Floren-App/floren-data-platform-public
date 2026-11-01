# Isolated append-only landing schema: consumers receive the materialized product,
# never permission on raw Kafka events or the table writer's identity.
resource "fdp_kafka_topic" "prices" {
  count              = !var.streaming_enabled ? 0 : 1
  cluster_id         = var.kafka_cluster_id
  name               = "pokemon-card-prices-${var.environment}"
  partitions         = 3
  replication_factor = var.topic_replication_factor
  cleanup_policy     = "delete"
  retention_ms       = 604800000 # Seven days; price_events retention is managed separately.
}
resource "fdp_schema" "market" {
  count       = var.streaming_enabled ? 1 : 0
  data_source = var.data_source
  name        = "pokemon_market"
}
resource "fdp_schema" "prices" {
  count       = !var.streaming_enabled ? 0 : 1
  data_source = var.data_source
  name        = "pokemon_prices"
  append_only = true
}
locals {
  price_grants = !var.streaming_enabled ? {} : {
    monitor          = { job = "price-monitor", resource = fdp_schema.prices[0].id, role = "Reader" }
    await            = { job = "price-await", resource = fdp_schema.prices[0].id, role = "Reader" }
    bootstrap        = { job = "price-bootstrap", resource = fdp_schema.prices[0].id, role = "Admin" }
    bootstrap_market = { job = "price-bootstrap", resource = fdp_schema.market[0].id, role = "Admin" }
    publish          = { job = "price-publish", resource = fdp_kafka_topic.prices[0].id, role = "Contributor" }
    consume          = { job = "price-sink", resource = fdp_kafka_topic.prices[0].id, role = "Reader" }
    land             = { job = "price-sink", resource = fdp_schema.prices[0].id, role = "Contributor" }
    read             = { job = "price-models", resource = fdp_schema.prices[0].id, role = "Reader" }
    build            = { job = "price-models", resource = fdp_schema.market[0].id, role = "Admin" }
  }
  operator_jobs = !var.stream_operators_enabled || !var.streaming_enabled ? toset([]) : toset(concat(
    ["price-publish", "price-models", "price-monitor"], var.price_sink_enabled ? ["price-sink"] : []
  ))
}
resource "fdp_role_assignment" "stream_topic" {
  count        = !var.stream_operators_enabled || !var.streaming_enabled ? 0 : 1
  resource_id  = fdp_kafka_topic.prices[0].id
  app_group_id = var.stream_operator_group_id
  role         = "Admin"
}
resource "fdp_role_assignment" "stream_execute" {
  for_each     = local.operator_jobs
  resource_id  = fdp_workload.job[each.key].resource_id
  app_group_id = var.stream_operator_group_id
  role         = "Executor"
}
resource "fdp_data_product" "prices" {
  count      = !var.streaming_enabled ? 0 : 1
  title      = "Pokemon synthetic prices (${title(var.environment)})"
  summary    = "Synthetic EUR demo quotes, not live market valuations. Latest quote per card, with Kafka provenance."
  members    = [{ resource_id = fdp_schema.market[0].id, object_path = "card_prices_current" }]
  depends_on = [fdp_catalog_governance.this]
}
resource "fdp_role_assignment" "prices_owner" {
  count        = !var.streaming_enabled ? 0 : 1
  resource_id  = fdp_data_product.prices[0].access_resource_id
  app_group_id = coalesce(var.product_owner_group_id, var.engineers_group_id)
  role         = "Owner"
}
resource "fdp_data_product_consumer" "prices" {
  for_each     = { for index, id in concat([var.analysts_group_id], var.consumer_group_ids) : tostring(index) => id if var.streaming_enabled }
  product_id   = fdp_data_product.prices[0].id
  app_group_id = each.value
}
resource "fdp_metric" "quote_coverage" {
  count       = !var.streaming_enabled ? 0 : 1
  resource_id = fdp_schema.market[0].id
  key         = "quoted-cards"
  name        = "Cards with a synthetic quote"
  kind        = "quality"
  minimum     = 3
  source      = var.data_source
  sql         = "SELECT count(*) FROM pokemon_market.card_prices_current"
  unit        = "cards"
  depends_on  = [fdp_catalog_governance.this]
}
resource "fdp_sla_policy" "price_models" {
  count       = !var.streaming_enabled ? 0 : 1
  resource_id = fdp_workload.job["price-models"].resource_id
  policy      = jsonencode({ success_rate_percent = 99, window_hours = 24 })
}
output "price_pipeline" {
  value = !var.streaming_enabled ? null : {
    topic     = fdp_kafka_topic.prices[0].name
    raw_table = "${var.data_source}.pokemon_prices.price_events"
    product   = "${var.data_source}.pokemon_market.card_prices_current"
    bootstrap = fdp_workload.job["price-bootstrap"].slug
    publish   = fdp_workload.job["price-publish"].slug
    models    = fdp_workload.job["price-models"].slug
    monitor   = fdp_workload.job["price-monitor"].url
    sink      = var.price_sink_enabled ? fdp_workload.job["price-sink"].slug : null
  }
}

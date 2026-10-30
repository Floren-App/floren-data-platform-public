#' Governed Kafka producer
#'
#' Uses the Python SDK through reticulate, including platform identity and token refresh.
#' @export
kafka_producer <- function(topic, layer_id = NULL, ...) {
  if (!requireNamespace("reticulate", quietly = TRUE))
    stop("Kafka support needs reticulate and the Python fdp[kafka] package.")
  reticulate::import("fdp.kafka")$producer(topic, layer_id = layer_id, ...)
}

#' Governed Kafka consumer
#'
#' Commit only after durable processing. The group is prefixed with the identity.
#' @export
kafka_consumer <- function(topic, group, layer_id = NULL, ...) {
  if (!requireNamespace("reticulate", quietly = TRUE))
    stop("Kafka support needs reticulate and the Python fdp[kafka] package.")
  reticulate::import("fdp.kafka")$consumer(topic, group = group, layer_id = layer_id, ...)
}

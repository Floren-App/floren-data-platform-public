variable "environment" {
  type = string
  validation {
    condition     = contains(["test", "prod"], var.environment)
    error_message = "Use test or prod."
  }
}
variable "layer_id" { type = string }
variable "data_source" { type = string }
variable "product_schema_id" { type = string }
variable "engineers_group_id" { type = string }
variable "analysts_group_id" { type = string }
variable "repository_url" {
  type        = string
  description = "Clone URL of a repository whose root is examples/pokemon (fdp.json at root), with test and protected main branches."
}
variable "landing_bucket" {
  type        = string
  default     = "pokemon-landing"
  description = "A separate file bucket already declared in this layer's RustFS buckets (never its Iceberg bucket)."
}
variable "credentials_version" {
  type    = number
  default = 1
}
variable "repository_credentials" {
  type      = map(string)
  default   = null
  sensitive = true
  ephemeral = true
}
variable "warehouses" {
  type        = object({ app = string, job = string, orchestrator = string })
  default     = { app = "xsmall", job = "small", orchestrator = "small" }
  description = "Sizes for dashboards, batch/dbt jobs and persistent Airflow. Tune to available cluster capacity."
}
variable "demo_data_policies" {
  type        = bool
  default     = false
  description = "Demonstrate column masks and a row filter on the product: analysts and the caller-bound explorer see prices rounded, illustrators partly hidden and no rare cards; data engineers and the pipeline identities that build the product are exempt."
}
variable "levels" {
  type        = object({ internal = string, personal = string })
  default     = { internal = "internal", personal = "confidential" }
  description = "Names of the platform's classification levels (fdp_classifications) for internal data and for personal data."
}
variable "consumer_group_ids" {
  type        = list(string)
  default     = []
  description = "Further app groups that use the Pokemon cards product with its Standard option, such as dashboard viewers."
}

variable "kafka_cluster_id" {
  type        = string
  default     = null
  description = "Managed Kafka cluster in this layer; required when streaming_enabled is true."
}
variable "price_sink_enabled" {
  type        = bool
  default     = false
  description = "Enable only after running price-bootstrap once in this layer."
  validation {
    condition     = !var.price_sink_enabled || var.streaming_enabled
    error_message = "The price sink requires streaming_enabled."
  }
}
variable "topic_replication_factor" {
  type    = number
  default = 1
  validation {
    condition     = contains([1, 3, 5], var.topic_replication_factor)
    error_message = "Use 1, 3 or 5, no more than the cluster's broker count."
  }
}
variable "engineering_data_access" {
  type        = bool
  default     = true
  description = "Disable for production release managers: deploy authority does not imply interactive raw-data access."
}
variable "engineering_folder_role" {
  type    = string
  default = "Admin"
}
variable "product_owner_group_id" {
  type    = string
  default = null
}
variable "stream_operator_group_id" {
  type    = string
  default = null
}
variable "streaming_enabled" {
  type        = bool
  default     = false
  description = "Plan-time switch; when true kafka_cluster_id must identify this layer's cluster."
  validation {
    condition     = !var.streaming_enabled || var.kafka_cluster_id != null
    error_message = "Supply kafka_cluster_id when enabling streaming."
  }
}
variable "stream_operators_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.stream_operators_enabled || var.stream_operator_group_id != null
    error_message = "Supply stream_operator_group_id when enabling operator grants."
  }
}

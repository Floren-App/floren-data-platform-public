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

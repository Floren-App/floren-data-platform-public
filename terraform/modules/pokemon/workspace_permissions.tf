locals {
  developer_workspaces = var.developer_workspaces == null ? var.environment != "prod" : var.developer_workspaces
}

# Private development is explicitly enabled in this layer. Production defaults to
# no development grant; operators can enable it independently when appropriate.
resource "fdp_permission_assignment" "engineer_workspace_pages" {
  for_each     = local.developer_workspaces ? toset(["workspaces", "runs"]) : toset([])
  target_id    = each.value
  namespace    = "pages"
  layer_id     = var.layer_id
  app_group_id = var.engineers_group_id
  role         = "Reader"
}

resource "fdp_permission_assignment" "engineer_workspace_functions" {
  for_each     = local.developer_workspaces ? toset(["Contributor", "DataContributor", "Executor"]) : toset([])
  target_id    = "workspaces"
  namespace    = "workspaces"
  layer_id     = var.layer_id
  app_group_id = var.engineers_group_id
  role         = each.value
}

resource "fdp_permission_assignment" "engineer_workspace_runs" {
  count        = local.developer_workspaces ? 1 : 0
  target_id    = "workspaces"
  namespace    = "runs"
  layer_id     = var.layer_id
  app_group_id = var.engineers_group_id
  role         = "Executor"
}

resource "fdp_permission_assignment" "engineer_workspace_content" {
  for_each     = local.developer_workspaces ? toset(["artifacts", "logs"]) : toset([])
  target_id    = "workspaces"
  namespace    = each.value
  layer_id     = var.layer_id
  app_group_id = var.engineers_group_id
  role         = "DataReader"
}

resource "fdp_permission_assignment" "engineer_private_credentials" {
  for_each     = local.developer_workspaces ? { git = "git-credentials", secrets = "personal-secrets" } : {}
  target_id    = each.value
  namespace    = each.key == "git" ? "git.credentials" : "secrets"
  app_group_id = var.engineers_group_id
  actions = each.key == "git" ? [
    "git.credentials.read", "git.credentials.create", "git.credentials.update", "git.credentials.use", "git.credentials.binding.change"
  ] : ["secrets.list", "secrets.read", "secrets.create", "secrets.update", "secrets.use"]
}

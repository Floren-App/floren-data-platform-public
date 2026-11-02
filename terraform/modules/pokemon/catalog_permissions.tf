locals {
  catalog_audiences       = toset(concat([var.engineers_group_id, var.analysts_group_id, coalesce(var.product_owner_group_id, var.engineers_group_id)], var.consumer_group_ids))
  catalog_page_grants     = { for pair in setproduct(local.catalog_audiences, ["products", "requests", "cases", "catalog"]) : "${pair[0]}:${pair[1]}" => { group = pair[0], page = pair[1] } }
  catalog_function_grants = { for pair in setproduct(local.catalog_audiences, ["products.collection", "products.object", "products.options", "products.requests.own"]) : "${pair[0]}:${pair[1]}" => { group = pair[0], namespace = pair[1] } }
  request_form_grants     = { for pair in setproduct(local.catalog_audiences, ["Contributor", "Executor"]) : "${pair[0]}:${pair[1]}" => { group = pair[0], role = pair[1] } }
}

# Discovery and requesting are independent of payload access and product ownership.
resource "fdp_permission_assignment" "catalog_pages" {
  for_each     = local.catalog_page_grants
  target_id    = each.value.page
  namespace    = "pages"
  layer_id     = var.layer_id
  app_group_id = each.value.group
  role         = "Viewer"
}

resource "fdp_permission_assignment" "catalog_functions" {
  for_each     = local.catalog_function_grants
  target_id    = "products"
  namespace    = each.value.namespace
  layer_id     = var.layer_id
  app_group_id = each.value.group
  role         = each.value.namespace == "products.requests.own" ? "Executor" : "Viewer"
}

resource "fdp_permission_assignment" "request_forms" {
  for_each     = local.request_form_grants
  target_id    = "cases"
  namespace    = "compliance.cases"
  layer_id     = var.layer_id
  app_group_id = each.value.group
  role         = each.value.role
}

resource "fdp_permission_assignment" "product_owner_review" {
  target_id    = fdp_data_product.cards.access_resource_id
  namespace    = "compliance.cases"
  layer_id     = var.layer_id
  app_group_id = coalesce(var.product_owner_group_id, var.engineers_group_id)
  role         = "Manager"
}

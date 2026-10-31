# App profiles

Object permissions say what someone may open. An **app profile** says which parts of the app
a group gets at all: its pages, its navigation and the actions behind them. The platform
enforces profiles: a feature that is off is refused by the API too, not only hidden.

People get the profile of their **active group**, chosen at the bottom of the sidebar.
Switching group switches the app, just as it switches what the lists show. Running programs,
workspaces and service identities are not limited by app profiles; their object permissions apply
as before.

Platform administrators get the whole app with **Global Admin** as their active group. With
another group active they see the app as that group's profile, for example to work as the privacy
office or to check what viewers get; Administration stays in reach, and the API still lets them do
everything.

## Features

| Feature | Gives |
|---|---|
| `products.browse` | Data products: browse them and open the dashboards you can use |
| `products.request` | Request access to data products (brings `products.browse`) |
| `products.publish` | Publish data products and manage their access (brings `products.browse`) |
| `catalog.objects` | All objects in the catalog, with lineage and governance |
| `workspaces` | Workspaces, workspace runs and Git credentials |
| `workloads.view` | Workloads: browse them, run on demand, read their logs |
| `workloads.manage` | Workloads: deploy, change and organize them (brings `workloads.view`) |
| `storage` | Storage: browse data and query it with SQL |
| `packages` | Packages |
| `kafka` | Kafka |
| `apis` | API management |
| `usage` | Compute usage |
| `security` | Security: manage app groups, their members and services |

Someone without workspaces and workloads gets a simple Home with the data products and
dashboards they can use; a dashboard opens straight away.

## Profiles

The platform comes with **no** profiles: until you define them, every group gets the whole app and
object permissions decide what people may open, as before. Profiles are configuration like layers
and compute pools; declare them in Terraform. For example:

| Profile | Features |
|---|---|
| Engineer | Everything except `security` |
| Analyst | `products.request`, `catalog.objects`, `workspaces`, `workloads.view`, `storage` |
| Viewer | `products.browse` |
| Privacy | `products.browse`; with a compliance role, the privacy area comes on top (see [access policies](access-policies.md)) |

At most one profile is the **default**: groups without a profile get it. Without a default, groups
without a profile get the whole app.

## Configure

- **Access → App profiles** shows profiles against features as a grid: tick features,
  rename a profile, choose the default and add or remove profiles. A profile given to a group
  cannot be removed.
- **Access → Groups**: a group's **Settings** show its profile; administrators change it there.
- **Terraform**: `fdp_app_profile` declares a profile and `fdp_app_group.app_profile` gives it to a
  group (see the [Terraform reference](reference/terraform/fdp_app_profile.md)).

```hcl
resource "fdp_app_profile" "viewer" {
  name     = "Viewer"
  features = ["products.browse"]
}

resource "fdp_app_group" "sales_viewers" {
  name        = "Sales viewers"
  owners      = [data.fdp_principal.jesse.id]
  app_profile = fdp_app_profile.viewer.name
}
```

Changes to profiles and groups are recorded in the audit trail.

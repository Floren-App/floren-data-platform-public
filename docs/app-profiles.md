# App profiles

Object permissions say what someone may open. An **app profile** says which parts of the app
a group gets at all: its pages, its navigation and the actions behind them. The platform
enforces profiles: a feature that is off is refused by the API too, not only hidden.

People get the profile of their **active group**, chosen at the bottom of the sidebar.
Switching group switches the app, just as it switches what the lists show. Global Admin always
has everything. Running programs, workspaces and service identities are not limited by app
profiles; their object permissions apply as before.

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

The platform starts with five profiles (on a new installation; add Privacy yourself on an existing one). They are configuration like any other: rename them,
change their features, add and remove profiles.

| Profile | Features |
|---|---|
| Full access (the default) | Everything |
| Engineer | Everything except `security` |
| Analyst | Browse and request products, all objects, workspaces, view workloads, storage |
| Viewer | Browse products and open the dashboards you can use |
| Privacy | Browse products; with a compliance role, the privacy area comes on top (see [access policies](access-policies.md)) |

One profile is the **default**: groups without a profile get it. Full access is the default at
first, so nothing changes for existing groups until you give them a profile.

## Configure

- **Administration → App profiles** shows profiles against features as a grid: tick features,
  rename a profile, choose the default and add or remove profiles. A profile in use or the
  default cannot be removed.
- **Security → App groups**: a group's page shows its profile; administrators change it there.
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

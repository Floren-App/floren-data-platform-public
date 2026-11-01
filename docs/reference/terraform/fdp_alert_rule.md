# fdp_alert_rule

Generated from the current provider schema.

An alert rule: the events it sends to a channel (fdp_alert_channel), optionally only for some layers, objects (and what is under them: a workload folder, a schema, a data product) and classifications. Events: run.failed, deploy.failed, sla.breached, sla.recovered, access.requested, access.request_decided, review.waiting, review.due, review.overdue, case.approved, case.declined, case.changes_requested, case.comment, membership.waiting, access.renewal_ready, access.ending, access.ended, access.deadline, access.removed, certificate.expiring. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `channel_id` | `basetypes.StringType` | Required | The channel's ID (fdp_alert_channel.id). |
| `classifications` | `types.SetType[basetypes.StringType]` | Optional, Computed | Classifications; empty: all. |
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `events` | `types.SetType[basetypes.StringType]` | Required |  |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_ids` | `types.SetType[basetypes.StringType]` | Optional, Computed | Layer IDs; empty: every layer. |
| `name` | `basetypes.StringType` | Required |  |
| `resource_ids` | `types.SetType[basetypes.StringType]` | Optional, Computed | Objects, and what is under them; empty: all. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.

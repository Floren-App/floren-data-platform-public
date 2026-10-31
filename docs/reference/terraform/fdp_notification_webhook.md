# fdp_notification_webhook

Generated from the current provider schema.

A webhook the platform posts notifications to as JSON: a chat channel, or automation that sends email. Each message has event, subject, text, link (into the app), recipients (username and email of who should act) and, for compliance cases, a case summary; form answers are never included. Headers: X-FDP-Event, X-FDP-Delivery and, with a secret, X-FDP-Signature (sha256=<HMAC-SHA256 of the body>). Events: review.waiting, review.due, review.overdue, case.approved, case.declined, case.changes_requested, case.comment, membership.waiting, access.renewal_ready, access.ending, access.ended, access.deadline, access.removed. Import by name.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `events` | `types.SetType[basetypes.StringType]` | Optional, Computed | Events to send; empty sends all. |
| `has_secret` | `basetypes.BoolType` | Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `secret_wo` | `basetypes.StringType` | Optional, Sensitive, Write-only | Signing secret (at least 16 characters), kept in the platform's secret store. Write-only: never stored in state or returned by the API. Sent on create and whenever secret_wo_version changes; leave it out and change secret_wo_version to remove the secret. |
| `secret_wo_version` | `basetypes.Int64Type` | Optional | Change to send secret_wo again. |
| `url` | `basetypes.StringType` | Required | https:// (or http:// inside your network). |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.

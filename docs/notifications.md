# Alerts

The platform tells people when something needs them: a run failed, a data SLA is not met, someone asks
for access, a compliance form waits for review, access is about to end. It posts these events as JSON
to **channels** you configure: a Teams or Slack channel, or your own automation (n8n, Power Automate,
a ticketing system) that sends the email. The platform holds no mail account or password.

**Rules** decide which events go to which channel, and for what: only some layers, only some objects
(and everything under them: a workload folder, a schema, a data product), only some classifications.
Without channels, nothing is sent; the app itself shows what waits.

## Configure

- **Administration → Alerts**:
  - **Channels**: add one with its URL and an optional signing secret. **Send test** posts a test
    message right away; **Messages** shows the latest messages and how they went. A channel can also
    list events it always gets, everywhere; with none, it gets only what rules send it.
  - **Rules**: name, events, the channel, and optionally layers and objects.
- **An object's Alerts tab**: the owners of a workload, workload folder, schema or data product add
  rules for that object, choosing from the channels administrators set up.
- **Terraform**: `fdp_alert_channel` and `fdp_alert_rule` (see the
  [Terraform reference](reference/terraform/fdp_alert_rule.md)). `fdp_notification_webhook` is the
  earlier name of `fdp_alert_channel` and keeps working.

```hcl
resource "fdp_alert_channel" "data_ops" {
  name      = "#data-ops"
  url       = "https://automation.example.com/hooks/fdp"
  secret_wo = var.fdp_webhook_secret   # write-only: never in state; change secret_wo_version to send it again
}

resource "fdp_alert_rule" "prod_failures" {
  name       = "Failed runs and SLAs in Prod"
  events     = ["run.failed", "deploy.failed", "sla.breached"]
  channel_id = fdp_alert_channel.data_ops.id
  layer_ids  = [data.fdp_layer.prod.id]
}

resource "fdp_alert_rule" "privacy" {
  name       = "Privacy reviews"
  events     = ["review.waiting", "review.overdue", "membership.waiting", "access.ending"]
  channel_id = fdp_alert_channel.privacy_mail.id
}
```

Managing rules needs **Manager** on the Alerts page (see [who may use the Administration pages](rbac.md));
an object's owners manage the rules that name only objects they own.

The URL must be `https://` (or `http://` for a service inside your network). The signing secret is
kept in the platform's secret store. A channel or rule only gets events that happened after it was made.

## Messages

Every message is a `POST` with a JSON body:

```json
{
  "id": 42,
  "at": "2026-11-02T09:00:00+00:00",
  "event": "review.waiting",
  "subject": "Review needed: Reader on customers for Marketing",
  "text": "ana asks for Reader on customers for Marketing. It waits for your review. ...",
  "link": "https://fdp.example.com/#case=3f9a1c2b7d4e",
  "recipients": [{ "username": "priya", "email": "priya@example.com" }],
  "case": { "id": "3f9a1c2b7d4e", "kind": "role", "status": "in_review", "what": "Reader on customers for Marketing",
            "group": "Marketing", "classification": "confidential", "layer": "Prod", "requester": "ana",
            "due_at": "2026-11-12T09:00:00+00:00", "approved_until": null }
}
```

- `recipients` are the people who should act, with their email address when the platform knows it
  (from SCIM, or the `email` claim of the sign-in token): the workload's or object's owners for runs,
  deploys and SLAs, the deciders for requests and reviews, platform administrators for certificates.
  An automation that sends mail sends `subject` and `text` to each of them.
- `link` opens the right page in the app, also after signing in. `case` is there for compliance events.
- Answers to compliance forms are never included. Nothing is decided outside the app.

Headers: `X-FDP-Event` (the event), `X-FDP-Delivery` (the message id; the same on a retry) and, with
a signing secret, `X-FDP-Signature: sha256=<hex HMAC-SHA256 of the raw body with the secret>`.
Check the signature before you trust a message.

A response other than 2xx, or no response within 10 seconds, is retried at the next interval, up
to five times; then the message is kept with its error, and the channel shows that messages could
not be delivered.

## Events

| Area | Event | When |
|---|---|---|
| Runs and workloads | `run.failed` | A run of a workload failed |
| | `deploy.failed` | A workload could not be deployed or synced from Git |
| Data quality | `sla.breached` | A data SLA or quality check failed |
| | `sla.recovered` | A failed data SLA is met again |
| Access | `access.requested` | Someone asked for access to a data product |
| | `access.request_decided` | An access request was approved or declined |
| | `review.waiting` | A compliance form waits for a review step |
| | `review.due` | A review is due within a day |
| | `review.overdue` | A review is overdue |
| | `case.approved` | A compliance form was approved |
| | `case.declined` | A compliance form was declined |
| | `case.changes_requested` | Reviewers asked for changes |
| | `case.comment` | A comment on a compliance form |
| | `membership.waiting` | Someone joined a group with justified access |
| Grants ending | `access.renewal_ready` | A renewal is ready before access ends |
| | `access.ending` | Access ends soon and the renewal was not sent |
| | `access.ended` | Justified access ended |
| | `access.deadline` | Existing access needs a compliance form by a deadline |
| | `access.removed` | Access without a form was removed at its deadline |
| Platform | `certificate.expiring` | A certificate the platform serves expires within 14 days, or is invalid |

Who receives the compliance events is described under [access policies](access-policies.md#notifications).

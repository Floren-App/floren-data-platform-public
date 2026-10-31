# Notifications

The platform tells people when something waits for them, for example a compliance form to review
or access that is about to end. It posts these events as JSON to **webhooks** you configure: a Teams
or Slack channel, or your own automation (n8n, Power Automate, a ticketing system) that sends the
email. The platform holds no mail account or password.

Without webhooks, nothing is sent; the app itself shows what waits.

## Configure

- **Administration → Notifications**: add a webhook with its URL, the events it wants (none chosen:
  all) and an optional signing secret. **Send test** posts a test message right away; **Messages**
  shows the latest messages and how they went.
- **Terraform**: `fdp_notification_webhook` (see the
  [Terraform reference](reference/terraform/fdp_notification_webhook.md)).

```hcl
resource "fdp_notification_webhook" "privacy_mail" {
  name      = "Privacy mail"
  url       = "https://automation.example.com/hooks/fdp"
  events    = ["review.waiting", "review.overdue", "membership.waiting"]
  secret_wo = var.fdp_webhook_secret   # write-only: never in state; change secret_wo_version to send it again
}
```

The URL must be `https://` (or `http://` for a service inside your network). The signing secret is
kept in the platform's secret store.

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
  (from SCIM, or the `email` claim of the sign-in token). An automation that sends mail sends
  `subject` and `text` to each of them.
- `link` opens the right page in the app, also after signing in.
- Answers to compliance forms are never included. Nothing is decided outside the app.

Headers: `X-FDP-Event` (the event), `X-FDP-Delivery` (the message id; the same on a retry) and, with
a signing secret, `X-FDP-Signature: sha256=<hex HMAC-SHA256 of the raw body with the secret>`.
Check the signature before you trust a message.

A response other than 2xx, or no response within 10 seconds, is retried at the next interval, up
to five times; then the message is kept with its error, and the webhook shows that messages could
not be delivered.

## Events

| Event | When |
|---|---|
| `review.waiting` | A compliance form waits for a review step |
| `review.due` | A review is due within a day |
| `review.overdue` | A review is overdue |
| `case.approved` | A compliance form was approved |
| `case.declined` | A compliance form was declined |
| `case.changes_requested` | Reviewers asked for changes |
| `case.comment` | A comment on a compliance form |
| `membership.waiting` | Someone joined a group with justified access |
| `access.renewal_ready` | A renewal is ready before access ends |
| `access.ending` | Access ends soon and the renewal was not sent |
| `access.ended` | Justified access ended |
| `access.deadline` | Existing access needs a compliance form by a deadline |
| `access.removed` | Access without a form was removed at its deadline |

Who receives which event is described under [access policies](access-policies.md#notifications).

# Access policies and compliance forms

Some data needs a reason before anyone may use it. **Access policies** say which data: for each
layer and classification, whether access needs a **compliance form** and who reviews it. Data
in a cell without a policy needs no form, as before.

When data falls under a policy, every way of giving access needs an approved form:

- a request for a data product;
- an owner giving a group a role on a schema, source or object directly;
- an owner making a group a consumer of a data product;
- Terraform and the API (they pass the ID of an approved case as `justification_id`).

Raising an existing role to one that reaches covered data counts as giving access. Lowering a
role, or keeping one, does not.

## Which data is covered

The **classification** that counts is the one the group would really see:

- a table or object is never below its most sensitive column;
- an explicit classification on the object can raise that, never lower it;
- masked columns count as their level's *masked* level (see
  [Classifications](catalog.md#classification)).

So a product option that hashes email addresses can fall in a cell without a policy, while the
*Full* option needs a form. For a role on a schema or source, the most sensitive object under it
counts, after the masks that apply to that group.

The lowest classification never needs a form. The policy grid has one row per classification and
one column per layer, so *confidential* data can need a form in Prod and not in Non-Prod.

## A policy

Each filled cell of the grid has:

| Setting | Means |
|---|---|
| Form | The compliance form people fill in; its published version is used |
| Review steps | In order: **data owners** (the owners of the product or object), then **compliance reviewers**; each step can need more than one approval |
| Access lasts at most | Approved access ends after this many days (or earlier, when the reviewer chooses an earlier date) |
| Reviews are due within | The deadline reviewers see |
| Renewal opens | How long before the end a renewal is prepared (30 days by default) |
| Existing access must be justified within | Time given to access that needs a form and has none (30 days by default) |

Nobody reviews their own case, and one person approves a step only once.

## Forms

A form has **pages** with **questions**. Question types: short text, long text, one choice,
several choices, date, number, a statement to agree to, a person, and a file (up to 5 MB).
Questions can be required and can have help text.

A page or question can depend on an earlier answer: *show the Legal basis page when "Special
categories" is Yes*, or *when "Personal data" includes health*. Hidden questions are not asked
and not required.

Changes are saved as a **draft** and **published** as a new version. New cases use the newest
published version; cases already filled in keep theirs, so a review always shows the questions as
they were asked. A form used by a policy, or one that cases were filled in with, cannot be deleted.

## Filling in a form

When access needs a form, the app shows it at the moment access is asked for or given:

- **Request access** on a data product shows the form below the request when the chosen option
  needs one. The product's owners decide first, then the compliance reviewers.
- **Give access** on a product's access options, and **Assign role** on an object's
  permissions, show the form instead of giving access straight away. The access is given once the
  review approves it.

The form is filled in page by page; *Next* waits until the page's required questions are
answered. Optionally, say until when the access is needed.

## Compliance roles

Compliance work is done by groups with a **review duty** (a compliance role). A duty can be scoped; an empty
scope means everything.

| Role | Does | Scoped by |
|---|---|---|
| Compliance reviewer | Reviews compliance forms | layers, classifications, objects |
| Membership approver | Approves people joining a group that has access given through a form | groups |
| Compliance administrator | Sets up forms, policies, classifications and compliance roles | — |

Platform administrators can do everything and are the only ones who make compliance
administrators. Give a privacy team's group a role together with a small
[app profile](app-profiles.md), for example one with only `products.browse`, so they see their part
of the app and not the engineering tools.

## New group members

Access given through a form belongs to the group *as it was reviewed*. When someone joins the
group later, directly or through the identity provider, they do not get that access until a
membership approver approves them. Everything else the group can do, they get straight away.

## Where the privacy work happens

The daily work is under **Access**; the rules are under **Administration → Privacy**.

- **Access → To do → Reviews**: the cases waiting for your decision, the earliest deadline first;
  overdue ones are marked. *All cases* shows every case you may see, by status.
- **Access → To do → New members**: people who joined a group with access given through a form,
  waiting for a membership approver.
- **Access → Records → Access register**: what was given through a form, to which group, approved by whom and until
  when, with the number of people in the group. **Export CSV** downloads it for audits. Compliance
  administrators see all of it; reviewers what is in their scope.
- **Administration → Access policies**: the grid of layers and classifications; open a cell to set
  its form, review steps and durations.
- **Administration → Compliance forms**: the forms, their versions, and the builder: add, remove and
  reorder pages and questions, set conditions, preview, save a draft and publish.
- **Administration → Review duties**: which groups review forms or approve new members, and for what
  (the compliance roles below).
- **Administration → Classifications**: the levels and what masked data counts as.

## Reviewing a case

A case page shows what the access gives (the product and option, or the role and object), the
data's classification and layer, the people in the group, the review steps, and the answers as they
were given (questions that were hidden are left out; attached files download). The history lists
when it was sent, every decision and every comment.

Reviewers of the current step decide:

- **Approve**, optionally until an earlier date than asked. The last step's approval gives the
  access, at most for the policy's longest time.
- **Ask for changes**, with a note: the requester changes the answers and sends it again; it comes
  back to the same step.
- **Decline**, with a note.

Anyone who can see the case can comment, for example to ask a question. Requesters find their cases
under **Catalog → My requests**; owners find role grants waiting for another owner under **Access
requests**.

## End dates and renewal

Approved access ends at its end date: a product's consumer stops being one, and a role goes back to
what the group had before (or goes away). The case shows as *Expired*, and the owners and the
requester are told.

Before the end (30 days by default), the platform prepares a **renewal**: a draft on the form's
current version, filled in with the earlier answers that still apply. The requester checks the
answers and sends it; it is reviewed like a new case, and its approval moves the end date. While
nothing is sent, reminders go out 14 and 3 days before the end. Anyone who may give the access can
also choose **Renew** on an approved or expired case.

## Access without a form

Access that needs a form but has none gets a **deadline** instead of being removed at once:

- access that existed before its policy (when the platform first gets access policies, or a policy
  is added to a cell);
- access whose data was classified higher since its form was approved (or moved to a cell with a
  stricter policy).

The object's owners and the group's owners are told. The group's row on the object's
**Permissions** (or on the product's **Access options**) says *Needs a form by …* with **Justify**,
which opens the form for exactly that access. An approved case clears the deadline; at the
deadline, the access is removed. **Access → Records → Access register** lists everything waiting
for a form.

The data's owners need no form for their own use of it: a group with a role that may give access
(such as Owner) on the object, or on anything above it, also gets roles below that without a form,
and never gets a deadline. Making a group an owner of covered data does need a form.

Reviews that come due remind their reviewers; overdue reviews go to the compliance administrators
too.

## Notifications

The platform tells people what waits for them through [notification webhooks](notifications.md):
it posts each event to the webhooks you configure, for example a Teams or Slack channel, or your
own automation that sends the email. Without webhooks, the app alone shows what waits (the counts
in the Privacy and Catalog sidebars).

| Event | When | Recipients |
|---|---|---|
| `review.waiting` | A case waits for a step | The reviewers of that step (data owners, or compliance reviewers in scope; compliance administrators when nobody is in scope) |
| `case.approved`, `case.declined`, `case.changes_requested` | A decision | The requester |
| `case.comment` | A comment | The requester, or the reviewers of the current step when the requester comments |
| `membership.waiting` | Someone joins a group with justified access | The membership approvers for that group (once) |
| `access.renewal_ready`, `access.ending` | A renewal is ready; reminders 14 and 3 days before the end | The requester, the object's owners and the group's owners |
| `access.ended`, `access.removed` | Access ended, or was removed at its deadline | The same |
| `access.deadline` | Existing access needs a form (with its deadline) | The object's owners and the group's owners |
| `review.due`, `review.overdue` | A review is due tomorrow, or overdue | The reviewers; when overdue also the compliance administrators |

A message names its recipients with their email address (from SCIM, or the `email` claim of the
sign-in token) and links into the app; nothing is decided outside the app, and form answers are
never sent.

## Terraform

Everything here is configuration, also in Terraform:

```hcl
resource "fdp_access_form" "personal_data" {
  name = "Personal data access"
  definition = jsonencode({ pages = [
    { id = "purpose", title = "Purpose", fields = [
      { id = "purpose", label = "What will you use it for", type = "long_text", required = true },
      { id = "special", label = "Special categories", type = "choice", required = true, options = ["Yes", "No"] },
    ] },
    { id = "basis", title = "Legal basis", show_if = { field = "special", equals = "Yes" }, fields = [
      { id = "basis", label = "Which legal basis", type = "short_text", required = true },
    ] },
  ] })
}

resource "fdp_access_policy" "prod_confidential" {
  layer_id       = data.fdp_layer.prod.id
  classification = "confidential"
  form_id        = fdp_access_form.personal_data.id
  steps          = [{ role = "data_owner" }, { role = "compliance_reviewer" }]
  max_days       = 365
  review_days    = 10
  renew_days     = 30
  grace_days     = 30
}

resource "fdp_app_profile" "privacy" {
  name     = "Privacy"
  features = ["products.browse"]
}

resource "fdp_notification_webhook" "privacy_mail" {
  name      = "Privacy mail"
  url       = "https://automation.example.com/hooks/fdp"
  events    = ["review.waiting", "review.overdue", "membership.waiting"]
  secret_wo = var.fdp_webhook_secret
}

resource "fdp_compliance_role" "privacy_reviews_prod" {
  role            = "compliance_reviewer"
  app_group_id    = fdp_app_group.privacy.id
  layers          = [data.fdp_layer.prod.id]
  classifications = ["confidential", "restricted"]
}
```

Applying a changed form definition publishes it as a new version. Grants made by Terraform under
a policy need an approved case: set `justification_id` on `fdp_role_assignment` or
`fdp_data_product_consumer`. Data products, their access options (masks and row filters per
option) and classifications are Terraform resources too: see `fdp_data_product`,
`fdp_classifications` and `fdp_column_classification` in the
[Terraform reference](reference/terraform/index.md).

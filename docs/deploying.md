# Sharing and deploying

Access now uses inherited app-group roles and registered service identities. See [RBAC](rbac.md)
for the current authorization and execution model; the older visibility-list examples below
are retained as background and are superseded by that model.

**Workspaces are private.** Nobody but you can open your workspace, see its files or its runs.
Code is shared through **git**: commit, push, branch and merge with the git tools in VS Code or
RStudio, against your own git host. Floren Data Platform doesn't replace any of that.

To make something usable by others, **deploy** it from git.

## What can be deployed

Every program in a repository's root `fdp.json` ([reference](fdp-json.md)):

| Kind | Once deployed | Data access |
|---|---|---|
| **App** (Streamlit, Shiny) | Runs permanently at `https://<fdp host>/p/<name>/`. Anyone allowed opens it from the **Workloads** view. | The **deployer's**: viewers see what the app shows them. Deploying requires the deployer to be granted every source the program declares. |
| **Program** (Python/R script, notebook) | Appears in the catalog with its argument form. Anyone allowed runs it on a warehouse and sees **their own** runs (console and outputs). | The **person running it**: someone without a grant gets a clear refusal. |

## Deploying

In the app: **Deployed → Deploy from git**.

1. **Source:** pick a repository (registered by an admin) and a branch, then **Pull**. Floren Data Platform
   fetches the branch and lists the programs in that commit's `fdp.json`.
2. **Program:** choose one. Its kind, runtime, entrypoint and data sources are shown.
3. **Deploy:** give it a title and address (`/p/<name>/`), a warehouse, and who may use it
   (everyone with Floren Data Platform, or specific groups). Leave **Keep in sync** on for GitOps.

## GitOps: deployed things follow their branch

The control plane checks every deployed branch periodically (default every 60 s; **Check for
updates** does it immediately):

| Situation | What happens |
|---|---|
| New commit, valid `fdp.json` | **Deployed.** Apps start the new version next to the old one, switch traffic once it answers, then stop the old one (no downtime). Programs: new runs use the new commit. |
| New commit, broken (invalid config, program removed, kind changed, app doesn't start) | **Not deployed.** The current version keeps running, and the reason shows on the workload. That commit isn't retried until the branch moves on (or someone clicks Check for updates). |
| App container died | **Restarted** at the deployed commit. |
| Keep in sync turned off | The workload stays on its commit. A newer commit shows as "update available". |

Every workload has a **history**: deployed, update found, deployed, deploy failed (with the
reason), settings changed, undeployed.

Validation works per program: one broken program in `fdp.json` doesn't block deploying or
updating the others.

## Security model

- **Only the control plane talks to git**, with a read-only credential stored in OpenBao. Workloads
  download their deployed commit's source from the control plane, and each workload's token only
  unlocks its own workload.
- **Viewers are checked on every request** to a deployed app. Undeploying, or narrowing who may
  use it, takes effect immediately, even for open sessions.
- **Deployed runs get their own folder** of the shared data volume (console and outputs) and a
  per-workload environment cache. They never see each other's files.
- Apps receive the viewer's user name in the `X-FDP-User` header (Streamlit:
  `st.context.headers`), for display or per-user logic.

## Admin: registering repositories

**Administration → Git repositories**: a name, the https clone URL, the default branch, who may
deploy from it (empty = everyone), and a **read-only** user and token. Use a bot account or
deploy token; it's stored in OpenBao and never shown again.

## Git in workspaces

Your git settings, saved credentials (`credential.helper store`) and `~/.ssh` live on your
workspace volume (`.fdp/git`, `.fdp/ssh`), so they survive workspace restarts. Run
consoles/outputs, environment caches and editor files (`.fdp/`, `__pycache__/`,
`.ipynb_checkpoints/`, `.Rproj.user/`) are excluded from commits automatically.

Clone repositories into your workspace (for example `/workspace/analytics`). Their
`fdp.json` is picked up there too, and their programs show as `analytics/<program>`. So the
loop is: run in your workspace → commit and push → the deployed version updates.

## Local development

`make dev` includes **Gitea** at http://localhost:3000 with the integration account
`fdp` / `fdp123`. Create a repository explicitly and register it under
Administration → Git repositories. The optional finance demo seed
(`EXAMPLE_STORAGE=1 POKEMON_STORAGE=0 make seed`) registers `ana/analytics` in Floren Data Platform;
create that Git repository and populate it with `examples/workspace` before deploying.
The default `make seed` only provisions admin/admin.

## Service accounts and secrets

**Service accounts** are identities for data products, not people. Admins create them under
**Administration → Service accounts** with:
- **data access through groups:** grant data sources to those groups as usual;
- **who may use them:** groups of people who may run things as the account and manage its tokens
  and secrets.

Deploy an app, a dbt project or an Airflow deployment with **Run as: sa:<name>**, and production
no longer depends on a person: their leaving, changing teams or losing access doesn't break it.
The account's groups are re-read at every deploy.

**Service tokens** (`dcs_…`) let tools outside Floren Data Platform act as a service account:
`fdp.connect("finance-dw", token=…)`, or `Authorization: Bearer dcs_…` against the API. A token
is shown once, can expire, can be revoked, and can be saved straight into the account's secrets.
Its use is logged ("last used", plus every data connection in the audit log).

**Secrets** (**Security → Secrets**) are stored in OpenBao. They belong to you (private), a group
(shared) or a service account, and are never shown again after saving.

A secret is either a **string value** (API key, password, token) or a **certificate**. A certificate
can be entered as PEM (the certificate, optionally with its chain, plus an optional private key and
CA chain) or uploaded as a PKCS#12 `.p12`/`.pfx` file.

- **Checks on save:** the key must belong to the certificate. A password is only needed to open an
  encrypted key or file; it is not stored.
- **Shown afterwards:** subject, issuer, validity (with an expiry warning 30 days ahead),
  alternative names and fingerprint. Private keys are never shown. Programs reference them in
`fdp.json`:

```json
"secrets": { "OPENAI_API_KEY": "me/openai", "SMTP_PASSWORD": "group:finance/smtp" }
```

| Reference | Resolves to |
|---|---|
| `me/<name>` | The secret of whoever the program runs as: you in a workspace, the service account in a production deployment running as one. The same config works in both. |
| `group:<group>/<name>` | A group's secret; the identity must be in the group. |
| `sa:<account>/<name>` | A service account's secret; the account itself, or a person allowed to use it. |

A string arrives as the variable itself. A certificate referenced as `"FINANCE_API": "me/finance-api"`
arrives as up to three variables:

- `FINANCE_API`: the certificate (PEM);
- `FINANCE_API_KEY`: the private key, if one was stored;
- `FINANCE_API_CA`: the CA chain, if one was stored.

TLS clients usually want files, so the SDK writes them to private temporary files:

```python
c = fdp.certificate("FINANCE_API")
requests.get("https://api.finance.test", cert=(c.cert, c.key), verify=c.ca or True)
```

```r
c <- fdp::certificate("FINANCE_API")
httr2::request(url) |> httr2::req_options(sslcert = c$cert, sslkey = c$key)
```

String values, certificates and private keys printed by a program are masked as `***` in its console.

They arrive as environment variables (a Kubernetes Secret on the cluster). Every run checks
access before starting, and the values are **masked as `***`** in run consoles. A workload can
override them with production values (**Production settings**: variables and secret references),
so development and production use different credentials from the same repository.

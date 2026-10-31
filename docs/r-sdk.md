# R SDK

FDP's RStudio, R compute and Shiny images include the `fdp` R package. R support is
optional at installation time. The current R package exports its API in
[NAMESPACE](../sdk/r/fdp/NAMESPACE); Python and R feature sets are not identical.

## Data and outputs

```r
library(fdp)

con <- fdp::connect("warehouse")
cards <- DBI::dbGetQuery(con, "SELECT * FROM reporting.cards_current LIMIT 25")
write.csv(cards, file.path(fdp::output_dir(), "cards.csv"), row.names = FALSE)
DBI::dbDisconnect(con)
```

Use registered integration names and granted schemas. Connections route through
the governed query path for the configured executor; never embed a shared database
administrator credential in a Shiny application.

## Programs and the runner

`fdp::args()` returns typed run arguments. Define them in [fdp.json](fdp-json.md),
use the RStudio runner add-in or `fdp::run_program()` for configured programs, and
inspect run logs/outputs through the SDK. R scripts run to completion; Shiny apps
are long-running workloads on compute.

## Files, APIs and identities

`fdp::files()` provides governed object-storage access. `fdp::api_call()` calls a
registered API. `fdp::certificate()` exposes certificate material only under the
authorized execution's configured secret references. Do not print secret values.

Workspaces use their owner's permissions narrowed to their layer. A fixed-mode
Shiny publication uses its managed identity. Caller-mode data access must resolve
and authorize each viewer's requests; see [RBAC](rbac.md) before sharing connections
or caching results across sessions.

## Logging

`fdp::log_info()`, `fdp::log_warning()`, `fdp::log_error()` and `fdp::log_debug()` write to the
[workload log](logging.md) and print to the console: `fdp::log_info("loaded rows", rows = 120)`.

## Dependencies

Use the platform package repository and a committed `renv.lock` where applicable.
Package versions and runtime versions are part of reproducibility; changing a
local RStudio library does not update an already deployed application's environment.
See [packages](packages.md) and [runtime configuration](configuration.md#runtimes).

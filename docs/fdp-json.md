# fdp.json reference

Each workspace can have a `fdp.json` in its root (`/workspace/fdp.json`). It lists the
**programs** that can run on a warehouse, their **arguments** (the UI turns these into a form), their
**requirements** and default **warehouse**. A workspace can define as many programs as it needs.

The same programs appear in three places:
- **Floren Data Platform app:** workspace tab → Programs
- **VS Code:** the Floren Data Platform icon in the activity bar → Runner (each run opens a live console terminal)
- **RStudio:** the runner in the Viewer pane (opens at session start; reopen with `fdp::runner()`)

VS Code autocompletes and validates the file using [schemas/fdp.schema.json](../schemas/fdp.schema.json).

## Example

```json
{
  "version": 1,
  "warehouse": "small",
  "programs": {
    "sales-report": {
      "description": "Sales per region for a period",
      "entrypoint": "analysis/sales_report.py",
      "requirements": "requirements.txt",
      "warehouse": "medium",
      "data_sources": ["finance-dw"],
      "args": [
        { "name": "start_date", "type": "date", "required": true },
        { "name": "region", "type": "choice", "choices": ["All", "North", "South"], "default": "All" },
        { "name": "top", "type": "integer", "min": 1, "max": 50, "default": 5 },
        { "name": "chart", "type": "boolean", "default": true }
      ]
    }
  }
}
```

A complete example with a Python script, an R script, a notebook and a Streamlit app is in
[examples/workspace](../examples/workspace).

## Top level

| Field | Type | Meaning |
|---|---|---|
| `version` | `1` | Format version. |
| `warehouse` | string | Default warehouse for programs that don't set one. |
| `layer` | string | Default layer name or stable layer ID for programs in this file. |
| `programs` | object | Programs keyed by name (lowercase letters, digits, `-`, `_`). |

## Program

| Field | Default | Meaning |
|---|---|---|
| `entrypoint` | (required) | File relative to the workspace root. A directory is treated as a Shiny app. |
| `kind` | `script` | `script` runs to completion; `notebook` executes an `.ipynb` with papermill; `app` serves Streamlit (`.py`) or Shiny (`.R`/directory) until stopped; `dbt` and `airflow`: see [dbt and Airflow](dbt-and-airflow.md). |
| `python`, `r`, `dbt_version`, `airflow` | catalog default | Runtime version, also settable for the whole file at the top level. |
| `secrets` | `{}` | Environment variables from Floren Data Platform secrets: `{"API_KEY": "me/openai"}`. A certificate secret also sets `<VAR>_KEY` and `<VAR>_CA` ([details](deploying.md#service-accounts-and-secrets)). |
| `runtime` | inferred | `python` or `r`; inferred from the entrypoint's extension. |
| `description` | `""` | Shown in the runners. |
| `layer` | file default | Required execution layer, by name or stable ID. Other-layer programs are hidden from workspace runners and refused on execution or workload. |
| `requirements` | none | Python: a `requirements.txt`. R: a file with one CRAN package per line, or an `renv.lock`. |
| `warehouse` | config default | Default warehouse; users can pick another when running. |
| `data_sources` | `[]` | Sources the program needs. A run is refused up front if the user isn't granted them. |
| `env` | `{}` | Extra environment variables. No secrets: use data sources for those. |
| `args` | `[]` | Arguments, see below. |

## Layers

A repository can contain both Test and Prod definitions. Declare their layers explicitly:

```json
{
  "layer": "Non-Prod",
  "programs": {
    "ingest-test": { "entrypoint": "ingestion/ingest.py" },
    "ingest-prod": { "layer": "Prod", "entrypoint": "ingestion/ingest.py" }
  }
}
```

The Test definition inherits Non-Prod. The Prod definition overrides it and is absent from
Non-Prod workspace runners, including the desktop app, VS Code, RStudio and CLI. Direct API
requests and Airflow tasks cannot bypass this restriction. Deploying and GitOps also require
the declared layer to match the workload's existing layer; a code change cannot move it.

Names are matched without case sensitivity. Stable IDs from Administration → Layers remain
valid after a layer is renamed. Unknown layers produce a configuration error for the affected
program. If both declarations are omitted, the program uses its workspace/workload layer
for compatibility. A declaration only restricts availability: it never grants access or changes
the server-derived layer. Data, API, identity and RBAC checks remain required, including for
ad-hoc file runs outside the program list.

## Arguments

| Field | Meaning |
|---|---|
| `name` | Identifier (`start_date`). The key in `fdp.args()`. |
| `type` | `string`, `text`, `integer`, `number`, `boolean`, `choice`, `date` (YYYY-MM-DD), `data_source` (pick one of your granted sources). |
| `label`, `help` | Shown in the form. |
| `required`, `default` | A required argument without a default must be filled in. |
| `choices` | Options for `choice`. |
| `min`, `max` | Limits for `integer`/`number`. |
| `pattern` | Regular expression a string must fully match. |
| `flag` | Command-line flag; default `--<name>` with `_` → `-` (`--start-date`). |
| `positional` | Pass the value as a positional argument instead of a flag. |

Values are validated on the server before anything starts, and every problem is reported at once,
next to its field.

### Reading arguments in your program

Arguments arrive two ways, so use whichever suits you:

- **As command-line flags:** `--start-date 2026-01-01 --region All --top 5 --chart`. Booleans are a
  bare flag when true and are left out when false. This works with `argparse`, `click`, `optparse`
  and R's `commandArgs()`.
- **Typed, via the SDK:** `fdp.args()` (Python) or `fdp::args()` (R) returns
  `{"start_date": "2026-01-01", "top": 5, "chart": True}`.

Notebooks receive them as papermill parameters: tag a cell `parameters`. Streamlit gets the flags
after `--`. Shiny apps read `fdp::args()`.

## Requirements

The first run with a given requirements file installs it into a cached environment on the
workspace volume (`.fdp/envs/`). Later runs reuse it instantly, until the file changes:
the cache key is a hash of the file plus the Python/R version. Packages already in the compute
image don't need to be listed. Two runs starting at once wait for a single install rather than
racing.

On Kubernetes, workloads have no internet access, so installs go to package mirrors. Set
`FDP_PIP_INDEX_URL` and `FDP_CRAN_MIRROR` on the control plane (for example devpi and a
CRAN mirror inside the cluster).

## Console, outputs and stopping

- **Console:** everything the program prints streams live to the runners. It's also saved to
  `.fdp/runs/<run-id>/console.log`, so it survives after the warehouse shuts down.
- **Outputs:** files written to `$FDP_OUTPUT_DIR` (`fdp.output_dir()`) are listed under
  the run, with previews for CSV, text and images. In the editor they're at
  `.fdp/runs/<run-id>/outputs/`. An executed notebook is saved there too.
- **Stop:** any runner (or Ctrl+C in the VS Code console terminal) sends the program SIGTERM and
  gives it 5 seconds to finish before it is killed. To save partial results, handle the signal:

  ```python
  import signal, sys
  signal.signal(signal.SIGTERM, lambda *_: (save_partial(), sys.exit(0)))
  ```

## From a terminal

```sh
fdp programs                                    # what fdp.json defines
fdp start sales-report start_date=2026-01-01 region=North --warehouse large
fdp logs <run-id> -f
fdp stop <run-id>
```

In R: `fdp::run_program("region-summary", list(min_amount = 100), warehouse = "large")`.

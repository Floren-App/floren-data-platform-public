# Programs from fdp.json: arguments inside a run, starting runs, following their console.

#' Typed argument values for this run (empty list outside a run).
args <- function() {
  raw <- Sys.getenv("FDP_ARGS", "{}")
  jsonlite::fromJSON(raw, simplifyVector = TRUE)
}

#' Where to write files that should show up as run outputs.
output_dir <- function() {
  path <- Sys.getenv("FDP_OUTPUT_DIR", ".")
  dir.create(path, showWarnings = FALSE, recursive = TRUE)
  path
}

#' Answer the internal API call that started this run: `data` becomes the caller's result and `status`
#' the HTTP status code, e.g. respond(list(error = "unknown region"), status = 404) to answer with an error.
#' Data frames become a list of rows. Returns the path of response.json (invisibly).
respond <- function(data = NULL, status = 200L) {
  if (!is.numeric(status) || length(status) != 1 || status < 200 || status > 599 || status %in% c(204, 304)) {
    stop("status must be an HTTP status code from 200 to 599 that carries a body (not 204 or 304)")
  }
  body <- jsonlite::toJSON(data, auto_unbox = TRUE, dataframe = "rows", POSIXt = "ISO8601", digits = NA, null = "null", na = "null")
  if (nchar(body, type = "bytes") > 10 * 1024^2) stop("the answer is larger than the 10 MB internal APIs return")
  folder <- output_dir()
  for (item in list(c("response.json", body), c("response.status", as.character(as.integer(status))))) {
    path <- file.path(folder, item[[1]])
    writeLines(item[[2]], paste0(path, ".tmp"), sep = "", useBytes = TRUE)  # never serve a half-written answer
    file.rename(paste0(path, ".tmp"), path)
  }
  invisible(file.path(folder, "response.json"))
}

#' This workspace's fdp.json as the control plane sees it (validated).
programs <- function() .get(paste0("v1/workspaces/", Sys.getenv("FDP_WORKSPACE_ID"), "/config"))

#' Warehouses (compute pools) you can run on.
warehouses <- function() .get("v1/pools")

#' Run a program from fdp.json, e.g. run_program("region-summary", list(region = "North")).
run_program <- function(program, args = list(), warehouse = NULL, follow = interactive()) {
  body <- list(workspace_id = Sys.getenv("FDP_WORKSPACE_ID"), program = program,
               args = if (length(args)) args else structure(list(), names = character()))
  if (!is.null(warehouse)) body$pool <- warehouse
  resp <- .request("v1/runs") |>
    httr2::req_body_json(body, auto_unbox = TRUE) |>
    httr2::req_error(is_error = function(r) FALSE) |>
    httr2::req_perform()
  out <- httr2::resp_body_json(resp)
  if (httr2::resp_status(resp) >= 400) {
    stop("cannot start ", program, ": ", jsonlite::toJSON(out$detail, auto_unbox = TRUE), call. = FALSE)
  }
  message(sprintf("started %s as run %s on warehouse %s", program, out$id, out$pool_name))
  if (follow) follow(out$id)
  invisible(out)
}

#' Runs started from this workspace (newest first).
workspace_runs <- function() {
  .request("v1/runs") |>
    httr2::req_url_query(workspace_id = Sys.getenv("FDP_WORKSPACE_ID")) |>
    httr2::req_perform() |>
    httr2::resp_body_json()
}

#' A run's console so far.
run_logs <- function(run_id, tail = 20000) {
  .request(paste0("v1/runs/", run_id, "/logs")) |>
    httr2::req_url_query(tail = tail) |>
    httr2::req_perform() |>
    httr2::resp_body_string()
}

#' Files a run wrote to its output directory.
run_outputs <- function(run_id) {
  resp <- .request(paste0("v1/runs/", run_id, "/outputs")) |>
    httr2::req_error(is_error = function(r) FALSE) |>
    httr2::req_perform()
  if (httr2::resp_status(resp) >= 400) return(list())
  httr2::resp_body_json(resp)
}

#' Stop a running program (it gets a few seconds to clean up).
stop_run <- function(run_id) {
  .request(paste0("v1/runs/", run_id, "/stop")) |>
    httr2::req_method("POST") |>
    httr2::req_perform() |>
    httr2::resp_body_json()
}

#' Create a starter fdp.json (and hello.R) in the workspace.
init <- function(path = "/workspace") {
  cfg <- file.path(path, "fdp.json")
  if (!file.exists(cfg)) {
    writeLines(c(
      "{",
      '  "version": 1,',
      '  "programs": {',
      '    "hello": {',
      '      "description": "Example program: replace with your own",',
      '      "entrypoint": "hello.R",',
      '      "args": [{ "name": "name", "type": "string", "default": "world" }]',
      "    }",
      "  }",
      "}"), cfg)
    hello <- file.path(path, "hello.R")
    if (!file.exists(hello)) writeLines(c('a <- fdp::args()', 'cat("Hello,", a$name, "\\n")'), hello)
  }
  invisible(cfg)
}

#' Print a run's console until it finishes.
follow <- function(run_id, interval = 2) {
  seen <- 0L
  repeat {
    logs <- run_logs(run_id)
    if (nchar(logs) > seen) cat(substring(logs, seen + 1L))
    seen <- nchar(logs)
    status <- .get(paste0("v1/runs/", run_id))$status
    if (!status %in% c("pending", "running")) {
      message("[", status, "]")
      return(invisible(status))
    }
    Sys.sleep(interval)
  }
}

#' Open the Floren Data Platform runner in RStudio's Viewer pane (runs as a background job).
runner <- function() {
  if (!requireNamespace("rstudioapi", quietly = TRUE) || !rstudioapi::isAvailable()) {
    stop("the runner panel needs RStudio; use run_program() from the console instead", call. = FALSE)
  }
  port <- httpuv::randomPort()
  script <- tempfile("fdp-runner-", fileext = ".R")
  writeLines(sprintf(
    "shiny::runApp(system.file('runner', package = 'fdp'), port = %dL, host = '127.0.0.1', launch.browser = FALSE)",
    port), script)
  rstudioapi::jobRunScript(script, name = "Floren Data Platform runner", workingDir = "/workspace")
  address <- sprintf("http://127.0.0.1:%d", port)
  is_up <- function() {
    con <- url(address)
    on.exit(try(close(con), silent = TRUE))
    tryCatch({ suppressWarnings(readLines(con, n = 1, warn = FALSE)); TRUE }, error = function(e) FALSE)
  }
  for (i in 1:60) {
    if (is_up()) break
    Sys.sleep(0.5)
  }
  rstudioapi::viewer(address)  # RStudio Server proxies localhost addresses for the Viewer pane
  invisible(address)
}

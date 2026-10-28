# Floren Data Platform SDK for R.
#
#   con <- fdp::connect("finance-dw")       # DBI connection
#   DBI::dbGetQuery(con, "select * from sales")
#   fdp::run_on("large", "models/train.R")  # run on a bigger compute pool

.request <- function(path) {
  api <- Sys.getenv("FDP_API")
  token <- Sys.getenv("FDP_SESSION_TOKEN")
  if (api == "" || token == "") {
    stop("fdp only works inside a Floren Data Platform workspace or run", call. = FALSE)
  }
  parts <- strsplit(token, ".", fixed = TRUE)[[1]]
  if (length(parts) == 3L) {
    claims <- tryCatch({
      payload <- chartr("-_", "+/", parts[2])
      payload <- paste0(payload, strrep("=", (4L - nchar(payload) %% 4L) %% 4L))
      jsonlite::fromJSON(rawToChar(openssl::base64_decode(payload)))
    }, error = function(e) NULL)
    if (!is.null(claims$run) && nzchar(claims$run) && claims$exp < as.numeric(Sys.time()) + 60) {
      renewed <- httr2::request(paste0(api, "/v1/executions/token")) |>
        httr2::req_method("POST") |> httr2::req_auth_bearer_token(token) |>
        httr2::req_perform() |> httr2::resp_body_json()
      token <- renewed$token
      Sys.setenv(FDP_SESSION_TOKEN = token)
    }
  }
  httr2::request(api) |>
    httr2::req_url_path_append(path) |>
    httr2::req_auth_bearer_token(token)
}

.get <- function(path) {
  .request(path) |> httr2::req_perform() |> httr2::resp_body_json()
}

#' Data sources your groups have been granted.
sources <- function() {
  items <- .get("v1/connect")
  data.frame(
    name = vapply(items, `[[`, "", "name"),
    kind = vapply(items, `[[`, "", "kind"),
    description = vapply(items, `[[`, "", "description")
  )
}

#' Raw connection info (kind, config, credentials). Prefer connect().
credentials <- function(name, schema = NULL, viewer_token = NULL) {
  path <- paste0("v1/connect/", utils::URLencode(name, reserved = TRUE))
  req <- .request(path)
  if (!is.null(schema)) req <- httr2::req_url_query(req, schema = schema)
  if (!is.null(viewer_token)) req <- httr2::req_headers(req, `X-FDP-Viewer` = viewer_token)
  req |> httr2::req_perform() |> httr2::resp_body_json()
}

#' A ready DBI connection for SQL sources.
connect <- function(name, ..., schema = NULL, viewer_token = NULL) {
  info <- credentials(name, schema = schema, viewer_token = viewer_token)
  cfg <- info$config
  cred <- info$credentials
  switch(info$kind,
    trino = .sql_connect(info, viewer_token = viewer_token),
    postgres = DBI::dbConnect(RPostgres::Postgres(),
      host = cfg$host, port = cfg$port, dbname = cfg$database,
      user = cred$username, password = cred$password, ...),
    mysql = DBI::dbConnect(RMariaDB::MariaDB(),
      host = cfg$host, port = cfg$port, dbname = cfg$database,
      user = cred$username, password = cred$password, ...),
    mssql = DBI::dbConnect(odbc::odbc(),
      driver = cfg$driver %||% "ODBC Driver 18 for SQL Server",
      server = cfg$host, port = cfg$port, database = cfg$database,
      uid = cred$username, pwd = cred$password, ...),
    stop(sprintf("no built-in client for '%s'; use fdp::credentials('%s')", info$kind, name),
         call. = FALSE)
  )
}

#' Run a script, notebook or Shiny app from this workspace on a compute pool.
run_on <- function(pool, entrypoint, kind = c("script", "notebook", "app")) {
  kind <- match.arg(kind)
  .request("v1/runs") |>
    httr2::req_body_json(list(
      workspace_id = Sys.getenv("FDP_WORKSPACE_ID"),
      pool = pool, kind = kind, entrypoint = entrypoint
    )) |>
    httr2::req_perform() |>
    httr2::resp_body_json()
}

runs <- function() .get("v1/runs")

`%||%` <- function(a, b) if (is.null(a)) b else a

#' Request-scoped data access for caller-mode shared apps.
query <- function(source, sql, parameters = list(), viewer_token = NULL, max_rows = 1000L) {
  body <- list(source = source, sql = sql, max_rows = max_rows, parameters = unname(parameters))
  req <- .request(if (is.null(viewer_token)) "v1/sql/query" else "v1/delegated/query")
  if (!is.null(viewer_token)) req <- httr2::req_headers(req, `X-FDP-Viewer` = viewer_token)
  req |> httr2::req_body_json(body, auto_unbox = TRUE) |>
    httr2::req_perform() |> httr2::resp_body_json()
}

#' Write a certificate secret to private files for TLS clients.
#'
#' With "secrets": {"VAR": "me/name"} in fdp.json the run receives the certificate as VAR, the
#' private key as VAR_KEY and the CA chain as VAR_CA. Returns list(cert, key, ca) of file paths
#' (key and ca are NULL when absent), e.g. httr2::req_options(sslcert = c$cert, sslkey = c$key).
#' @export
certificate <- function(var) {
  pem <- Sys.getenv(var)
  if (!nzchar(pem) || !grepl("BEGIN CERTIFICATE", pem, fixed = TRUE)) {
    stop(var, " is not a certificate secret of this run; add it under 'secrets' in fdp.json")
  }
  folder <- tempfile(paste0("fdp-", tolower(var), "-"))
  dir.create(folder, mode = "0700")
  write <- function(name, content) {
    if (!nzchar(content)) return(NULL)
    path <- file.path(folder, name)
    old <- Sys.umask("077"); on.exit(Sys.umask(old), add = TRUE)
    writeLines(content, path, sep = "")
    path
  }
  list(cert = write("cert.pem", pem), key = write("key.pem", Sys.getenv(paste0(var, "_KEY"))),
       ca = write("ca.pem", Sys.getenv(paste0(var, "_CA"))))
}

#' Call an operation of an external API registered in API management, as this program's identity.
#'
#' Floren Data Platform adds the API's headers, credentials and client certificate; the program never holds them.
#' REST: api_call("crm", "GET", "/customers/7"). SOAP: api_call("bank", "POST", "GetBalance", body = "<GetBalance>...</GetBalance>").
#' Returns the httr2 response (errors are not raised, so check httr2::resp_status()).
#' @export
api_call <- function(api, method = "GET", path = "/", query = list(), body = NULL, headers = list()) {
  req <- .request(paste0("v1/egress/", api, "/", sub("^/", "", path))) |>
    httr2::req_method(method) |> httr2::req_error(is_error = function(resp) FALSE)
  if (length(query)) req <- do.call(httr2::req_url_query, c(list(req), query))
  if (length(headers)) req <- do.call(httr2::req_headers, c(list(req), headers))
  if (is.character(body)) req <- httr2::req_body_raw(req, body, type = "text/xml")
  else if (!is.null(body)) req <- httr2::req_body_json(req, body)
  httr2::req_perform(req)
}

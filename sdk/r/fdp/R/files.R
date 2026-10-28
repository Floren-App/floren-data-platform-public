# Files in S3-compatible data sources (RustFS, MinIO, S3), within the granted bucket/prefix.
#
#   f <- fdp::files("lake-files")
#   f$write_json("reports/today.json", list(ok = TRUE))
#   f$read_text("notes/readme.txt")
#   f$list("reports/")
#   df <- f$read_csv("exports/sales.csv")
#
# Requests are signed with AWS Signature V4 (implemented here on openssl, which httr2 already
# depends on), so this works with any httr2 version.

.aws_enc <- function(x, slash = FALSE) {
  out <- vapply(x, function(s) {
    chars <- strsplit(enc2utf8(s), "")[[1]]
    keep <- grepl("^[A-Za-z0-9._~-]$", chars) | (slash & chars == "/")
    enc <- vapply(chars, function(ch) paste0("%", toupper(as.character(charToRaw(ch))), collapse = ""), "")
    paste0(ifelse(keep, chars, enc), collapse = "")
  }, "")
  unname(out)
}

.hmac <- function(key, msg) as.raw(openssl::sha256(charToRaw(msg), key = key))

# A signed S3 request (AWS Signature V4, path-style).
.s3_request <- function(endpoint, region, cred, method, path, query = list(), body = raw(0), type = NULL) {
  query <- query[!vapply(query, is.null, TRUE)]
  host <- sub("^https?://", "", endpoint)
  now <- as.POSIXlt(Sys.time(), tz = "UTC")
  amz_date <- format(now, "%Y%m%dT%H%M%SZ")
  day <- format(now, "%Y%m%d")
  payload <- paste(as.character(openssl::sha256(body)), collapse = "")
  canon_q <- if (length(query)) {
    q <- query[order(names(query))]
    paste(.aws_enc(names(q)), .aws_enc(as.character(unlist(q))), sep = "=", collapse = "&")
  } else ""
  canon_path <- .aws_enc(path, slash = TRUE)
  signed_headers <- "host;x-amz-content-sha256;x-amz-date"
  canonical_headers <- paste0("host:", host, "\nx-amz-content-sha256:", payload, "\nx-amz-date:", amz_date, "\n")
  if (!is.null(cred$session_token) && nzchar(cred$session_token)) {
    canonical_headers <- paste0(canonical_headers, "x-amz-security-token:", cred$session_token, "\n")
    signed_headers <- paste0(signed_headers, ";x-amz-security-token")
  }
  canonical <- paste(method, canon_path, canon_q, canonical_headers, signed_headers, payload, sep = "\n")
  scope <- paste(day, region, "s3", "aws4_request", sep = "/")
  to_sign <- paste("AWS4-HMAC-SHA256", amz_date, scope,
                   paste(as.character(openssl::sha256(charToRaw(canonical))), collapse = ""), sep = "\n")
  key <- .hmac(.hmac(.hmac(.hmac(charToRaw(paste0("AWS4", cred$secret_access_key)), day), region), "s3"), "aws4_request")
  signature <- paste(as.character(openssl::sha256(charToRaw(to_sign), key = key)), collapse = "")
  url <- paste0(endpoint, canon_path, if (nzchar(canon_q)) paste0("?", canon_q))
  req <- httr2::request(url) |>
    httr2::req_method(method) |>
    httr2::req_headers(`x-amz-date` = amz_date, `x-amz-content-sha256` = payload,
                       Authorization = paste0("AWS4-HMAC-SHA256 Credential=", cred$access_key_id, "/", scope,
                                              ", SignedHeaders=", signed_headers, ", Signature=", signature))
  if (!is.null(cred$session_token)) req <- httr2::req_headers(req, `x-amz-security-token` = cred$session_token)
  if (length(body)) req <- httr2::req_body_raw(req, body, type = type %||% "application/octet-stream")
  httr2::req_perform(req)
}

# Which catalog files a deployed job reads and writes; reported automatically when R exits (and in
# batches), so nothing needs declaring in fdp.json. Best-effort: never fails a job, never grants access.
.file_lineage <- new.env()
.file_lineage$pending <- list()

.flush_file_lineage <- function(...) {
  items <- .file_lineage$pending
  if (!length(items)) return(invisible())
  .file_lineage$pending <- list()
  refs <- function(field) unname(lapply(Filter(function(x) x$field == field, items), function(x) x[c("source", "schema", "name")]))
  try(.request("v1/catalog/ingest/program") |>
        httr2::req_body_json(list(inputs = refs("inputs"), outputs = refs("outputs")), auto_unbox = TRUE) |>
        httr2::req_perform(), silent = TRUE)
  invisible()
}
reg.finalizer(.file_lineage, .flush_file_lineage, onexit = TRUE)

.note_file <- function(source, namespaces, path, field) {
  if (!nzchar(Sys.getenv("FDP_WORKLOAD_ID")) || !length(namespaces)) return(invisible())
  prefixes <- vapply(namespaces, function(p) gsub("^/+|/+$", "", p), "")
  hits <- names(prefixes)[!nzchar(prefixes) | path == prefixes | startsWith(path, paste0(prefixes, "/"))]
  if (!length(hits)) return(invisible())
  best <- hits[which.max(nchar(prefixes[hits]))]
  rel <- if (nzchar(prefixes[[best]])) sub("^/+", "", substring(path, nchar(prefixes[[best]]) + 1)) else path
  if (!nzchar(rel)) return(invisible())
  .file_lineage$pending[[paste(field, source, best, rel, sep = "\r")]] <- list(field = field, source = source, schema = best, name = rel)
  if (length(.file_lineage$pending) >= 200) .flush_file_lineage()
  invisible()
}

files <- function(name, viewer_token = NULL) {
  if (!is.null(viewer_token)) return(.delegated_files(name, viewer_token))
  info <- credentials(name)
  if (!info$kind %in% c("s3", "iceberg")) {
    stop(name, " is a ", info$kind, " data source, not object storage", call. = FALSE)
  }
  cfg <- info$config
  cred <- info$credentials
  if (info$kind == "iceberg") {      # the storage behind an Iceberg catalog
    loc <- sub("^s3://", "", cfg$storage %||% "")
    cfg <- list(endpoint_url = cfg$s3_endpoint, region = cfg$region,
                bucket = sub("/.*$", "", loc), prefix = if (grepl("/", loc)) sub("^[^/]*/", "", loc) else "")
  }
  endpoint <- sub("/+$", "", cfg$endpoint_url %||% "https://s3.amazonaws.com")
  region <- cfg$region %||% "us-east-1"
  bucket <- cfg$bucket
  prefix <- gsub("^/+|/+$", "", cfg$prefix %||% "")
  namespaces <- info$access$namespaces %||% list()
  rel <- function(path) paste(Filter(function(p) nzchar(p) && p != ".", strsplit(gsub("^/+", "", path), "/", fixed = TRUE)[[1]]), collapse = "/")

  key <- function(path) {
    parts <- strsplit(gsub("^/+", "", path), "/", fixed = TRUE)[[1]]
    if (any(parts == "..")) stop("paths may not contain '..'", call. = FALSE)
    path <- paste(parts[nzchar(parts) & parts != "."], collapse = "/")
    if (nzchar(prefix) && nzchar(path)) paste0(prefix, "/", path) else paste0(prefix, path)
  }
  s3 <- function(method, k = "", ...) {
    .s3_request(endpoint, region, cred, method, paste0("/", bucket, if (nzchar(k)) paste0("/", k)), ...)
  }
  get <- function(path) {
    data <- s3("GET", key(path)) |> httr2::resp_body_raw()
    .note_file(name, namespaces, rel(path), "inputs")
    data
  }
  put <- function(path, body, type) {
    s3("PUT", key(path), body = body, type = type)
    .note_file(name, namespaces, rel(path), "outputs")
    invisible(paste0("s3://", bucket, "/", key(path)))
  }

  list_files <- function(path = "") {
    base <- key(path)
    if (nzchar(base) && !endsWith(base, "/")) base <- paste0(base, "/")
    out <- character()
    token <- NULL
    repeat {
      r <- s3("GET", query = list(`list-type` = "2", prefix = base, `continuation-token` = token)) |>
        httr2::resp_body_string()
      keys <- regmatches(r, gregexpr("<Key>[^<]*</Key>", r))[[1]]
      out <- c(out, gsub("</?Key>", "", keys))
      if (!grepl("<IsTruncated>true</IsTruncated>", r)) break
      token <- sub(".*<NextContinuationToken>([^<]*)</NextContinuationToken>.*", "\\1", r)
    }
    if (nzchar(prefix)) substring(out, nchar(prefix) + 2) else out
  }

  structure(list(
    name = name,
    url = function(path = "") paste0("s3://", bucket, "/", key(path)),
    list = list_files,
    read_raw = get,
    read_text = function(path) rawToChar(get(path)),
    read_json = function(path, ...) jsonlite::fromJSON(rawToChar(get(path)), ...),
    read_csv = function(path, ...) utils::read.csv(text = rawToChar(get(path)), ...),
    write_raw = function(path, data, type = "application/octet-stream") put(path, data, type),
    write_text = function(path, text) put(path, charToRaw(enc2utf8(paste(text, collapse = "\n"))), "text/plain; charset=utf-8"),
    write_json = function(path, x, ...) put(path, charToRaw(jsonlite::toJSON(x, auto_unbox = TRUE, pretty = TRUE, ...)), "application/json"),
    write_csv = function(path, df, ...) {
      con <- textConnection("csv", "w", local = TRUE)
      utils::write.csv(df, con, row.names = FALSE, ...)
      close(con)
      put(path, charToRaw(paste(csv, collapse = "\n")), "text/csv")
    },
    delete = function(path) invisible(s3("DELETE", key(path)))
  ), class = "fdp_files")
}

print.fdp_files <- function(x, ...) cat("<fdp files ", x$name, ": ", x$url(), ">\n", sep = "")


.delegated_files <- function(name, viewer_token) {
  call <- function(operation, path = "", ...) {
    body <- c(list(source = name, operation = operation, path = path), list(...))
    .request("v1/delegated/files") |>
      httr2::req_headers(`X-FDP-Viewer` = viewer_token) |>
      httr2::req_body_json(body) |> httr2::req_perform() |> httr2::resp_body_json()
  }
  get <- function(path) openssl::base64_decode(call("read", path)$data)
  put <- function(path, data, type) {
    call("write", path, data = openssl::base64_encode(data), content_type = type)
    invisible(path)
  }
  list(
    name = name,
    list = function(path = "") {
      result <- call("list", path)
      if (isTRUE(result$truncated)) stop("more than 1000 entries; choose a narrower prefix", call. = FALSE)
      vapply(result$files, function(x) x$path, "")
    },
    read_raw = get,
    read_text = function(path) rawToChar(get(path)),
    read_json = function(path, ...) jsonlite::fromJSON(rawToChar(get(path)), ...),
    read_csv = function(path, ...) utils::read.csv(text = rawToChar(get(path)), ...),
    write_raw = function(path, data, type = "application/octet-stream") put(path, data, type),
    write_text = function(path, text) put(path, charToRaw(enc2utf8(paste(text, collapse = "\n"))), "text/plain; charset=utf-8"),
    write_json = function(path, x, ...) put(path, charToRaw(jsonlite::toJSON(x, auto_unbox = TRUE, ...)), "application/json"),
    delete = function(path) invisible(call("delete", path))
  )
}

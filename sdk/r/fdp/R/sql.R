# A DBI client for the standard Trino HTTP protocol through Floren Data Platform.
methods::setClass("FDPConnection", contains = "DBIConnection", slots = c(state = "environment"))
methods::setClass("FDPResult", contains = "DBIResult", slots = c(state = "environment"))

.sql_connect <- function(info, viewer_token = NULL) {
  state <- new.env(parent = emptyenv())
  state$info <- info
  state$viewer <- viewer_token
  state$open <- TRUE
  origin <- Sys.getenv("FDP_SQL_URL", Sys.getenv("FDP_API", info$config$endpoint))
  state$origin <- sub("/api/?$", "", sub("/$", "", origin))
  methods::new("FDPConnection", state = state)
}

.sql_http <- function(con, method, url, statement = NULL) {
  if (!con@state$open) stop("SQL connection is closed", call. = FALSE)
  # Signed continuations are returned by our gateway, never by an arbitrary server.
  if (!startsWith(url, paste0(con@state$origin, "/v1/"))) stop("invalid SQL continuation", call. = FALSE)
  req <- httr2::req_url(.request("v1/statement"), url) |>
    httr2::req_method(method) |>
    httr2::req_headers(`X-Trino-Catalog` = con@state$info$config$catalog,
                      `X-Trino-Schema` = con@state$info$config$schema,
                      `X-Trino-User` = con@state$info$credentials$username)
  if (!is.null(con@state$viewer)) req <- httr2::req_headers(req, `X-FDP-Viewer` = con@state$viewer)
  if (!is.null(statement)) req <- httr2::req_body_raw(req, statement, type = "text/plain")
  response <- httr2::req_perform(req)
  if (httr2::resp_status(response) == 204L) return(NULL)
  page <- jsonlite::fromJSON(httr2::resp_body_string(response), simplifyVector = FALSE, bigint_as_char = TRUE)
  if (!is.null(page$error)) stop(page$error$message, call. = FALSE)
  page
}

.sql_absorb <- function(result, page) {
  if (length(page$columns)) result@state$columns <- page$columns
  if (length(page$data)) result@state$rows <- c(result@state$rows, page$data)
  result@state$next_url <- page$nextUri
  result@state$affected <- page$updateCount %||% result@state$affected
  result@state$query_id <- page$id
  invisible(result)
}

methods::setMethod("dbIsValid", "FDPConnection", function(dbObj, ...) dbObj@state$open)
methods::setMethod("dbDisconnect", "FDPConnection", function(conn, ...) {
  conn@state$open <- FALSE
  TRUE
})
.sql_literal <- function(value) {
  if (is.null(value) || (length(value) == 1L && is.na(value))) return("NULL")
  if (length(value) != 1L) stop("SQL parameters must be scalar", call. = FALSE)
  if (is.logical(value)) return(if (value) "TRUE" else "FALSE")
  if (inherits(value, "Date")) return(paste0("DATE '", format(value, "%Y-%m-%d"), "'"))
  if (is.numeric(value)) {
    if (!is.finite(value)) stop("SQL parameters must be finite", call. = FALSE)
    return(format(value, scientific = FALSE, trim = TRUE))
  }
  paste0("'", gsub("'", "''", as.character(value), fixed = TRUE), "'")
}

methods::setMethod("dbSendQuery", c("FDPConnection", "character"), function(conn, statement, ..., params = NULL) {
  if (length(params)) {
    statement <- paste0("EXECUTE IMMEDIATE ", .sql_literal(statement), " USING ",
                        paste(vapply(params, .sql_literal, ""), collapse = ", "))
  }
  state <- new.env(parent = emptyenv())
  state$con <- conn
  state$columns <- list()
  state$rows <- list()
  state$affected <- 0L
  state$open <- TRUE
  state$fetched <- 0L
  result <- methods::new("FDPResult", state = state)
  .sql_absorb(result, .sql_http(conn, "POST", paste0(conn@state$origin, "/v1/statement"), statement))
  result
})
methods::setMethod("dbSendStatement", c("FDPConnection", "character"), function(conn, statement, ...) {
  DBI::dbSendQuery(conn, statement, ...)
})
methods::setMethod("dbFetch", "FDPResult", function(res, n = -1, ...) {
  if (!res@state$open) stop("SQL result is closed", call. = FALSE)
  while (!is.null(res@state$next_url) && (n < 0 || length(res@state$rows) < n || !length(res@state$columns))) {
    .sql_absorb(res, .sql_http(res@state$con, "GET", res@state$next_url))
  }
  take <- if (n < 0) length(res@state$rows) else min(length(res@state$rows), n)
  rows <- head(res@state$rows, take)
  res@state$rows <- if (take) res@state$rows[-seq_len(take)] else res@state$rows
  res@state$fetched <- res@state$fetched + take
  # Preserve precision for decimal/bigint and keep complex Trino values as list columns.
  data <- lapply(seq_along(res@state$columns), function(i) {
    values <- lapply(rows, function(row) row[[i]] %||% NA)
    type <- res@state$columns[[i]]$type
    if (grepl("^(array|map|row)", type)) return(I(values))
    if (type %in% c("bigint") || startsWith(type, "decimal")) {
      return(vapply(values, function(value) if (is.na(value)) NA_character_ else as.character(value), ""))
    }
    unlist(values, recursive = FALSE)
  })
  names(data) <- vapply(res@state$columns, `[[`, "", "name")
  as.data.frame(data, optional = TRUE, stringsAsFactors = FALSE)
})
methods::setMethod("dbClearResult", "FDPResult", function(res, ...) {
  if (res@state$open && !is.null(res@state$next_url)) {
    .sql_http(res@state$con, "DELETE", res@state$next_url)
  }
  res@state$open <- FALSE
  TRUE
})
methods::setMethod("dbHasCompleted", "FDPResult", function(res, ...) {
  is.null(res@state$next_url) && !length(res@state$rows)
})
methods::setMethod("dbIsValid", "FDPResult", function(dbObj, ...) dbObj@state$open)
methods::setMethod("dbGetRowCount", "FDPResult", function(res, ...) res@state$fetched)
methods::setMethod("dbGetRowsAffected", "FDPResult", function(res, ...) res@state$affected)
methods::setMethod("dbColumnInfo", "FDPResult", function(res, ...) {
  data.frame(name = vapply(res@state$columns, `[[`, "", "name"),
             type = vapply(res@state$columns, `[[`, "", "type"))
})
methods::setMethod("dbGetQuery", c("FDPConnection", "character"), function(conn, statement, ..., params = NULL) {
  result <- DBI::dbSendQuery(conn, statement, params = params)
  on.exit(DBI::dbClearResult(result))
  DBI::dbFetch(result, ...)
})
methods::setMethod("dbExecute", c("FDPConnection", "character"), function(conn, statement, ..., params = NULL) {
  result <- DBI::dbSendStatement(conn, statement, params = params)
  on.exit(DBI::dbClearResult(result))
  DBI::dbFetch(result)
  DBI::dbGetRowsAffected(result)
})
methods::setMethod("dbQuoteIdentifier", c("FDPConnection", "character"), function(conn, x, ...) {
  DBI::SQL(paste0('"', gsub('"', '""', x, fixed = TRUE), '"'))
})
methods::setMethod("dbQuoteString", c("FDPConnection", "character"), function(conn, x, ...) {
  DBI::SQL(ifelse(is.na(x), "NULL", paste0("'", gsub("'", "''", x, fixed = TRUE), "'")))
})

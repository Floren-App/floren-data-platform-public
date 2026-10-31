# The platform's central log, from a run or app.
#
#   fdp::log_info("loaded rows", table = "sales", rows = 120)
#   fdp::log_warning("3 rows without a region")
#   fdp::log_error("upload failed", attempt = 2)
#
# Each line also prints to the console. The platform stamps the run, workload, code version (commit)
# and layer. Sending never stops your program; outside Floren Data Platform lines only print.

.log <- function(level, message, ...) {
  fields <- list(...)
  extra <- if (length(fields)) paste0(" ", paste(names(fields), unlist(lapply(fields, format)), sep = "=", collapse = " ")) else ""
  message(sprintf("[%s] %s%s", toupper(level), message, extra))
  if (Sys.getenv("FDP_API") == "" || Sys.getenv("FDP_SESSION_TOKEN") == "") return(invisible(FALSE))
  entry <- list(level = level, message = substr(as.character(message), 1L, 4000L),
                at = format(Sys.time(), "%Y-%m-%dT%H:%M:%OS3Z", tz = "UTC"))
  if (length(fields)) entry$fields <- lapply(fields, function(v) if (is.atomic(v) && length(v) == 1L) v else format(v))
  ok <- tryCatch({
    .request("v1/logs") |>
      httr2::req_body_json(list(entries = list(entry)), auto_unbox = TRUE) |>
      httr2::req_timeout(5) |>
      httr2::req_perform()
    TRUE
  }, error = function(e) FALSE)
  invisible(ok)
}

#' Write to the central log (also printed to the console).
log_debug <- function(message, ...) .log("debug", message, ...)
log_info <- function(message, ...) .log("info", message, ...)
log_warning <- function(message, ...) .log("warning", message, ...)
log_error <- function(message, ...) .log("error", message, ...)

# Floren Data Platform runner for RStudio's Viewer pane: pick a fdp.json program, fill in its
# arguments, choose a warehouse, run, and follow the console. Started by fdp::runner().
library(shiny)

dc <- asNamespace("fdp")
ACTIVE <- c("pending", "running")

css <- "
body { font: 13px/1.4 -apple-system, 'Segoe UI', Ubuntu, sans-serif; margin: 0; padding: 0 10px 16px; }
h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; margin: 16px 0 6px; color: #555; }
.hdr { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-weight: 600; }
.muted { color: #6e6e6e; } .err { color: #c62828; white-space: pre-wrap; margin: 6px 0; }
.form-group { margin-bottom: 8px; } .shiny-input-container { width: 100% !important; }
.btn-run { width: 100%; margin-top: 6px; }
.runs { width: 100%; border-collapse: collapse; } .runs td { padding: 4px 2px; border-bottom: 1px solid #eee; vertical-align: top; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #999; margin-right: 4px; }
.dot.running, .dot.pending { background: #cca700; } .dot.succeeded { background: #2ea043; } .dot.failed { background: #f85149; }
.runs a { margin-right: 8px; cursor: pointer; }
#console { background: #1e1e1e; color: #ddd; font: 12px/1.35 Menlo, Consolas, monospace; max-height: 340px;
           overflow: auto; white-space: pre-wrap; border-radius: 4px; padding: 8px; }
@media (prefers-color-scheme: dark) { body { background: #222; color: #ddd; } h4 { color: #aaa; } .runs td { border-color: #333; } }
"

# Keep the console scrolled to the newest line.
scroll_js <- "$(document).on('shiny:value', function(e) { if (e.name === 'console') {
  setTimeout(function() { var el = document.getElementById('console'); el.scrollTop = el.scrollHeight; }, 0); } });"

safely <- function(expr, default = NULL) tryCatch(expr, error = function(e) default)

ui <- fluidPage(
  tags$head(tags$style(HTML(css)), tags$script(HTML(scroll_js))),
  div(class = "hdr", "Floren Data Platform runner", actionLink("reload", "reload")),
  uiOutput("picker"),
  uiOutput("form"),
  uiOutput("error"),
  h4("Runs"),
  uiOutput("runs"),
  h4(textOutput("console_title", inline = TRUE)),
  verbatimTextOutput("console"),
  uiOutput("outputs")
)

server <- function(input, output, session) {
  cfg <- reactiveVal(NULL)
  pools <- reactiveVal(list())
  srcs <- reactiveVal(data.frame(name = character()))
  err <- reactiveVal(NULL)
  current <- reactiveVal(NULL)

  load <- function() {
    cfg(safely(dc$programs(), list(exists = FALSE, errors = list("cannot reach Floren Data Platform"))))
    pools(safely(dc$warehouses(), list()))
    srcs(safely(dc$sources(), data.frame(name = character())))
  }
  observeEvent(input$reload, load(), ignoreNULL = FALSE)
  observeEvent(input$init, { dc$init(); load() })

  programs <- reactive({
    c <- cfg()
    # A broken file only hides its own programs; the others stay runnable.
    if (is.null(c) || !isTRUE(c$exists) || is.null(c$config)) list() else c$config$programs
  })

  output$picker <- renderUI({
    c <- cfg()
    req(c)
    if (!isTRUE(c$exists)) {
      return(tagList(p(class = "muted", "No fdp.json in this workspace yet."),
                     actionButton("init", "Create fdp.json")))
    }
    problems <- if (length(c$errors)) div(class = "err", paste0("fdp.json has problems:\n• ", paste(unlist(c$errors), collapse = "\n• ")))
    if (!length(programs())) return(tagList(problems, if (is.null(problems)) p(class = "muted", "No programs defined yet.")))
    tagList(problems, selectInput("program", "Program", names(programs()), selected = isolate(input$program)))
  })

  arg_input <- function(a) {
    id <- paste0("arg_", a$name)
    label <- paste0(a$label %||% a$name, if (isTRUE(a$required)) " *" else "")
    d <- a$default
    el <- switch(a$type,
      boolean = checkboxInput(id, label, value = isTRUE(d)),
      choice = selectInput(id, label, c(if (!isTRUE(a$required)) "", unlist(a$choices)), selected = d %||% ""),
      data_source = selectInput(id, label, c(if (!isTRUE(a$required)) "", srcs()$name), selected = d %||% ""),
      integer = numericInput(id, label, value = d %||% NA, min = a$min %||% NA, max = a$max %||% NA, step = 1),
      number = numericInput(id, label, value = d %||% NA, min = a$min %||% NA, max = a$max %||% NA),
      date = dateInput(id, label, value = d %||% NA),
      textInput(id, label, value = d %||% ""))
    tagList(el, if (!is.null(a$help)) div(class = "muted", style = "margin:-6px 0 8px", a$help))
  }

  output$form <- renderUI({
    req(input$program, programs()[[input$program]])
    prog <- programs()[[input$program]]
    default_pool <- prog$warehouse %||% cfg()$config$warehouse %||% ""
    choices <- vapply(pools(), function(p) p$name, "")
    names(choices) <- vapply(pools(), function(p) sprintf("%s · %g vCPU · %g GB%s", p$name, p$cpu, p$memory_gb,
                                                          if (p$name == default_pool) " (default)" else ""), "")
    tagList(
      if (nzchar(prog$description %||% "")) p(class = "muted", prog$description),
      div(class = "muted", style = "font-size:11px;margin-bottom:8px",
          sprintf("%s · %s · %s%s", prog$kind, prog$runtime, prog$entrypoint,
                  if (!is.null(prog$requirements)) paste0(" · ", prog$requirements) else "")),
      lapply(prog$args, arg_input),
      selectInput("pool", "Warehouse", choices, selected = default_pool),
      actionButton("run", paste("▶ Run", input$program), class = "btn-primary btn-run")
    )
  })

  collect <- function(prog) {
    out <- list()
    for (a in prog$args) {
      v <- input[[paste0("arg_", a$name)]]
      if (is.null(v) || length(v) == 0 || (length(v) == 1 && (is.na(v) || identical(v, "")))) next
      if (inherits(v, "Date")) v <- format(v, "%Y-%m-%d")
      out[[a$name]] <- v
    }
    out
  }

  observeEvent(input$run, {
    prog <- programs()[[input$program]]
    tryCatch({
      r <- dc$run_program(input$program, collect(prog), warehouse = input$pool, follow = FALSE)
      current(r$id)
      err(NULL)
    }, error = function(e) err(conditionMessage(e)))
  })

  output$error <- renderUI(if (!is.null(err())) div(class = "err", err()))

  runs <- reactive({
    invalidateLater(2500)
    safely(dc$workspace_runs(), list())
  })

  output$runs <- renderUI({
    rs <- head(runs(), 12)
    if (!length(rs)) return(p(class = "muted", "No runs yet."))
    tags$table(class = "runs", lapply(rs, function(r) {
      live <- r$status %in% ACTIVE
      tags$tr(
        tags$td(span(class = paste("dot", r$status), title = r$status), r$program %||% r$entrypoint,
                div(class = "muted", sprintf("%s · %s", r$pool_name, substr(r$created_at, 12, 16)))),
        tags$td(style = "text-align:right;white-space:nowrap",
          tags$a(onclick = sprintf("Shiny.setInputValue('show_run', '%s', {priority: 'event'})", r$id), "console"),
          if (live) tags$a(onclick = sprintf("Shiny.setInputValue('stop_run', '%s', {priority: 'event'})", r$id),
                           "■ stop")))
    }))
  })

  observeEvent(input$show_run, current(input$show_run))
  observeEvent(input$stop_run, safely(dc$stop_run(input$stop_run)))

  selected <- reactive({
    id <- current()
    if (is.null(id)) return(NULL)
    Find(function(r) identical(r$id, id), runs())
  })

  output$console_title <- renderText({
    r <- selected()
    if (is.null(r)) "Console" else sprintf("Console · %s on %s · %s", r$program %||% r$entrypoint, r$pool_name, r$status)
  })

  output$console <- renderText({
    r <- selected()
    if (is.null(r)) return("Run a program to see its console here.")
    if (r$status %in% ACTIVE) invalidateLater(1500)
    safely(dc$run_logs(r$id), "")
  })

  output$outputs <- renderUI({
    r <- selected()
    req(r, !(r$status %in% ACTIVE))
    files <- safely(dc$run_outputs(r$id), list())
    if (!length(files)) return(NULL)
    tagList(h4("Outputs"), p(class = "muted", sprintf("Files pane: .fdp/runs/%s/outputs", r$id)),
            tags$ul(lapply(files, function(f) tags$li(f$path, span(class = "muted", sprintf(" (%s bytes)", f$size))))))
  })
}

`%||%` <- function(a, b) if (is.null(a)) b else a

shinyApp(ui, server)

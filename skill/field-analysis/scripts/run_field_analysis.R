#!/usr/bin/env Rscript

parse_args <- function(args) {
  out <- list()
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) stop(sprintf("Unexpected argument: %s", key))
    name <- sub("^--", "", key)
    if (i == length(args) || startsWith(args[[i + 1]], "--")) {
      out[[name]] <- TRUE
      i <- i + 1
    } else {
      out[[name]] <- args[[i + 1]]
      i <- i + 2
    }
  }
  out
}

script_dir <- function() {
  file_arg <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(file_arg) > 0) return(dirname(normalizePath(sub("^--file=", "", file_arg[[1]]), mustWork = TRUE)))
  getwd()
}

source_here <- function(name) {
  source(file.path(script_dir(), name), local = .GlobalEnv, encoding = "UTF-8")
}

source_here("utils.R")
source_here("io.R")
source_here("trait_metadata.R")
source_here("summaries.R")
source_here("models.R")
source_here("report_builder.R")

args <- parse_args(commandArgs(trailingOnly = TRUE))
if (is.null(args$input)) stop("--input is required")
if (is.null(args$design)) stop("--design is required")

design <- tolower(args$design)
if (!design %in% c("rcbd", "diagonal")) stop("--design must be one of: rcbd, diagonal")
profile <- if (is.null(args$profile)) "full_report" else args$profile
run_id <- if (is.null(args[["run-id"]])) format(Sys.time(), "%Y%m%d%H%M%S") else gsub("[^A-Za-z0-9_-]+", "-", args[["run-id"]])

root <- normalizePath(file.path(script_dir(), ".."), mustWork = TRUE)
input_path <- args$input
if (!file.exists(input_path)) {
  candidate <- file.path(root, input_path)
  if (file.exists(candidate)) input_path <- candidate
}
input_path <- normalizePath(input_path, mustWork = TRUE)

output_dir <- if (is.null(args[["output-dir"]])) file.path(root, "outputs") else args[["output-dir"]]
if (!grepl("^[A-Za-z]:[/\\\\]", output_dir) && !startsWith(output_dir, "/")) {
  output_dir <- file.path(getwd(), output_dir)
}
dir.create(output_dir, recursive = TRUE, showWarnings = FALSE)
output_dir <- normalizePath(output_dir, mustWork = TRUE)

raw <- read_analysis_input(input_path)
df <- normalize_input(raw)
report <- build_report(df, input_path = input_path, design = design, run_id = run_id, profile = profile)

result_file <- file.path(output_dir, sprintf("field-analysis-%s-full-report-%s.json", design, run_id))
summary_file <- file.path(output_dir, sprintf("field-analysis-summary-%s-full-report-%s.json", design, run_id))
session_file <- file.path(output_dir, ".field-analysis-session.json")
write_report_json(report, result_file)

summary <- list(
  ok = TRUE,
  design = design,
  analysis_profile = profile,
  run_id = run_id,
  input = input_path,
  output_dir = output_dir,
  output_json = result_file,
  summary_json = summary_file,
  session_json = session_file,
  format = "field-analysis-report-v1",
  chapters = names(report$chapters)
)
writeLines(jsonlite::toJSON(summary, pretty = TRUE, auto_unbox = TRUE, null = "null"), summary_file)

session <- list(
  active_report = result_file,
  active_summary = summary_file,
  format = "field-analysis-report-v1",
  design = design,
  analysis_profile = profile,
  run_id = run_id,
  input = input_path,
  output_dir = output_dir,
  updated_at = format(Sys.time(), "%Y-%m-%d %H:%M:%S %z"),
  available_traits = sort(unique(as.character(df$trait))),
  available_chapters = names(report$chapters)
)
writeLines(jsonlite::toJSON(session, pretty = TRUE, auto_unbox = TRUE, null = "null"), session_file)

cat("Field analysis report completed\n")
cat(sprintf("Design: %s\n", design))
cat(sprintf("Run ID: %s\n", run_id))
cat(sprintf("Output JSON: %s\n", result_file))
cat(sprintf("Summary JSON: %s\n", summary_file))
cat(sprintf("Session JSON: %s\n", session_file))

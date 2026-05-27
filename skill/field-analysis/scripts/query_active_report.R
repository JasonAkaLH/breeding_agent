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

records_to_df <- function(fields, records) {
  if (is.null(fields) || is.null(records) || length(records) == 0) {
    return(data.frame(stringsAsFactors = FALSE))
  }
  rows <- lapply(records, function(row) {
    row <- as.list(row)
    if (length(row) < length(fields)) row <- c(row, rep(list(NA), length(fields) - length(row)))
    if (length(row) > length(fields)) row <- row[seq_along(fields)]
    row <- lapply(row, function(value) {
      if (is.null(value)) return(NA)
      value
    })
    names(row) <- fields
    as.data.frame(row, stringsAsFactors = FALSE, check.names = FALSE)
  })
  out <- do.call(rbind, rows)
  row.names(out) <- NULL
  out
}

filter_record_df <- function(df, ped_id = NULL, loc_id = NULL, top_n = NULL) {
  if (!is.null(ped_id) && "ped_id" %in% names(df)) {
    df <- df[as.character(df$ped_id) == as.character(ped_id), , drop = FALSE]
  }
  if (!is.null(loc_id) && "loc_id" %in% names(df)) {
    df <- df[as.character(df$loc_id) == as.character(loc_id), , drop = FALSE]
  }
  if (!is.null(top_n) && nrow(df) > top_n) {
    df <- df[seq_len(top_n), , drop = FALSE]
  }
  df
}

df_to_records <- function(df) {
  if (is.null(df) || nrow(df) == 0) return(list(fields = character(0), records = list()))
  list(
    fields = names(df),
    records = lapply(seq_len(nrow(df)), function(i) unname(as.list(df[i, , drop = FALSE])))
  )
}

trait_summary_df <- function(report) {
  records_to_df(report$traits$trait_summary_fields, report$traits$trait_summary)
}

by_trait_df <- function(node, fields_name, by_trait_name, trait) {
  fields <- node[[fields_name]]
  by_trait <- node[[by_trait_name]]
  if (is.null(trait)) {
    return(NULL)
  }
  records_to_df(fields, by_trait[[trait]])
}

analysis_for_trait <- function(section, trait) {
  if (is.null(section) || is.null(trait) || is.null(section$by_trait)) return(NULL)
  section$by_trait[[trait]]
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Package 'jsonlite' is required.")

root <- normalizePath(file.path(script_dir(), ".."), mustWork = TRUE)
output_dir <- if (is.null(args[["output-dir"]])) file.path(root, "outputs") else args[["output-dir"]]
if (!grepl("^[A-Za-z]:[/\\\\]", output_dir) && !startsWith(output_dir, "/")) {
  output_dir <- file.path(getwd(), output_dir)
}
output_dir <- normalizePath(output_dir, mustWork = TRUE)

session_path <- if (is.null(args$session)) file.path(output_dir, ".field-analysis-session.json") else args$session
if (!file.exists(session_path) && is.null(args$report)) {
  stop("No active field-analysis session found. Run field analysis first or provide --report.")
}
if (file.exists(session_path)) session_path <- normalizePath(session_path, mustWork = TRUE)

session <- NULL
report_path <- args$report
if (is.null(report_path)) {
  session <- jsonlite::fromJSON(session_path, simplifyVector = FALSE)
  report_path <- session$active_report
}
if (!file.exists(report_path)) stop(sprintf("Report file not found: %s", report_path))
report_path <- normalizePath(report_path, mustWork = TRUE)

report <- jsonlite::fromJSON(report_path, simplifyVector = FALSE)
if (!identical(report$format, "field-analysis-report-v1")) {
  stop("Report is not field-analysis-report-v1.")
}

trait <- args$trait
ped_id <- args[["ped-id"]]
loc_id <- args[["loc-id"]]
top_n <- if (is.null(args[["top-n"]])) 10 else suppressWarnings(as.integer(args[["top-n"]]))
sections <- if (is.null(args$section)) c("overview") else strsplit(args$section, ",", fixed = TRUE)[[1]]
sections <- trimws(sections)

query <- list(
  ok = TRUE,
  report = report_path,
  session = session_path,
  requested = list(
    trait = trait,
    ped_id = ped_id,
    loc_id = loc_id,
    sections = sections,
    top_n = top_n
  ),
  metadata = report$metadata,
  chapters = report$chapters
)

if (!is.null(trait)) {
  ts <- trait_summary_df(report)
  ts <- ts[as.character(ts$trait) == as.character(trait), , drop = FALSE]
  query$trait_summary <- df_to_records(ts)

  material_df <- by_trait_df(report$materials, "material_summary_fields", "by_trait", trait)
  material_df <- filter_record_df(material_df, ped_id = ped_id, top_n = top_n)
  query$materials <- df_to_records(material_df)

  loc_df <- by_trait_df(report$locations, "location_summary_fields", "summary_by_trait", trait)
  loc_df <- filter_record_df(loc_df, loc_id = loc_id)
  query$locations <- df_to_records(loc_df)

  ml_df <- by_trait_df(report$locations, "material_location_fields", "materials_by_trait", trait)
  ml_df <- filter_record_df(ml_df, ped_id = ped_id, loc_id = loc_id, top_n = top_n)
  query$material_locations <- df_to_records(ml_df)

  analyses <- list()
  if ("anova" %in% sections || "all" %in% sections) analyses$anova <- analysis_for_trait(report$analyses$anova, trait)
  if ("lsd" %in% sections || "all" %in% sections) analyses$lsd_grouping <- analysis_for_trait(report$analyses$lsd_grouping, trait)
  if ("spatial" %in% sections || "all" %in% sections) analyses$spatial_adjustment <- analysis_for_trait(report$analyses$spatial_adjustment, trait)
  if ("stability" %in% sections || "all" %in% sections) {
    if (!is.null(report$analyses$stability$by_trait)) {
      records <- report$analyses$stability$by_trait[[trait]]
      analyses$stability <- list(
        status = report$analyses$stability$status,
        stability_fields = report$analyses$stability$stability_fields,
        records = records
      )
    } else {
      analyses$stability <- report$analyses$stability
    }
  }
  if (length(analyses) > 0) query$analyses <- analyses
}

cat(jsonlite::toJSON(query, pretty = TRUE, auto_unbox = TRUE, null = "null", na = "null"))

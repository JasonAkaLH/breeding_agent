suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for fieldbook CSV export.")
  }
})

`%||%` <- function(a, b) if (is.null(a)) b else a

args <- commandArgs(trailingOnly = TRUE)

parse_args <- function(args) {
  out <- list()
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) stop(sprintf("Unexpected argument: %s", key), call. = FALSE)
    name <- sub("^--", "", key)
    if (i == length(args) || startsWith(args[[i + 1]], "--")) {
      out[[name]] <- "true"
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
  if (length(file_arg) > 0) {
    return(normalizePath(dirname(sub("^--file=", "", file_arg[[1]])), winslash = "/", mustWork = FALSE))
  }
  normalizePath(".", winslash = "/", mustWork = FALSE)
}

skill_dir <- function() {
  normalizePath(file.path(script_dir(), ".."), winslash = "/", mustWork = FALSE)
}

is_absolute_path <- function(path) {
  grepl("^([A-Za-z]:[/\\\\]|/|\\\\\\\\)", path)
}

resolve_input_path <- function(path, root_dir) {
  if (is_absolute_path(path) || file.exists(path)) {
    return(normalizePath(path, winslash = "/", mustWork = TRUE))
  }
  normalizePath(file.path(root_dir, path), winslash = "/", mustWork = TRUE)
}

resolve_output_path <- function(path, root_dir) {
  if (is_absolute_path(path)) return(normalizePath(path, winslash = "/", mustWork = FALSE))
  normalizePath(path, winslash = "/", mustWork = FALSE)
}

as_fieldbook_df <- function(x) {
  if (is.data.frame(x)) return(as.data.frame(x, stringsAsFactors = FALSE))
  if (is.list(x) && length(x) > 0 && is.list(x[[1]])) {
    rows <- lapply(x, function(row) as.data.frame(row, stringsAsFactors = FALSE, check.names = FALSE))
    out <- do.call(rbind, rows)
    row.names(out) <- NULL
    return(out)
  }
  as.data.frame(x, stringsAsFactors = FALSE, check.names = FALSE)
}

extract_fieldbook <- function(payload, design) {
  if (!is.null(payload$out_design)) {
    out <- as_fieldbook_df(payload$out_design)
    out$site <- NULL
    return(out)
  }
  if (identical(design, "rcbd") && !is.null(payload$results) && length(payload$results) > 0) {
    rows <- list()
    for (i in seq_along(payload$results)) {
      item <- payload$results[[i]]
      df <- as_fieldbook_df(item$out_design)
      df$site <- item$label %||% paste0("site", i)
      rows[[i]] <- df
    }
    out <- do.call(rbind, rows)
    row.names(out) <- NULL
    return(out)
  }
  stop("Input JSON does not contain a supported out_design field.", call. = FALSE)
}

opts <- parse_args(args)
input <- opts[["input"]]
output <- opts[["output"]]
design <- tolower(opts[["design"]] %||% "diagonal")

if (is.null(input)) stop("Missing required argument --input", call. = FALSE)
if (is.null(output)) stop("Missing required argument --output", call. = FALSE)
if (!design %in% c("rcbd", "diagonal", "interval")) stop("--design must be rcbd, diagonal, or interval.", call. = FALSE)

root_dir <- skill_dir()
input_path <- resolve_input_path(input, root_dir)
output_path <- resolve_output_path(output, root_dir)

payload <- jsonlite::fromJSON(input_path, simplifyVector = FALSE)
if (isFALSE(payload$ok)) stop("Cannot export CSV for a failed design result.", call. = FALSE)

fieldbook <- extract_fieldbook(payload, design)
if (identical(design, "diagonal")) {
  columns <- c("plots", "ped_id", "hyb_type", "ranges", "pass", "set", "design_check")
} else if (identical(design, "rcbd")) {
  if (!"ped_id" %in% names(fieldbook) && "trt" %in% names(fieldbook)) {
    names(fieldbook)[names(fieldbook) == "trt"] <- "ped_id"
  }
  if (!"hyb_check" %in% names(fieldbook) && "design_check" %in% names(fieldbook)) {
    names(fieldbook)[names(fieldbook) == "design_check"] <- "hyb_check"
  }
  columns <- c("plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type", "site")
  columns <- columns[columns %in% names(fieldbook)]
} else {
  columns <- c("plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type")
}

missing_required <- setdiff(columns[columns != "site"], names(fieldbook))
if (length(missing_required) > 0) {
  stop(sprintf("out_design is missing required columns: %s", paste(missing_required, collapse = ", ")), call. = FALSE)
}

fieldbook <- fieldbook[, columns, drop = FALSE]
dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
write.csv(fieldbook, output_path, row.names = FALSE, fileEncoding = "UTF-8")

cat(sprintf("CSV exported: %s\n", output_path))

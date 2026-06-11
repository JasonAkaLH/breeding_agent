suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for the interval runner.")
  }
})

args <- commandArgs(trailingOnly = TRUE)

`%||%` <- function(a, b) if (is.null(a)) b else a

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

as_bool <- function(x, default = FALSE) {
  if (is.null(x)) return(default)
  lx <- tolower(as.character(x))
  if (lx %in% c("true", "t", "1", "yes", "y")) return(TRUE)
  if (lx %in% c("false", "f", "0", "no", "n")) return(FALSE)
  stop(sprintf("Invalid boolean value: %s", x), call. = FALSE)
}

as_int <- function(x, name, default = NULL) {
  if (is.null(x)) {
    if (is.null(default)) stop(sprintf("Missing required argument --%s", name), call. = FALSE)
    return(default)
  }
  val <- suppressWarnings(as.integer(x))
  if (is.na(val) || val < 1L) stop(sprintf("--%s must be a positive integer", name), call. = FALSE)
  val
}

script_dir <- function() {
  file_arg <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(file_arg) > 0) {
    return(normalizePath(dirname(sub("^--file=", "", file_arg[[1]])), winslash = "/", mustWork = FALSE))
  }
  normalizePath(dirname(sys.frame(1)$ofile %||% "."), winslash = "/", mustWork = FALSE)
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
  if (is.null(path)) return(NULL)
  if (is_absolute_path(path)) return(normalizePath(path, winslash = "/", mustWork = FALSE))
  normalizePath(path, winslash = "/", mustWork = FALSE)
}

make_error <- function(type, message, details = list()) {
  list(ok = FALSE, error = list(type = type, message = message, details = details))
}

read_materials <- function(input_path) {
  ext <- tolower(tools::file_ext(input_path))
  data <- if (ext == "csv") {
    read.csv(input_path, stringsAsFactors = FALSE, check.names = FALSE, fileEncoding = "UTF-8")
  } else if (ext == "json") {
    as.data.frame(jsonlite::fromJSON(input_path), stringsAsFactors = FALSE)
  } else {
    stop("Input must be a CSV or JSON file.", call. = FALSE)
  }

  required <- c("ped_id", "hyb_check", "set")
  missing_required <- setdiff(required, names(data))
  if (length(missing_required) > 0) {
    stop(sprintf("Input data is missing required columns: %s", paste(missing_required, collapse = ", ")), call. = FALSE)
  }

  data <- data[, required, drop = FALSE]
  data$ped_id <- enc2utf8(trimws(as.character(data$ped_id)))
  data$hyb_check <- enc2utf8(trimws(as.character(data$hyb_check)))
  data$set <- enc2utf8(trimws(as.character(data$set)))

  if (any(is.na(data$ped_id) | data$ped_id == "")) stop("Input column 'ped_id' contains missing or empty values.", call. = FALSE)
  if (any(is.na(data$hyb_check) | data$hyb_check == "")) stop("Input column 'hyb_check' contains missing or empty values.", call. = FALSE)
  if (any(is.na(data$set) | data$set == "")) stop("Input column 'set' contains missing or empty values.", call. = FALSE)
  if (!any(data$hyb_check != "0")) stop("Interval design requires at least one CK row (hyb_check != 0).", call. = FALSE)
  if (!any(data$hyb_check == "0")) stop("Interval design requires at least one test row (hyb_check = 0).", call. = FALSE)

  ck_rows <- data[data$hyb_check != "0", c("ped_id", "set"), drop = FALSE]
  ck_keys <- paste(ck_rows$set, ck_rows$ped_id, sep = "\r")
  duplicated_ck <- unique(ck_rows[duplicated(ck_keys), , drop = FALSE])
  if (nrow(duplicated_ck) > 0L) {
    duplicated_labels <- paste0(duplicated_ck$set, ":", duplicated_ck$ped_id)
    stop(
      sprintf("CK ped_id must be unique within each set for interval design. Duplicated CK(s): %s", paste(duplicated_labels, collapse = ", ")),
      call. = FALSE
    )
  }

  data
}

make_ck_table <- function(data) {
  cks <- data[data$hyb_check != "0", c("ped_id", "set"), drop = FALSE]
  cks$ck_no <- seq_len(nrow(cks))
  cks <- cks[, c("ck_no", "ped_id", "set"), drop = FALSE]
  row.names(cks) <- NULL
  cks
}

parse_ck_spec <- function(spec, ck_table) {
  if (is.null(spec) || trimws(spec) == "") {
    stop("Missing required --ck-spec. Use format like: 1,1,10; 2,5,10", call. = FALSE)
  }

  chunks <- strsplit(spec, ";", fixed = TRUE)[[1]]
  chunks <- trimws(chunks[nzchar(trimws(chunks))])
  if (length(chunks) == 0L) stop("--ck-spec does not contain any CK parameters.", call. = FALSE)

  rows <- lapply(chunks, function(chunk) {
    parts <- trimws(strsplit(chunk, ",", fixed = TRUE)[[1]])
    if (length(parts) != 3L) {
      stop(sprintf("Invalid CK spec item '%s'. Expected ck_no,start_pos,interval.", chunk), call. = FALSE)
    }
    values <- suppressWarnings(as.integer(parts))
    if (any(is.na(values)) || any(values < 1L)) {
      stop(sprintf("Invalid CK spec item '%s'. Values must be positive integers.", chunk), call. = FALSE)
    }
    data.frame(ck_no = values[[1]], start_pos = values[[2]], interval = values[[3]])
  })

  parsed <- do.call(rbind, rows)
  duplicate_no <- unique(parsed$ck_no[duplicated(parsed$ck_no)])
  if (length(duplicate_no) > 0L) {
    stop(sprintf("Duplicate ck_no in --ck-spec: %s", paste(duplicate_no, collapse = ", ")), call. = FALSE)
  }

  unknown_no <- setdiff(parsed$ck_no, ck_table$ck_no)
  if (length(unknown_no) > 0L) {
    stop(sprintf("Unknown ck_no in --ck-spec: %s", paste(unknown_no, collapse = ", ")), call. = FALSE)
  }

  missing_no <- setdiff(ck_table$ck_no, parsed$ck_no)
  if (length(missing_no) > 0L) {
    stop(sprintf("Missing interval parameters for ck_no: %s", paste(missing_no, collapse = ", ")), call. = FALSE)
  }

  merged <- merge(ck_table, parsed, by = "ck_no", sort = FALSE)
  merged <- merged[order(merged$ck_no), , drop = FALSE]
  row.names(merged) <- NULL
  merged
}

main <- function() {
  opts <- parse_args(args)
  runner_dir <- script_dir()
  core_path <- file.path(runner_dir, "interval_contrast_design_core.R")
  if (!file.exists(core_path)) stop(sprintf("Missing bundled interval core script: %s", core_path), call. = FALSE)
  source(core_path)

  input <- opts[["input"]]
  if (is.null(input)) stop("Missing required argument --input", call. = FALSE)
  root_dir <- skill_dir()
  input_path <- resolve_input_path(input, root_dir)
  output_path <- resolve_output_path(opts[["output"]], root_dir)

  data <- read_materials(input_path)
  ck_table <- make_ck_table(data)
  list_checks <- as_bool(opts[["list-checks"]], default = FALSE)

  if (isTRUE(list_checks)) {
    payload <- list(
      ok = TRUE,
      design = "interval",
      mode = "ck_table",
      ck_table = ck_table,
      instructions = "Ask the user to provide CK parameters as: ck_no,start_pos,interval; separate multiple CKs with semicolons."
    )
  } else {
    ncols <- as_int(opts[["ncols"]], "ncols")
    nrows <- if (is.null(opts[["nrows"]])) NULL else as_int(opts[["nrows"]], "nrows")
    planter <- opts[["planter"]] %||% "serpentine"
    if (!planter %in% c("serpentine", "cartesian")) stop("--planter must be serpentine or cartesian.", call. = FALSE)
    randomize <- as_bool(opts[["randomize"]], default = TRUE)
    seed <- if (is.null(opts[["seed"]])) sample.int(900000L, 1L) + 10000L else as_int(opts[["seed"]], "seed")

    ck_spec_table <- parse_ck_spec(opts[["ck-spec"]], ck_table)
    ck_params <- ck_spec_table[, c("ped_id", "set", "start_pos", "interval"), drop = FALSE]
    payload <- run_interval_contrast(
      data = data,
      ck_params = ck_params,
      ncols = ncols,
      nrows = nrows,
      planter = planter,
      randomize = randomize,
      seed = seed
    )
    payload$ck_table <- ck_table
    payload$ck_params_confirmation <- ck_spec_table[, c("ck_no", "ped_id", "set", "start_pos", "interval"), drop = FALSE]
  }

  json <- jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, null = "null", na = "null")
  if (!is.null(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    writeLines(json, output_path, useBytes = TRUE)
  } else {
    cat(json)
  }
}

tryCatch(
  main(),
  error = function(e) {
    payload <- make_error("interval_error", conditionMessage(e))
    json <- jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, null = "null")
    args_parsed <- tryCatch(parse_args(args), error = function(...) list())
    output_path <- tryCatch(resolve_output_path(args_parsed[["output"]], skill_dir()), error = function(...) args_parsed[["output"]])
    if (!is.null(output_path)) {
      dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
      writeLines(json, output_path, useBytes = TRUE)
    } else {
      cat(json)
    }
    quit(status = 1)
  }
)

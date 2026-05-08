suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for the mini RCBD runner.")
  }
})

args <- commandArgs(trailingOnly = TRUE)

parse_args <- function(args) {
  out <- list()
  i <- 1
  while (i <= length(args)) {
    key <- args[[i]]
    if (!startsWith(key, "--")) {
      stop(sprintf("Unexpected argument: %s", key), call. = FALSE)
    }
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
  if (is.na(val) || val < 1) stop(sprintf("--%s must be a positive integer", name), call. = FALSE)
  val
}

stable_set_order <- function(values) {
  sort(unique(as.character(values)), method = "radix")
}

standardize_rows <- function(df) {
  mapping <- c(plot_id = "plots", block = "r", ped_id = "trt")
  for (src in names(mapping)) {
    dst <- mapping[[src]]
    if (src %in% names(df) && !(dst %in% names(df))) {
      names(df)[names(df) == src] <- dst
    }
  }
  keep <- c("plots", "r", "trt", "ranges", "pass", "set", "set_index", "set_ncols",
            "design_check", "hyb_type")
  df <- df[, intersect(keep, names(df)), drop = FALSE]
  row.names(df) <- NULL
  df
}

make_error <- function(type, message, details = list()) {
  list(ok = FALSE, error = list(type = type, message = message, details = details))
}

script_dir <- function() {
  file_arg <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (length(file_arg) > 0) {
    return(normalizePath(dirname(sub("^--file=", "", file_arg[[1]])), winslash = "/", mustWork = FALSE))
  }
  normalizePath(dirname(sys.frame(1)$ofile %||% "."), winslash = "/", mustWork = FALSE)
}

run_one_design <- function(data, blocks, planter, seed, check_constraint, test_constraint, core_path) {
  if (!planter %in% c("serpentine", "cartesian")) {
    stop("planter must be 'serpentine' or 'cartesian'", call. = FALSE)
  }

  source(core_path)

  sets <- stable_set_order(data$set)
  all_rows <- list()
  sets_payload <- list()
  plot_output_offset <- 0
  current_row_start <- 1L

  for (set_index in seq_along(sets)) {
    set_label <- sets[[set_index]]
    set_data <- data[data$set == set_label, , drop = FALSE]
    set_data$exp_group <- set_index
    set_ncols <- nrow(set_data)
    set_seed <- seed + set_index * 1000L
    set_nrows <- blocks

    res <- rcbd_design_core(
      ped.list = set_data,
      blocks = blocks,
      nrows = set_nrows,
      ncols = set_ncols,
      seed = set_seed,
      planter = "serpentine",
      ck_constrain = check_constraint,
      hyb_constrain = test_constraint,
      plot_id_start = (current_row_start - 1L) * set_ncols + 1L,
      block_start = 1L
    )

    out <- res$out_design
    physical_row <- current_row_start + out$block - 1L
    position_in_row <- out$plot_within_block
    out$ranges <- physical_row
    out$pass <- ifelse(
      planter == "serpentine" & physical_row %% 2L == 0L,
      set_ncols + 1L - position_in_row,
      position_in_row
    )
    out$set <- set_label
    out$set_index <- set_index
    out$set_ncols <- set_ncols
    out <- standardize_rows(out)
    row.names(out) <- NULL
    out$plots <- plot_output_offset + seq_len(nrow(out))
    plot_output_offset <- max(out$plots)
    current_row_start <- current_row_start + blocks
    all_rows[[length(all_rows) + 1L]] <- out

    sets_payload[[set_label]] <- list(
      out_design = out,
      parameters = list(
        set = set_label,
        set_index = set_index,
        blocks = blocks,
        set_ncols = set_ncols,
        seed = set_seed
      )
    )
  }

  combined <- do.call(rbind, all_rows)
  row.names(combined) <- NULL

  list(
    ok = TRUE,
    design = "rcbd",
    out_design = combined,
    sets = sets_payload,
    parameters = list(
      sets = sets,
      blocks = blocks,
      seed = seed,
      planter = planter,
      check_position_constraint = check_constraint,
      test_position_constraint = test_constraint
    ),
    warnings = list(),
    assumptions = list()
  )
}

main <- function() {
  opts <- parse_args(args)
  runner_dir <- script_dir()
  core_path <- file.path(runner_dir, "rcbd_design_core.R")
  if (!file.exists(core_path)) {
    stop(sprintf("Missing bundled RCBD core script: %s", core_path), call. = FALSE)
  }

  input <- opts[["input"]]
  if (is.null(input)) stop("Missing required argument --input", call. = FALSE)
  input_path <- normalizePath(input, winslash = "/", mustWork = TRUE)
  output_path <- opts[["output"]]

  blocks <- as_int(opts[["blocks"]], "blocks")
  planter <- opts[["planter"]]
  if (is.null(planter)) planter <- "serpentine"
  seed <- as_int(opts[["seed"]], "seed", default = sample.int(900000L, 1L) + 10000L)
  site_num <- as_int(opts[["site-num"]], "site-num", default = 1L)
  site_random <- as_bool(opts[["site-random"]], default = FALSE)
  check_constraint <- as_bool(opts[["check-position-constraint"]], default = TRUE)
  test_constraint <- as_bool(opts[["test-position-constraint"]], default = TRUE)
  single_set_if_missing <- as_bool(opts[["single-set-if-missing"]], default = TRUE)

  ext <- tolower(tools::file_ext(input_path))
  data <- if (ext == "csv") {
    read.csv(input_path, stringsAsFactors = FALSE, check.names = FALSE)
  } else {
    as.data.frame(jsonlite::fromJSON(input_path), stringsAsFactors = FALSE)
  }

  if (!("ped_id" %in% names(data)) && "plot_id" %in% names(data)) {
    data$ped_id <- data$plot_id
  }
  if (!("design_check" %in% names(data)) && "hyb_check" %in% names(data)) {
    data$design_check <- data$hyb_check
  }

  required <- c("ped_id", "design_check")
  missing_required <- setdiff(required, names(data))
  if (length(missing_required) > 0) {
    stop(sprintf("Input data is missing required columns: %s", paste(missing_required, collapse = ", ")), call. = FALSE)
  }

  data$ped_id <- as.character(data$ped_id)
  data$design_check <- as.character(data$design_check)
  if (!("set" %in% names(data))) {
    if (!single_set_if_missing) {
      stop("Input data must include a 'set' column for multi-set RCBD.", call. = FALSE)
    }
    data$set <- "A"
  }
  data$set <- as.character(data$set)
  if (any(is.na(data$set) | data$set == "")) {
    stop("Input column 'set' contains missing or empty values.", call. = FALSE)
  }

  if (site_random && site_num > 1L) {
    results <- list()
    for (site_index in seq_len(site_num)) {
      run_seed <- seed + site_index * 100000L
      item <- run_one_design(data, blocks, planter, run_seed, check_constraint, test_constraint, core_path)
      item$label <- paste0("site", site_index)
      item$parameters$base_seed <- seed
      item$parameters$run_seed <- run_seed
      results[[site_index]] <- item
    }
    payload <- list(
      ok = TRUE,
      design = "rcbd",
      results = results,
      parameters = list(
        site_num = site_num,
        site_random = TRUE,
        base_seed = seed,
        planter = planter,
        check_position_constraint = check_constraint,
        test_position_constraint = test_constraint
      )
    )
  } else {
    payload <- run_one_design(data, blocks, planter, seed, check_constraint, test_constraint, core_path)
    payload$parameters$site_num <- site_num
    payload$parameters$site_random <- FALSE
    if (site_num > 1L) {
      payload$assumptions <- c(payload$assumptions, sprintf("One common design template can be reused for %d sites.", site_num))
    }
  }

  json <- jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, null = "null", na = "null")
  if (!is.null(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    writeLines(json, output_path, useBytes = TRUE)
  } else {
    cat(json)
  }
}

`%||%` <- function(a, b) if (is.null(a)) b else a

result <- tryCatch(
  {
    main()
    NULL
  },
  error = function(e) {
    payload <- make_error("local_rcbd_error", conditionMessage(e))
    json <- jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, null = "null")
    args_parsed <- tryCatch(parse_args(args), error = function(...) list())
    output_path <- args_parsed[["output"]]
    if (!is.null(output_path)) {
      dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
      writeLines(json, output_path, useBytes = TRUE)
    } else {
      cat(json)
    }
    quit(status = 1)
  }
)

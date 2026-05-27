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
  mapping <- c(plot_id = "plots", block = "r", design_check = "hyb_check")
  for (src in names(mapping)) {
    dst <- mapping[[src]]
    if (src %in% names(df) && !(dst %in% names(df))) {
      names(df)[names(df) == src] <- dst
    }
  }
  keep <- c("plots", "r", "ped_id", "ranges", "pass", "set", "set_index", "set_ncols",
            "hyb_check", "hyb_type")
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

run_one_design <- function(data, blocks, planter, seed, check_constraint, test_constraint, core_path) {
  if (!planter %in% c("serpentine", "cartesian")) {
    stop("planter must be 'serpentine' or 'cartesian'", call. = FALSE)
  }

  source(core_path)

  has_cross_set_check_conflict <- function(previous_out, current_out) {
    if (is.null(previous_out) || nrow(previous_out) == 0 || nrow(current_out) == 0) {
      return(FALSE)
    }
    previous_row <- max(previous_out$ranges)
    current_row <- min(current_out$ranges)
    if (current_row != previous_row + 1L) {
      return(FALSE)
    }

    previous_checks <- previous_out[previous_out$ranges == previous_row & previous_out$hyb_type == "ck", , drop = FALSE]
    current_checks <- current_out[current_out$ranges == current_row & current_out$hyb_type == "ck", , drop = FALSE]
    if (nrow(previous_checks) == 0 || nrow(current_checks) == 0) {
      return(FALSE)
    }
    length(intersect(previous_checks$pass, current_checks$pass)) > 0
  }

  sets <- stable_set_order(data$set)
  all_rows <- list()
  sets_payload <- list()
  warnings <- list()
  plot_output_offset <- 0
  current_row_start <- 1L

  for (set_index in seq_along(sets)) {
    set_label <- sets[[set_index]]
    set_data <- data[data$set == set_label, , drop = FALSE]
    set_data$exp_group <- set_index
    set_ncols <- nrow(set_data)
    set_seed <- seed + set_index * 1000L
    set_nrows <- blocks

    max_boundary_tries <- 200L
    accepted <- FALSE
    out <- NULL
    run_seed <- set_seed
    retry_count <- 0L

    for (boundary_try in seq_len(max_boundary_tries)) {
      run_seed <- set_seed + (boundary_try - 1L) * 1000000L
      res <- rcbd_design_core(
        ped.list = set_data,
        blocks = blocks,
        nrows = set_nrows,
        ncols = set_ncols,
        seed = run_seed,
        planter = "serpentine",
        ck_constrain = check_constraint,
        hyb_constrain = test_constraint,
        plot_id_start = (current_row_start - 1L) * set_ncols + 1L,
        block_start = 1L
      )

      candidate <- res$out_design
      physical_row <- current_row_start + candidate$block - 1L
      position_in_row <- candidate$plot_within_block
      candidate$ranges <- physical_row
      candidate$pass <- ifelse(
        planter == "serpentine" & physical_row %% 2L == 0L,
        set_ncols + 1L - position_in_row,
        position_in_row
      )
      candidate$set <- set_label
      candidate$set_index <- set_index
      candidate$set_ncols <- set_ncols
      candidate <- standardize_rows(candidate)
      row.names(candidate) <- NULL

      previous_out <- if (length(all_rows) > 0) all_rows[[length(all_rows)]] else NULL
      if (check_constraint && has_cross_set_check_conflict(previous_out, candidate)) {
        retry_count <- retry_count + 1L
        next
      }

      out <- candidate
      accepted <- TRUE
      break
    }

    if (!accepted) {
      stop(sprintf("Set %s: cross-set check position constraint solution not found", set_label), call. = FALSE)
    }
    if (retry_count > 0L) {
      warnings[[length(warnings) + 1L]] <- sprintf(
        "Set %s was regenerated %d time(s) to avoid cross-set adjacent check conflicts.",
        set_label,
        retry_count
      )
    }

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
        seed = run_seed,
        base_seed = set_seed,
        cross_set_boundary_retries = retry_count
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
    warnings = warnings,
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
  root_dir <- skill_dir()
  input_path <- resolve_input_path(input, root_dir)
  output_path <- resolve_output_path(opts[["output"]], root_dir)

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

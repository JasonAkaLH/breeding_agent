suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for the mini diagonal runner.")
  }
})

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
    read.csv(input_path, stringsAsFactors = FALSE, check.names = FALSE)
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
  data$ped_id <- as.character(data$ped_id)
  data$hyb_check <- as.character(data$hyb_check)
  data$set <- as.character(data$set)

  if (any(is.na(data$ped_id) | data$ped_id == "")) stop("Input column 'ped_id' contains missing or empty values.", call. = FALSE)
  if (any(is.na(data$set) | data$set == "")) stop("Input column 'set' contains missing or empty values.", call. = FALSE)
  if (any(!(data$hyb_check %in% c("0", "1", "2")))) stop("Input column 'hyb_check' must contain only 0, 1, or 2.", call. = FALSE)
  if (sum(data$hyb_check == "2") < 1) stop("At least one row must have hyb_check = 2 for diagonal checks.", call. = FALSE)
  if (sum(data$hyb_check != "2") < 1) stop("At least one row must have hyb_check != 2 for test materials.", call. = FALSE)

  data.frame(
    ped_id = data$ped_id,
    design_check = data$hyb_check,
    set = data$set,
    stringsAsFactors = FALSE
  )
}

standardize_output <- function(out_design) {
  out <- out_design
  names(out)[names(out) == "plot_id"] <- "plots"
  keep <- c("plots", "ped_id", "hyb_type", "ranges", "pass", "set", "design_check")
  out <- out[, intersect(keep, names(out)), drop = FALSE]
  out$plots <- as.integer(out$plots)
  out$ranges <- as.integer(out$ranges)
  out$pass <- as.integer(out$pass)
  out$ped_id <- as.character(out$ped_id)
  out$set <- as.character(out$set)
  out$design_check <- as.character(out$design_check)
  row.names(out) <- NULL
  out
}

level_label <- function(level) {
  switch(level,
    "A" = "Level A (5%-10%)",
    "B" = "Level B (10%-15%)",
    "C" = "Level C (15%-20%)",
    level
  )
}

make_distribution_qc <- function(out, ncols, nrows, shared_config = NULL) {
  ck <- out[out$hyb_type == "ck", , drop = FALSE]
  if (nrow(ck) == 0) {
    return(list(
      check_count = 0,
      row_check_counts = list(),
      column_check_counts = list(),
      check_count_by_set = list(),
      unique_check_materials_by_set = list(),
      adjacent_check_pairs = 0,
      row_count_range = list(min = 0, max = 0),
      column_count_range = list(min = 0, max = 0),
      diagonal_mode = NULL,
      strict_diagonal_passed = FALSE,
      mean_distance_to_diagonal = NULL,
      max_distance_to_diagonal = NULL,
      off_diagonal_check_count = 0,
      controlled_rescue_count = 0,
      check_count_by_diagonal_line = list(),
      active_diagonal_count = 0,
      target_diagonal_count = NULL,
      singleton_diagonal_line_count = 0,
      within_line_gap_cv = NULL,
      parallel_line_gap_cv = NULL,
      row_check_count_cv = NULL,
      column_check_count_cv = NULL,
      set_ck_target = list(),
      set_ck_actual = list(),
      set_ck_shortage = list()
    ))
  }

  row_counts <- table(factor(ck$ranges, levels = seq_len(nrows)))
  col_counts <- table(factor(ck$pass, levels = seq_len(ncols)))
  set_counts <- table(ck$set)
  material_col <- if ("ped_id" %in% names(ck)) "ped_id" else "trt"
  unique_by_set <- lapply(split(ck[[material_col]], ck$set), function(x) sort(unique(as.character(x)), method = "radix"))

  adjacent_pairs <- 0L
  if (nrow(ck) > 1) {
    coords <- ck[, c("ranges", "pass"), drop = FALSE]
    for (i in 1:(nrow(coords) - 1L)) {
      for (j in (i + 1L):nrow(coords)) {
        dist <- max(abs(coords$ranges[[i]] - coords$ranges[[j]]), abs(coords$pass[[i]] - coords$pass[[j]]))
        if (dist <= 1) adjacent_pairs <- adjacent_pairs + 1L
      }
    }
  }

  row_count_values <- as.integer(row_counts)
  col_count_values <- as.integer(col_counts)
  set_count_values <- as.integer(set_counts)
  names(row_count_values) <- names(row_counts)
  names(col_count_values) <- names(col_counts)
  names(set_count_values) <- names(set_counts)

  selected_axes <- NULL
  diagonal_mode <- NULL
  set_target <- list()
  if (!is.null(shared_config)) {
    selected_axes <- shared_config$selected_diags
    diagonal_mode <- shared_config$config$diagonal_mode %||% NULL
    if (!is.null(shared_config$ck_quota)) set_target <- as.list(as.integer(shared_config$ck_quota))
    if (!is.null(shared_config$ck_quota)) names(set_target) <- names(shared_config$ck_quota)
  }

  min_dim <- min(nrows, ncols)
  ck_u <- (ck$pass - 0.5) / ncols
  ck_v <- (ck$ranges - 0.5) / nrows
  ck_axis <- ck_u - ck_v
  ck_pos <- (ck_u + ck_v) / 2

  if (!is.null(selected_axes) && length(selected_axes) > 0) {
    dist_matrix <- abs(outer(ck_axis, selected_axes, "-"))
    line_id <- max.col(-dist_matrix, ties.method = "first")
    diagonal_distances <- dist_matrix[cbind(seq_len(nrow(dist_matrix)), line_id)] * min_dim
    line_counts <- table(factor(line_id, levels = seq_along(selected_axes)))
    line_count_values <- as.integer(line_counts)
    names(line_count_values) <- names(line_counts)
    active_line_counts <- line_count_values[line_count_values > 0]

    within_line_cvs <- vapply(seq_along(selected_axes), function(i) {
      pos <- sort(ck_pos[line_id == i])
      if (length(pos) <= 2) return(0)
      cv_safe(diff(pos))
    }, numeric(1))

    parallel_line_gap_cv <- if (length(selected_axes) <= 2) {
      0
    } else {
      cv_safe(diff(sort(selected_axes)) * min_dim)
    }
  } else {
    line_id <- integer(nrow(ck))
    diagonal_distances <- rep(NA_real_, nrow(ck))
    line_count_values <- integer(0)
    active_line_counts <- integer(0)
    within_line_cvs <- numeric(0)
    parallel_line_gap_cv <- NA_real_
  }

  mean_distance_to_diagonal <- if (all(is.na(diagonal_distances))) NA_real_ else mean(diagonal_distances, na.rm = TRUE)
  max_distance_to_diagonal <- if (all(is.na(diagonal_distances))) NA_real_ else max(diagonal_distances, na.rm = TRUE)
  off_diagonal_check_count <- if (all(is.na(diagonal_distances))) NA_integer_ else sum(diagonal_distances > 0.55, na.rm = TRUE)
  controlled_rescue_count <- if (all(is.na(diagonal_distances))) NA_integer_ else sum(diagonal_distances > 1.05, na.rm = TRUE)

  set_actual_named <- as.list(set_count_values)
  set_shortage <- list()
  if (length(set_target) > 0) {
    target_names <- names(set_target)
    actual_lookup <- set_count_values
    for (nm in target_names) {
      actual_value <- if (nm %in% names(actual_lookup)) as.integer(actual_lookup[[nm]]) else 0L
      set_shortage[[nm]] <- max(0L, as.integer(set_target[[nm]]) - actual_value)
    }
  }

  list(
    check_count = nrow(ck),
    row_check_counts = as.list(row_count_values),
    column_check_counts = as.list(col_count_values),
    check_count_by_set = as.list(set_count_values),
    unique_check_materials_by_set = unique_by_set,
    adjacent_check_pairs = adjacent_pairs,
    row_count_range = list(min = min(row_count_values), max = max(row_count_values)),
    column_count_range = list(min = min(col_count_values), max = max(col_count_values)),
    diagonal_mode = diagonal_mode,
    strict_diagonal_passed = isTRUE(!is.na(max_distance_to_diagonal) && max_distance_to_diagonal <= 0.55),
    mean_distance_to_diagonal = mean_distance_to_diagonal,
    max_distance_to_diagonal = max_distance_to_diagonal,
    off_diagonal_check_count = off_diagonal_check_count,
    controlled_rescue_count = controlled_rescue_count,
    check_count_by_diagonal_line = as.list(line_count_values),
    active_diagonal_count = length(active_line_counts),
    target_diagonal_count = if (!is.null(shared_config)) shared_config$config$target_diagonal_count %||% NULL else NULL,
    singleton_diagonal_line_count = sum(active_line_counts == 1L),
    within_line_gap_cv = if (length(within_line_cvs) == 0) NA_real_ else mean(within_line_cvs),
    parallel_line_gap_cv = parallel_line_gap_cv,
    row_check_count_cv = cv_safe(row_count_values),
    column_check_count_cv = cv_safe(col_count_values),
    set_ck_target = set_target,
    set_ck_actual = set_actual_named,
    set_ck_shortage = set_shortage,
    distribution_note = "All generated check plots are selected from a global normalized diagonal skeleton first, then assigned to sets. Row and column balance are secondary optimization criteria."
  )
}

run_with_level <- function(data, ck_ratio, ncols, nrows, planter, randomize, seed) {
  data$design_check <- as.numeric(as.character(data$design_check))
  data$set <- as.character(data$set)
  prevalidation <- validate_diagonal_design(data, ck_ratio, ncols, nrows)
  if (!isTRUE(prevalidation$valid)) stop(prevalidation$message, call. = FALSE)
  ck_ratio_range <- get_ck_ratio_range(ck_ratio)
  shared_config <- diagonal_solve_layout(data, ck_ratio_range, ncols, nrows, planter)
  design <- diagonal_design_core(shared_config, data, randomize, seed)
  out <- standardize_output(design$out_design)
  actual_check_ratio <- sum(out$hyb_type == "ck") / nrow(out)

  list(
    out_design = out,
    config = shared_config$config,
    nrows = shared_config$nrows,
    total_plots = shared_config$total_plots,
    actual_check_ratio = actual_check_ratio,
    quality_control = make_distribution_qc(out, ncols, shared_config$nrows, shared_config)
  )
}

main <- function() {
  opts <- parse_args(args)
  runner_dir <- script_dir()
  core_path <- file.path(runner_dir, "diagonal_design_core.R")
  if (!file.exists(core_path)) stop(sprintf("Missing bundled diagonal core script: %s", core_path), call. = FALSE)
  source(core_path)

  input <- opts[["input"]]
  if (is.null(input)) stop("Missing required argument --input", call. = FALSE)
  root_dir <- skill_dir()
  input_path <- resolve_input_path(input, root_dir)
  output_path <- resolve_output_path(opts[["output"]], root_dir)

  ncols <- as_int(opts[["ncols"]], "ncols")
  if (ncols < 4) stop("--ncols must be at least 4 for mini diagonal design.", call. = FALSE)
  nrows <- if (is.null(opts[["nrows"]])) NULL else as_int(opts[["nrows"]], "nrows")
  requested_ck_ratio <- toupper(opts[["ck-ratio"]] %||% "A")
  if (!requested_ck_ratio %in% c("A", "B", "C")) stop("--ck-ratio must be one of A, B, or C.", call. = FALSE)
  planter <- opts[["planter"]] %||% "serpentine"
  if (!planter %in% c("serpentine", "cartesian")) stop("--planter must be serpentine or cartesian.", call. = FALSE)
  randomize <- as_bool(opts[["randomize"]], default = TRUE)
  seed <- if (is.null(opts[["seed"]])) sample.int(900000L, 1L) + 10000L else as_int(opts[["seed"]], "seed")

  data <- read_materials(input_path)
  levels_to_try <- c("A", "B", "C")
  levels_to_try <- levels_to_try[match(requested_ck_ratio, levels_to_try):length(levels_to_try)]
  attempts <- list()
  result <- NULL
  used_ck_ratio <- NULL

  for (level in levels_to_try) {
    item_result <- NULL
    attempt <- tryCatch(
      {
        item_result <- run_with_level(data, level, ncols, nrows, planter, randomize, seed)
        list(level = level, ok = TRUE, message = "success")
      },
      error = function(e) list(level = level, ok = FALSE, message = conditionMessage(e))
    )
    attempts[[length(attempts) + 1L]] <- attempt
    if (isTRUE(attempt$ok)) {
      result <- item_result
      used_ck_ratio <- level
      break
    }
  }

  if (is.null(result)) {
    payload <- make_error(
      "mini_diagonal_no_valid_level",
      "Unable to generate a valid diagonal design using the requested level or higher levels.",
      list(requested_ck_ratio = requested_ck_ratio, attempts = attempts)
    )
  } else {
    auto_upgraded <- used_ck_ratio != requested_ck_ratio
    payload <- list(
      ok = TRUE,
      design = "diagonal",
      out_design = result$out_design,
      parameters = list(
        requested_ck_ratio = requested_ck_ratio,
        requested_ck_ratio_label = level_label(requested_ck_ratio),
        used_ck_ratio = used_ck_ratio,
        used_ck_ratio_label = level_label(used_ck_ratio),
        auto_upgraded = auto_upgraded,
        actual_check_ratio = result$actual_check_ratio,
        actual_check_percent = sprintf("%.1f%%", result$actual_check_ratio * 100),
        ncols = ncols,
        nrows = result$nrows,
        total_plots = result$total_plots,
        diagonal_spacing = result$config$spacing,
        diagonal_interval = result$config$diagonal_interval,
        diagonal_count = result$config$diagonal_count,
        candidate_diagonal_count = result$config$candidate_diagonal_count,
        diagonal_axes = as.numeric(result$config$selected_diags),
        target_diagonal_count = result$config$target_diagonal_count,
        singleton_diagonal_line_count = result$config$singleton_diagonal_line_count,
        diagonal_angle_deg = round(atan(result$nrows / ncols) * 180 / pi, 2),
        diagonal_mode = result$config$diagonal_mode,
        mean_distance_to_diagonal = result$config$mean_distance_to_diagonal,
        max_distance_to_diagonal = result$config$max_distance_to_diagonal,
        off_diagonal_check_count = result$config$off_diagonal_check_count,
        controlled_rescue_count = result$config$controlled_rescue_count,
        planter = planter,
        randomize = randomize,
        seed = seed
      ),
      ck_ratio_levels = list(
        A = "5%-10% low-density diagonal checks",
        B = "10%-15% medium-density diagonal checks",
        C = "15%-20% high-density diagonal checks"
      ),
      quality_control = result$quality_control,
      attempts = attempts
    )
  }

  json <- jsonlite::toJSON(payload, pretty = TRUE, auto_unbox = TRUE, null = "null", na = "null")
  if (!is.null(output_path)) {
    dir.create(dirname(output_path), recursive = TRUE, showWarnings = FALSE)
    writeLines(json, output_path, useBytes = TRUE)
  } else {
    cat(json)
  }
  if (isFALSE(payload$ok)) quit(status = 1)
}

`%||%` <- function(a, b) if (is.null(a)) b else a

tryCatch(
  main(),
  error = function(e) {
    payload <- make_error("mini_diagonal_error", conditionMessage(e))
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

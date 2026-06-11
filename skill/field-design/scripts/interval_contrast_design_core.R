suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("R package 'jsonlite' is required for interval contrast design.")
  }
})

stable_set_order <- function(values) {
  unique(as.character(values))
}

make_interval_path <- function(ncols, nrows_rep, planter) {
  grid <- expand.grid(pass = seq_len(ncols), ranges = seq_len(nrows_rep))
  if (identical(planter, "serpentine")) {
    grid$order_pass <- grid$pass
    even_rows <- grid$ranges %% 2L == 0L
    grid$order_pass[even_rows] <- ncols + 1L - grid$order_pass[even_rows]
    grid <- grid[order(grid$ranges, grid$order_pass), , drop = FALSE]
    grid$order_pass <- NULL
  } else {
    grid <- grid[order(grid$ranges, grid$pass), , drop = FALSE]
  }
  row.names(grid) <- NULL
  grid
}

estimate_plots_per_rep <- function(test_count, ck_params) {
  test_idx <- 1L
  pos <- 1L
  next_pos <- as.integer(ck_params$start_pos)
  step <- as.integer(ck_params$interval) + 1L

  while (test_idx <= test_count) {
    hit <- which(next_pos == pos)
    if (length(hit) > 1L) {
      stop(
        sprintf(
          "Check overlap detected at position %d for CK(s): %s",
          pos,
          paste(ck_params$ped_id[hit], collapse = ", ")
        ),
        call. = FALSE
      )
    }
    if (length(hit) == 1L) {
      next_pos[hit] <- next_pos[hit] + step[hit]
    } else {
      test_idx <- test_idx + 1L
    }
    pos <- pos + 1L
  }

  pos - 1L
}

interval_contrast_core <- function(ped_list, ck_params, ncols, nrows = NULL,
                                   planter = "serpentine", randomize = TRUE,
                                   seed = NULL, plot_id_start = 1L) {
  if (!planter %in% c("serpentine", "cartesian")) {
    stop("planter must be 'serpentine' or 'cartesian'.", call. = FALSE)
  }

  ped_list$ped_id <- enc2utf8(as.character(ped_list$ped_id))
  ped_list$hyb_check <- enc2utf8(as.character(ped_list$hyb_check))
  ped_list$set <- enc2utf8(as.character(ped_list$set))
  ck_params$ped_id <- enc2utf8(as.character(ck_params$ped_id))
  if ("set" %in% names(ck_params)) ck_params$set <- enc2utf8(as.character(ck_params$set))
  ck_params$start_pos <- as.integer(ck_params$start_pos)
  ck_params$interval <- as.integer(ck_params$interval)

  check_data <- ped_list[ped_list$hyb_check != "0", , drop = FALSE]
  test_data <- ped_list[ped_list$hyb_check == "0", , drop = FALSE]
  test_list <- as.character(test_data$ped_id)
  test_count <- length(test_list)

  if (nrow(check_data) == 0L) stop("Interval design requires at least one CK row (hyb_check != 0).", call. = FALSE)
  if (test_count == 0L) stop("Interval design requires at least one test row (hyb_check = 0).", call. = FALSE)

  if ("set" %in% names(ck_params)) {
    ck_params <- ck_params[ck_params$set == ped_list$set[[1]], , drop = FALSE]
  }
  ck_params <- ck_params[match(check_data$ped_id, ck_params$ped_id), , drop = FALSE]
  if (nrow(ck_params) != nrow(check_data) || any(is.na(ck_params$ped_id))) {
    stop(sprintf("Missing interval parameters for one or more CKs in set %s.", ped_list$set[[1]]), call. = FALSE)
  }
  check_lookup <- setNames(as.character(check_data$hyb_check), as.character(check_data$ped_id))

  plot_id_counter <- as.integer(plot_id_start)

  plots_per_run <- estimate_plots_per_rep(test_count, ck_params)
  nrows_run <- if (is.null(nrows)) ceiling(plots_per_run / ncols) else as.integer(nrows)
  if (nrows_run * ncols < plots_per_run) {
    stop(sprintf("nrows * ncols is too small for set %s.", ped_list$set[[1]]), call. = FALSE)
  }

  grid_ordered <- make_interval_path(ncols, nrows_run, planter)
  path_to_fill <- grid_ordered[seq_len(plots_per_run), , drop = FALSE]

  if (!is.null(seed)) set.seed(seed)
  shuffled_tests <- if (isTRUE(randomize)) sample(test_list) else test_list

  check_plan <- lapply(seq_len(nrow(ck_params)), function(i) {
    list(
      name = ck_params$ped_id[[i]],
      hyb_check = check_lookup[[ck_params$ped_id[[i]]]],
      step = ck_params$interval[[i]] + 1L,
      next_pos = ck_params$start_pos[[i]]
    )
  })

  path_to_fill$ped_id <- NA_character_
  path_to_fill$hyb_type <- NA_character_
  path_to_fill$hyb_check <- NA_character_

  test_material_idx <- 1L
  for (i in seq_len(nrow(path_to_fill))) {
    hits <- which(vapply(check_plan, function(ck) ck$next_pos == i, logical(1)))
    if (length(hits) > 1L) {
      hit_names <- vapply(check_plan[hits], function(ck) ck$name, character(1))
      stop(
        sprintf("Check overlap detected at position %d for CK(s): %s", i, paste(hit_names, collapse = ", ")),
        call. = FALSE
      )
    } else if (length(hits) == 1L) {
      j <- hits[[1]]
      path_to_fill$ped_id[[i]] <- check_plan[[j]]$name
      path_to_fill$hyb_type[[i]] <- "ck"
      path_to_fill$hyb_check[[i]] <- check_plan[[j]]$hyb_check
      check_plan[[j]]$next_pos <- check_plan[[j]]$next_pos + check_plan[[j]]$step
    } else {
      path_to_fill$ped_id[[i]] <- shuffled_tests[[test_material_idx]]
      path_to_fill$hyb_type[[i]] <- "hyb"
      path_to_fill$hyb_check[[i]] <- "0"
      test_material_idx <- test_material_idx + 1L
    }
  }

  path_to_fill$r <- 1L
  path_to_fill$plots <- plot_id_counter:(plot_id_counter + nrow(path_to_fill) - 1L)
  path_to_fill$set <- ped_list$set[[1]]

  out_design <- path_to_fill[, c(
    "plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"
  ), drop = FALSE]
  row.names(out_design) <- NULL
  out_design
}

run_interval_contrast <- function(data, ck_params, ncols, nrows = NULL,
                                  planter = "serpentine", randomize = TRUE,
                                  seed = NULL) {
  sets <- stable_set_order(data$set)
  all_rows <- list()
  plot_id_start <- 1L

  for (set_index in seq_along(sets)) {
    set_label <- sets[[set_index]]
    set_data <- data[data$set == set_label, , drop = FALSE]
    set_checks <- set_data[set_data$hyb_check != "0", , drop = FALSE]
    if ("set" %in% names(ck_params)) {
      set_ck_params <- ck_params[ck_params$set == set_label & ck_params$ped_id %in% set_checks$ped_id, , drop = FALSE]
    } else {
      set_ck_params <- ck_params[ck_params$ped_id %in% set_checks$ped_id, , drop = FALSE]
    }
    set_seed <- if (is.null(seed)) NULL else seed + set_index * 1000L

    set_out <- interval_contrast_core(
      ped_list = set_data,
      ck_params = set_ck_params,
      ncols = ncols,
      nrows = nrows,
      planter = planter,
      randomize = randomize,
      seed = set_seed,
      plot_id_start = plot_id_start
    )

    plot_id_start <- max(set_out$plots) + 1L
    all_rows[[length(all_rows) + 1L]] <- set_out
  }

  out <- do.call(rbind, all_rows)
  global_nrows <- if (is.null(nrows)) ceiling(nrow(out) / ncols) else as.integer(nrows)
  if (global_nrows * ncols < nrow(out)) {
    stop("nrows * ncols is too small for the combined interval design.", call. = FALSE)
  }
  global_path <- make_interval_path(ncols, global_nrows, planter)
  global_path <- global_path[seq_len(nrow(out)), , drop = FALSE]
  out$plots <- seq_len(nrow(out))
  out$ranges <- global_path$ranges
  out$pass <- global_path$pass
  out$r <- 1L
  out$ped_id <- enc2utf8(as.character(out$ped_id))
  out$set <- enc2utf8(as.character(out$set))
  out$hyb_check <- enc2utf8(as.character(out$hyb_check))
  out$hyb_type <- enc2utf8(as.character(out$hyb_type))
  row.names(out) <- NULL

  sets_payload <- list()
  for (set_index in seq_along(sets)) {
    set_label <- sets[[set_index]]
    set_rows <- out[out$set == set_label, , drop = FALSE]
    sets_payload[[set_label]] <- list(
      out_design = set_rows,
      parameters = list(
        set = set_label,
        ncols = ncols,
        nrows = if (is.null(nrows)) NULL else as.integer(nrows),
        seed = if (is.null(seed)) NULL else seed + set_index * 1000L
      )
    )
  }

  list(
    ok = TRUE,
    design = "interval",
    out_design = out,
    sets = sets_payload,
    parameters = list(
      sets = sets,
      ncols = ncols,
      nrows = if (is.null(nrows)) NULL else as.integer(nrows),
      planter = planter,
      randomize = randomize,
      seed = seed,
      ck_params = ck_params
    )
  )
}

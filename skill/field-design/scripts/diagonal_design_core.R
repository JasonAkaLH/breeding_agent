# Standalone diagonal design core for mini-field-design.
# The solver keeps user-provided ncols fixed and optimizes check slots against
# normalized field diagonals before assigning materials.

`%||%` <- function(a, b) if (is.null(a)) b else a

cv_safe <- function(x) {
  x <- as.numeric(x)
  if (length(x) <= 1 || mean(x) == 0) return(0)
  stats::sd(x) / mean(x)
}

count_adjacent_pairs <- function(coords) {
  if (is.null(coords) || nrow(coords) <= 1) return(0L)
  out <- 0L
  for (i in 1:(nrow(coords) - 1L)) {
    for (j in (i + 1L):nrow(coords)) {
      dist <- max(
        abs(coords$ranges[[i]] - coords$ranges[[j]]),
        abs(coords$pass[[i]] - coords$pass[[j]])
      )
      if (dist <= 1) out <- out + 1L
    }
  }
  out
}

target_active_diagonal_count <- function(check_count) {
  desired <- ceiling(check_count / 4)
  desired <- max(3L, min(7L, desired))
  desired <- min(desired, max(1L, check_count))
  if (desired > 1L && desired %% 2L == 0L) desired <- desired + 1L
  desired
}

build_field_geometry <- function(nrows, ncols, planter) {
  total_plots <- nrows * ncols
  ranges <- rep(seq_len(nrows), each = ncols)
  pass <- integer(total_plots)

  for (r in seq_len(nrows)) {
    idx <- ((r - 1L) * ncols + 1L):(r * ncols)
    if (planter == "serpentine" && r %% 2L == 0L) {
      pass[idx] <- ncols:1L
    } else {
      pass[idx] <- seq_len(ncols)
    }
  }

  u <- (pass - 0.5) / ncols
  v <- (ranges - 0.5) / nrows

  data.frame(
    plot_id = seq_len(total_plots),
    ranges = ranges,
    pass = pass,
    u = u,
    v = v,
    diag_axis = u - v,
    diag_pos = (u + v) / 2,
    diag = pass - ranges,
    stringsAsFactors = FALSE
  )
}

get_ck_ratio_range <- function(level) {
  switch(level,
    "A" = list(min = 0.05, max = 0.10, mid = 0.075, level = "A"),
    "B" = list(min = 0.10, max = 0.15, mid = 0.125, level = "B"),
    "C" = list(min = 0.15, max = 0.20, mid = 0.175, level = "C"),
    stop("Invalid level. Must be 'A', 'B', or 'C'")
  )
}

allocate_ck_quota_by_set <- function(ped.list, total_plots, ck_ratio_range = NULL) {
  ped.list$set <- as.character(ped.list$set)
  sets <- sort(unique(ped.list$set), method = "radix")
  all_tests <- ped.list[ped.list$design_check != 2, , drop = FALSE]
  all_checks <- ped.list[ped.list$design_check == 2, , drop = FALSE]

  test_counts <- as.integer(table(factor(all_tests$set, levels = sets)))
  names(test_counts) <- sets
  unique_checks <- vapply(sets, function(grp) {
    length(unique(all_checks$ped_id[all_checks$set == grp]))
  }, integer(1))

  total_tests <- nrow(all_tests)
  total_ck_slots <- total_plots - total_tests
  required_min_ck <- unique_checks
  if (!is.null(ck_ratio_range)) {
    ratio_min_ck <- ceiling((ck_ratio_range$min / (1 - ck_ratio_range$min)) * test_counts)
    required_min_ck <- pmax(required_min_ck, ratio_min_ck)
  }

  min_ck_total <- sum(required_min_ck)

  if (total_ck_slots < min_ck_total) {
    return(list(
      ok = FALSE,
      message = sprintf(
        "Need at least %d CK slots to satisfy per-set minimum check density, but only %d slots are available.",
        min_ck_total, total_ck_slots
      )
    ))
  }

  remaining <- total_ck_slots - min_ck_total
  weights <- test_counts
  if (sum(weights) == 0) weights <- rep(1L, length(sets))
  raw_extra <- if (remaining > 0) remaining * weights / sum(weights) else rep(0, length(sets))
  extra <- floor(raw_extra)
  diff <- remaining - sum(extra)

  if (diff > 0) {
    frac <- raw_extra - floor(raw_extra)
    add_order <- order(frac, weights, decreasing = TRUE)
    for (i in seq_len(diff)) {
      idx <- add_order[((i - 1L) %% length(add_order)) + 1L]
      extra[[idx]] <- extra[[idx]] + 1L
    }
  }

  ck_quota <- as.integer(required_min_ck + extra)
  names(ck_quota) <- sets
  set_slots <- as.integer(test_counts + ck_quota)
  names(set_slots) <- sets

  list(
    ok = TRUE,
    sets = sets,
    test_counts = test_counts,
    min_ck = unique_checks,
    required_min_ck = required_min_ck,
    ck_quota = ck_quota,
    set_slots = set_slots,
    total_ck_slots = total_ck_slots
  )
}

assign_set_blocks <- function(total_plots, set_slots, sets) {
  assignments <- rep(NA_character_, total_plots)
  cursor <- 1L
  for (grp in sets) {
    n <- as.integer(set_slots[[grp]])
    if (n <= 0) next
    end <- min(total_plots, cursor + n - 1L)
    assignments[cursor:end] <- grp
    cursor <- end + 1L
  }
  if (any(is.na(assignments))) {
    last_set <- tail(sets, 1L)
    assignments[is.na(assignments)] <- last_set
  }
  assignments
}

make_selected_axes <- function(geometry, spacing_grid, nrows, ncols) {
  min_dim <- min(nrows, ncols)
  spacing_axis <- spacing_grid / min_dim
  axis_min <- min(geometry$diag_axis)
  axis_max <- max(geometry$diag_axis)

  axes <- c(0)
  d <- spacing_axis
  while (d <= axis_max + 1e-9) {
    axes <- c(axes, d)
    d <- d + spacing_axis
  }
  d <- -spacing_axis
  while (d >= axis_min - 1e-9) {
    axes <- c(axes, d)
    d <- d - spacing_axis
  }

  sort(unique(round(axes, 10)))
}

build_diagonal_candidates <- function(geometry, selected_axes, diagonal_interval,
                                      phase_seed, set_assignments, nrows, ncols) {
  min_dim <- min(nrows, ncols)
  axis_distance <- abs(outer(geometry$diag_axis, selected_axes, "-"))
  line_id <- max.col(-axis_distance, ties.method = "first")
  diag_distance <- axis_distance[cbind(seq_len(nrow(axis_distance)), line_id)] * min_dim
  nearest_axis <- selected_axes[line_id]

  interval_t <- diagonal_interval / min_dim
  phase <- (((line_id - 1L) * 0.37 + phase_seed * 0.19) * interval_t) %% interval_t
  line_start <- abs(nearest_axis) / 2
  rel <- (geometry$diag_pos - line_start + phase) %% interval_t
  anchor_distance <- pmin(rel, interval_t - rel) * min_dim

  out <- geometry
  out$set <- as.character(set_assignments)
  out$line_id <- line_id
  out$line_axis <- nearest_axis
  out$diag_distance <- diag_distance
  out$anchor_distance <- anchor_distance
  out$base_score <- diag_distance * 15 + anchor_distance * 2
  out
}

is_adjacent_to_selected <- function(row, col, selected_coords) {
  if (is.null(selected_coords) || nrow(selected_coords) == 0) return(FALSE)
  any(pmax(abs(selected_coords$ranges - row), abs(selected_coords$pass - col)) <= 1)
}

line_spacing_penalty <- function(line_id, diag_pos, selected_line_pos, min_dim) {
  key <- as.character(line_id)
  existing <- selected_line_pos[[key]]
  if (is.null(existing) || length(existing) == 0) return(0)

  gaps <- abs(existing - diag_pos) * min_dim
  min_gap <- min(gaps)
  close_penalty <- if (min_gap < 2.0) (2.0 - min_gap)^2 * 80 else 0

  all_pos <- sort(c(existing, diag_pos))
  uniformity_penalty <- if (length(all_pos) >= 3) cv_safe(diff(all_pos)) * 6 else 0
  spread_penalty <- 2 / (min_gap + 0.25)

  close_penalty + uniformity_penalty + spread_penalty
}

line_uniformity_penalty <- function(line_id, diag_pos, selected_line_pos, line_pos_bounds, min_dim) {
  key <- as.character(line_id)
  existing <- selected_line_pos[[key]]
  if (is.null(existing) || length(existing) == 0) return(0)

  bounds <- line_pos_bounds[[key]]
  if (is.null(bounds) || length(bounds) != 2L || bounds[[2]] <= bounds[[1]]) return(0)

  all_pos <- sort(c(existing, diag_pos))
  line_span <- bounds[[2]] - bounds[[1]]
  occupied_span <- max(all_pos) - min(all_pos)
  boundary_gaps <- c(all_pos[[1]] - bounds[[1]], diff(all_pos), bounds[[2]] - tail(all_pos, 1L))
  boundary_gaps <- pmax(boundary_gaps, 1e-6)

  min_gap <- min(diff(all_pos)) * min_dim
  close_penalty <- if (length(all_pos) > 1L && min_gap < 2.5) (2.5 - min_gap)^2 * 140 else 0
  coverage_penalty <- ((line_span - occupied_span) / line_span)^2 * 35
  gap_cv_penalty <- cv_safe(boundary_gaps) * 32

  close_penalty + coverage_penalty + gap_cv_penalty
}

select_ck_slots <- function(candidates, ck_quota, mode, nrows, ncols) {
  threshold <- switch(mode,
    strict_diagonal = 0.55,
    diagonal_band = 1.05,
    controlled_rescue = Inf,
    Inf
  )

  allowed <- candidates[candidates$diag_distance <= threshold, , drop = FALSE]
  sets <- names(ck_quota)
  if (nrow(allowed) == 0) {
    return(list(ok = FALSE, message = "No diagonal candidates are available."))
  }

  available_by_set <- table(factor(allowed$set, levels = sets))
  shortage_sets <- sets[as.integer(available_by_set) < as.integer(ck_quota)]
  if (length(shortage_sets) > 0) {
    return(list(
      ok = FALSE,
      message = sprintf(
        "Not enough %s candidates for set(s): %s",
        mode, paste(shortage_sets, collapse = ", ")
      )
    ))
  }

  scarcity <- as.numeric(available_by_set) / pmax(1, as.integer(ck_quota))
  names(scarcity) <- sets
  set_order <- names(sort(scarcity, decreasing = FALSE))

  row_occ <- rep(0L, nrows)
  col_occ <- rep(0L, ncols)
  line_occ <- rep(0L, max(candidates$line_id))
  selected_ids <- integer(0)
  selected_rows <- list()
  selected_coords <- data.frame(ranges = integer(0), pass = integer(0))
  selected_line_pos <- list()
  min_dim <- min(nrows, ncols)
  line_pos_bounds <- lapply(split(allowed$diag_pos, allowed$line_id), function(x) range(x, na.rm = TRUE))

  for (grp in set_order) {
    need <- as.integer(ck_quota[[grp]])
    if (need <= 0) next
    for (k in seq_len(need)) {
      pool <- allowed[allowed$set == grp & !(allowed$plot_id %in% selected_ids), , drop = FALSE]
      if (nrow(pool) == 0) {
        return(list(ok = FALSE, message = sprintf("Unable to fill CK quota for set %s.", grp)))
      }

      adjacent <- vapply(seq_len(nrow(pool)), function(i) {
        is_adjacent_to_selected(pool$ranges[[i]], pool$pass[[i]], selected_coords)
      }, logical(1))

      dynamic_score <- pool$base_score +
        row_occ[pool$ranges] * 6.0 +
        col_occ[pool$pass] * 0.8 +
        line_occ[pool$line_id] * 12.0 +
        ifelse(line_occ[pool$line_id] == 0L, -8.0, 0)

      if (any(!adjacent)) {
        pool <- pool[!adjacent, , drop = FALSE]
        dynamic_score <- dynamic_score[!adjacent]
      } else {
        dynamic_score <- dynamic_score + if (mode == "controlled_rescue") 100 else 1000
      }

      dynamic_score <- dynamic_score +
        vapply(seq_len(nrow(pool)), function(i) {
          line_spacing_penalty(pool$line_id[[i]], pool$diag_pos[[i]], selected_line_pos, min_dim)
        }, numeric(1))
      dynamic_score <- dynamic_score +
        vapply(seq_len(nrow(pool)), function(i) {
          line_uniformity_penalty(pool$line_id[[i]], pool$diag_pos[[i]], selected_line_pos, line_pos_bounds, min_dim)
        }, numeric(1))

      chosen_idx <- which.min(dynamic_score)
      chosen <- pool[chosen_idx, , drop = FALSE]
      selected_ids <- c(selected_ids, chosen$plot_id)
      selected_rows[[length(selected_rows) + 1L]] <- chosen
      selected_coords <- rbind(
        selected_coords,
        data.frame(ranges = chosen$ranges, pass = chosen$pass)
      )
      row_occ[[chosen$ranges]] <- row_occ[[chosen$ranges]] + 1L
      col_occ[[chosen$pass]] <- col_occ[[chosen$pass]] + 1L
      line_occ[[chosen$line_id]] <- line_occ[[chosen$line_id]] + 1L
      line_key <- as.character(chosen$line_id)
      selected_line_pos[[line_key]] <- c(selected_line_pos[[line_key]], chosen$diag_pos)
    }
  }

  selected <- do.call(rbind, selected_rows)
  selected <- selected[order(selected$plot_id), , drop = FALSE]
  row.names(selected) <- NULL

  list(ok = TRUE, selected = selected)
}

score_selected_layout <- function(selected, selected_axes, ck_quota, ck_ratio_range,
                                  actual_ratio, mode, nrows, ncols) {
  row_counts <- as.integer(table(factor(selected$ranges, levels = seq_len(nrows))))
  col_counts <- as.integer(table(factor(selected$pass, levels = seq_len(ncols))))
  line_counts <- as.integer(table(factor(selected$line_id, levels = seq_along(selected_axes))))
  set_counts <- as.integer(table(factor(selected$set, levels = names(ck_quota))))
  names(set_counts) <- names(ck_quota)

  adjacent_pairs <- count_adjacent_pairs(selected[, c("ranges", "pass"), drop = FALSE])
  set_shortage <- sum(pmax(0, as.integer(ck_quota) - set_counts))
  mean_diag <- mean(selected$diag_distance)
  max_diag <- max(selected$diag_distance)
  off_diag <- sum(selected$diag_distance > 0.55)
  rescue <- sum(selected$diag_distance > 1.05)
  ratio_dev <- abs(actual_ratio - ck_ratio_range$mid)
  line_count <- length(selected_axes)
  active_line_counts <- line_counts[line_counts > 0]
  selected_by_line <- split(selected$diag_pos, selected$line_id)
  within_line_gap_cvs <- vapply(selected_by_line, function(pos) {
    pos <- sort(as.numeric(pos))
    if (length(pos) <= 2L) return(0)
    cv_safe(diff(pos))
  }, numeric(1))
  within_line_gap_cv <- if (length(within_line_gap_cvs) == 0) 0 else mean(within_line_gap_cvs)
  active_line_count <- length(active_line_counts)
  target_line_count <- target_active_diagonal_count(nrow(selected))
  line_count_penalty <- (active_line_count - target_line_count)^2 * 140 +
    max(0, active_line_count - target_line_count)^2 * 90 +
    max(0, line_count - target_line_count - 2L)^2 * 18
  singleton_line_penalty <- sum(active_line_counts == 1L) * 28
  row_target_max <- ceiling(nrow(selected) / nrows)
  row_concentration_penalty <- sum(pmax(0, row_counts - row_target_max)^2) * 80
  empty_row_penalty <- max(0, nrows - sum(row_counts > 0) - floor(nrows * 0.25)) * 3

  mode_penalty <- switch(mode,
    strict_diagonal = 0,
    diagonal_band = 35,
    controlled_rescue = 3000,
    5000
  )
  off_diag_weight <- switch(mode,
    strict_diagonal = 1000,
    diagonal_band = 85,
    controlled_rescue = 220,
    1000
  )

  score <- mode_penalty +
    off_diag_weight * off_diag +
    1800 * rescue +
    300 * max_diag +
    100 * mean_diag +
    80 * set_shortage +
    50 * adjacent_pairs +
    320 * cv_safe(active_line_counts) +
    260 * within_line_gap_cv +
    90 * cv_safe(row_counts) +
    15 * cv_safe(col_counts) +
    row_concentration_penalty +
    empty_row_penalty +
    line_count_penalty +
    singleton_line_penalty +
    10 * ratio_dev

  list(
    score = score,
    adjacent_pairs = adjacent_pairs,
    row_counts = row_counts,
    col_counts = col_counts,
    line_counts = line_counts,
    set_counts = set_counts,
    mean_distance_to_diagonal = mean_diag,
    max_distance_to_diagonal = max_diag,
    off_diagonal_check_count = off_diag,
    controlled_rescue_count = rescue,
    row_check_count_cv = cv_safe(row_counts),
    column_check_count_cv = cv_safe(col_counts),
    diagonal_line_count_cv = cv_safe(active_line_counts),
    within_line_gap_cv = within_line_gap_cv,
    active_diagonal_count = active_line_count,
    target_diagonal_count = target_line_count,
    singleton_diagonal_line_count = sum(active_line_counts == 1L),
    strict_diagonal_passed = off_diag == 0
  )
}

find_optimal_ck_ratio <- function(total_num_tests, total_num_checks, ncols,
                                  ck_ratio_min, ck_ratio_max, ck_ratio_mid,
                                  requested_nrows = NULL) {
  best_solution <- NULL
  best_score <- Inf

  if (!is.null(requested_nrows)) {
    rows_to_try <- requested_nrows
  } else {
    min_nrows <- max(1L, ceiling(total_num_tests / ncols))
    max_nrows <- ceiling(total_num_tests / (ncols * (1 - ck_ratio_max))) + 3L
    rows_to_try <- seq.int(min_nrows, max_nrows)
  }

  for (nrows in rows_to_try) {
    total_plots <- nrows * ncols
    total_ck_slots <- total_plots - total_num_tests
    if (total_ck_slots < total_num_checks) next
    if (total_ck_slots < 1) next
    actual_ratio <- total_ck_slots / total_plots
    if (actual_ratio < ck_ratio_min - 1e-9 || actual_ratio > ck_ratio_max + 1e-9) next

    ratio_score <- abs(actual_ratio - ck_ratio_mid)
    shape_score <- abs((nrows / ncols) - 1) * 0.01
    score <- ratio_score + shape_score
    if (score < best_score) {
      best_score <- score
      best_solution <- list(
        nrows = nrows,
        total_plots = total_plots,
        total_check_positions = total_ck_slots,
        test_plots = total_num_tests,
        actual_ratio = actual_ratio,
        diagonal_ratio = actual_ratio,
        spacing = NA_integer_,
        diagonal_interval = NA_integer_,
        diagonal_count = NA_integer_,
        is_complete = TRUE,
        selected_diags = numeric(0)
      )
    }
  }

  best_solution
}

diagonal_solve_layout <- function(ped.list, ck_ratio_range, ncols, nrows, planter) {
  if (!"set" %in% names(ped.list)) ped.list$set <- "1"
  ped.list$set <- as.character(ped.list$set)
  ped.list$design_check <- as.numeric(as.character(ped.list$design_check))

  num_checks <- sum(ped.list$design_check == 2)
  num_tests <- sum(ped.list$design_check != 2)
  unique_checks_total <- sum(vapply(split(
    ped.list$ped_id[ped.list$design_check == 2],
    ped.list$set[ped.list$design_check == 2]
  ), function(x) length(unique(x)), integer(1)))

  if (num_checks == 0) stop("No diagonal check varieties found (design_check = 2)")
  if (num_tests == 0) stop("No test varieties found")

  quick_config <- find_optimal_ck_ratio(
    num_tests, unique_checks_total, ncols,
    ck_ratio_range$min, ck_ratio_range$max, ck_ratio_range$mid,
    requested_nrows = nrows
  )

  if (is.null(quick_config)) {
    stop(sprintf(
      "Unable to find a valid plot count for ncols=%d and Level %s. Try a higher ck_ratio level or a different ncols.",
      ncols, ck_ratio_range$level
    ), call. = FALSE)
  }

  if (!is.null(nrows)) {
    rows_to_try <- nrows
  } else {
    min_nrows <- max(1L, ceiling(num_tests / ncols))
    max_nrows <- ceiling(num_tests / (ncols * (1 - ck_ratio_range$max))) + 3L
    rows_to_try <- seq.int(min_nrows, max_nrows)
  }

  best <- NULL
  best_score <- Inf
  modes <- c("strict_diagonal", "diagonal_band", "controlled_rescue")
  quota_failures <- character()

  for (mode in modes) {
    if (mode == "controlled_rescue" && !is.null(best)) next

    for (nr in rows_to_try) {
      total_plots <- nr * ncols
      total_ck_slots <- total_plots - num_tests
      if (total_ck_slots < unique_checks_total || total_ck_slots < 1) next

      actual_ratio <- total_ck_slots / total_plots
      if (actual_ratio < ck_ratio_range$min - 1e-9 ||
          actual_ratio > ck_ratio_range$max + 1e-9) next

      quota <- allocate_ck_quota_by_set(ped.list, total_plots, ck_ratio_range)
      if (!isTRUE(quota$ok)) {
        quota_failures <- c(quota_failures, sprintf("nrows=%d: %s", nr, quota$message))
        next
      }

      geometry <- build_field_geometry(nr, ncols, planter)
      set_assignments <- assign_set_blocks(total_plots, quota$set_slots, quota$sets)
      geometry$set <- set_assignments

      max_spacing <- max(2L, min(15L, min(nr, ncols)))
      max_interval <- max(2L, min(10L, min(nr, ncols)))
      target_line_count <- target_active_diagonal_count(total_ck_slots)

      for (spacing in 2:max_spacing) {
        selected_axes <- make_selected_axes(geometry, spacing, nr, ncols)
        if (length(selected_axes) == 0) next
        if (length(selected_axes) > target_line_count + 2L ||
            length(selected_axes) < max(1L, target_line_count - 2L)) next

        for (diagonal_interval in 2:max_interval) {
          for (phase_seed in 0:2) {
            candidates <- build_diagonal_candidates(
              geometry, selected_axes, diagonal_interval, phase_seed,
              set_assignments, nr, ncols
            )

            picked <- select_ck_slots(candidates, quota$ck_quota, mode, nr, ncols)
            if (!isTRUE(picked$ok)) next

            metrics <- score_selected_layout(
              picked$selected, selected_axes, quota$ck_quota,
              ck_ratio_range, actual_ratio, mode, nr, ncols
            )

            if (metrics$score < best_score) {
              best_score <- metrics$score
              best <- list(
                config = list(
                  diagonal_count = metrics$active_diagonal_count,
                  candidate_diagonal_count = length(selected_axes),
                  spacing = spacing,
                  diagonal_interval = diagonal_interval,
                  diagonal_ratio = actual_ratio,
                  actual_ratio = actual_ratio,
                  total_plots = total_plots,
                  nrows = nr,
                  total_check_positions = total_ck_slots,
                  test_plots = num_tests,
                  remainder = 0,
                  is_complete = TRUE,
                  selected_diags = selected_axes,
                  ck_ratio_range = ck_ratio_range,
                  diagonal_mode = mode,
                  phase_seed = phase_seed,
                  score = metrics$score,
                  mean_distance_to_diagonal = metrics$mean_distance_to_diagonal,
                  max_distance_to_diagonal = metrics$max_distance_to_diagonal,
                  off_diagonal_check_count = metrics$off_diagonal_check_count,
                  controlled_rescue_count = metrics$controlled_rescue_count,
                  target_diagonal_count = metrics$target_diagonal_count,
                  singleton_diagonal_line_count = metrics$singleton_diagonal_line_count,
                  strict_diagonal_passed = metrics$strict_diagonal_passed
                ),
                physical_layout = geometry,
                selected_check_positions = picked$selected$plot_id,
                selected_check_metadata = picked$selected,
                selected_diags = selected_axes,
                set_assignments = set_assignments,
                ck_quota = quota$ck_quota,
                min_ck_by_set = quota$min_ck,
                set_slots = quota$set_slots,
                metrics = metrics,
                nrows = nr,
                ncols = ncols,
                total_plots = total_plots
              )
            }
          }
        }
      }
    }
  }

  if (is.null(best)) {
    detail <- unique(quota_failures)
    if (length(detail) > 0) {
      stop(sprintf(
        "Unable to place diagonal checks for ncols=%d and Level %s: %s",
        ncols, ck_ratio_range$level, paste(detail, collapse = " ")
      ), call. = FALSE)
    } else {
      stop(sprintf(
        "Unable to place diagonal checks for ncols=%d and Level %s while preserving the diagonal skeleton.",
        ncols, ck_ratio_range$level
      ), call. = FALSE)
    }
  }

  best
}

diagonal_design_core <- function(shared_config, ped.list, randomize, seed) {
  if (!is.null(seed)) set.seed(seed)

  total_plots <- shared_config$total_plots
  physical_layout <- shared_config$physical_layout
  set_assignments <- as.character(shared_config$set_assignments)
  selected_check_positions <- as.integer(shared_config$selected_check_positions)

  ped.list$set <- as.character(ped.list$set)
  ped.list$design_check <- as.numeric(as.character(ped.list$design_check))
  all_diagonal_checks <- ped.list[ped.list$design_check == 2, , drop = FALSE]
  all_tests <- ped.list[ped.list$design_check != 2, , drop = FALSE]
  sets <- sort(unique(ped.list$set), method = "radix")

  check_assignments <- rep(NA_character_, total_plots)
  test_assignments <- rep(NA_character_, total_plots)

  for (grp in sets) {
    positions <- selected_check_positions[set_assignments[selected_check_positions] == grp]
    grp_checks <- unique(as.character(all_diagonal_checks$ped_id[all_diagonal_checks$set == grp]))
    if (length(grp_checks) == 0 && length(positions) > 0) {
      stop(sprintf("Set %s has CK slots but no diagonal check material.", grp), call. = FALSE)
    }
    if (length(positions) > 0) {
      assigned <- c(grp_checks, rep(grp_checks, length.out = max(0, length(positions) - length(grp_checks))))
      assigned <- assigned[seq_along(positions)]
      if (randomize && length(assigned) > 1) assigned <- sample(assigned)
      check_assignments[positions] <- assigned
    }
  }

  for (grp in sets) {
    grp_indices <- which(set_assignments == grp)
    available_indices <- setdiff(grp_indices, selected_check_positions)
    grp_tests <- as.character(all_tests$ped_id[all_tests$set == grp])
    if (randomize && length(grp_tests) > 1) grp_tests <- sample(grp_tests)

    if (length(available_indices) < length(grp_tests)) {
      stop(sprintf(
        "Set %s has %d test slots but needs %d test materials.",
        grp, length(available_indices), length(grp_tests)
      ), call. = FALSE)
    }

    if (length(grp_tests) > 0) {
      test_assignments[available_indices[seq_along(grp_tests)]] <- grp_tests
    }
  }

  final_assignments <- test_assignments
  ck_idx <- which(!is.na(check_assignments))
  final_assignments[ck_idx] <- check_assignments[ck_idx]

  if (any(is.na(final_assignments))) {
    empty_idx <- which(is.na(final_assignments))
    for (grp in sets) {
      grp_empty <- empty_idx[set_assignments[empty_idx] == grp]
      if (length(grp_empty) == 0) next
      grp_checks <- unique(as.character(all_diagonal_checks$ped_id[all_diagonal_checks$set == grp]))
      if (length(grp_checks) == 0) next
      filler <- rep(grp_checks, length.out = length(grp_empty))
      final_assignments[grp_empty] <- filler
      check_assignments[grp_empty] <- filler
    }
  }

  if (any(is.na(final_assignments))) {
    stop("Generated layout contains unfilled plots.", call. = FALSE)
  }

  out_design <- data.frame(
    plot_id = seq_len(total_plots),
    ped_id = final_assignments,
    hyb_type = ifelse(!is.na(check_assignments), "ck", "hyb"),
    ranges = physical_layout$ranges,
    pass = physical_layout$pass,
    set = set_assignments,
    stringsAsFactors = FALSE
  )

  dc_map <- setNames(as.character(ped.list$design_check), as.character(ped.list$ped_id))
  out_design$design_check <- dc_map[out_design$ped_id]
  out_design$design_check[is.na(out_design$design_check)] <- "9"

  for (grp in sets) {
    required <- length(unique(all_diagonal_checks$ped_id[all_diagonal_checks$set == grp]))
    actual <- length(unique(out_design$ped_id[out_design$set == grp & out_design$hyb_type == "ck"]))
    if (actual < required) {
      stop(sprintf(
        "Set %s needs %d unique diagonal checks, but only %d were assigned.",
        grp, required, actual
      ), call. = FALSE)
    }
  }

  list(out_design = out_design)
}

validate_diagonal_design <- function(data, ck_ratio, ncols, nrows = NULL) {
  if (!ck_ratio %in% c("A", "B", "C")) {
    return(list(valid = FALSE, message = "ck_ratio must be 'A', 'B', or 'C'", suggestions = NULL))
  }
  if (!is.numeric(ncols) || ncols < 4) {
    return(list(valid = FALSE, message = "ncols must be an integer >= 4", suggestions = NULL))
  }

  data$design_check <- as.numeric(as.character(data$design_check))
  if (!"set" %in% names(data)) data$set <- "1"
  data$set <- as.character(data$set)

  num_checks <- sum(data$design_check == 2)
  num_tests <- sum(data$design_check != 2)
  if (num_checks == 0) {
    return(list(valid = FALSE, message = "No diagonal check varieties found (design_check = 2)", suggestions = NULL))
  }
  if (num_tests == 0) {
    return(list(valid = FALSE, message = "No test varieties found", suggestions = NULL))
  }

  ck_ratio_range <- get_ck_ratio_range(ck_ratio)
  estimated_total_plots <- ceiling(num_tests / (1 - ck_ratio_range$mid))
  estimated_nrows <- ceiling(estimated_total_plots / ncols)
  aspect_ratio <- estimated_nrows / ncols
  if (aspect_ratio < 0.1 || aspect_ratio > 10.0) {
    return(list(
      valid = FALSE,
      message = sprintf(
        "Field shape is extreme for the requested ncols (estimated %d rows x %d columns, aspect %.2f).",
        estimated_nrows, ncols, aspect_ratio
      ),
      suggestions = NULL,
      failure_type = "aspect_ratio"
    ))
  }

  unique_checks_total <- sum(vapply(split(
    data$ped_id[data$design_check == 2],
    data$set[data$design_check == 2]
  ), function(x) length(unique(x)), integer(1)))

  config <- find_optimal_ck_ratio(
    num_tests, unique_checks_total, ncols,
    ck_ratio_range$min, ck_ratio_range$max, ck_ratio_range$mid,
    requested_nrows = nrows
  )

  if (is.null(config)) {
    return(list(
      valid = FALSE,
      message = sprintf(
        "No valid plot count found for fixed ncols=%d and Level %s.",
        ncols, ck_ratio
      ),
      suggestions = NULL,
      failure_type = "no_config"
    ))
  }

  list(
    valid = TRUE,
    message = "configuration validation passed",
    config = config,
    estimated_nrows = config$nrows,
    estimated_total_plots = config$total_plots,
    estimated_ck_ratio = config$actual_ratio
  )
}

# Standalone RCBD core bundled with the mini RCBD skill.
# Keep this file in scripts/ so the runner does not depend on external R scripts.

rcbd_design_core <- function(ped.list, blocks, nrows, ncols, seed,
                             planter = "serpentine",
                             ck_constrain = FALSE, hyb_constrain = FALSE,
                             plot_id_start = 1, block_start = 1) {

  calculate_pass <- function(pwb, physical_row, total_cols, planter_type) {
    if (planter_type == "serpentine" && physical_row %% 2 == 0) {
      return(total_cols + 1 - pwb)
    }
    return(pwb)
  }

  if (!planter %in% c("serpentine", "cartesian")) stop("Invalid planter")

  ped_list_group <- ped.list
  num_ped <- nrow(ped_list_group)
  check_varieties <- ped_list_group[ped_list_group$design_check != "0", ]
  non_check_varieties <- ped_list_group[ped_list_group$design_check == "0", ]
  num_checks <- nrow(check_varieties)
  design_group <- data.frame()

  constraint_env <- new.env()
  constraint_env$used_physical_cols_ck <- list()
  constraint_env$used_physical_cols_hyb <- list()

  if (num_checks > 0) {
    for (id in check_varieties$ped_id) constraint_env$used_physical_cols_ck[[id]] <- integer(0)
  }
  if (nrow(non_check_varieties) > 0) {
    for (id in non_check_varieties$ped_id) constraint_env$used_physical_cols_hyb[[id]] <- integer(0)
  }


  for (block in 1:blocks) {
    set.seed(seed + block)
    start_plot_for_block <- plot_id_start + (block - 1) * num_ped
    current_physical_row <- floor((start_plot_for_block - 1) / ncols) + 1
    check_map_this_block <- data.frame()

    if (num_checks > 0 && ck_constrain) {
      available_pwb_base <- 2:(num_ped - 1)
      if (length(available_pwb_base) < num_checks) {
        stop(sprintf("Block %d: Not enough space for constrained checks", block))
      }

      max_tries <- 2000
      solution_found <- FALSE
      for (try in 1:max_tries) {
        shuffled_positions <- sample(available_pwb_base)
        chosen_positions <- integer(num_checks)
        if (num_checks > 0) {
          chosen_positions[1] <- shuffled_positions[1]
          if (num_checks > 1) {
            temp_available <- shuffled_positions
            for (i in 2:num_checks) {
              current_chosen <- chosen_positions[1:(i - 1)]
              valid_next_pos_pool <- setdiff(temp_available, c(current_chosen, current_chosen - 1, current_chosen + 1))
              if (length(valid_next_pos_pool) == 0) {
                chosen_positions[i] <- NA
                break
              }
              next_pos <- sample(valid_next_pos_pool, 1)
              chosen_positions[i] <- next_pos
              temp_available <- setdiff(temp_available, next_pos)
            }
          }
        }
        if (any(is.na(chosen_positions)) || any(chosen_positions == 0)) next

        shuffled_check_ids <- sample(check_varieties$ped_id)
        passes_for_this_layout <- c()
        is_physically_valid <- TRUE
        for (i in 1:num_checks) {
          ck_id <- shuffled_check_ids[i]
          pwb <- chosen_positions[i]
          pass <- calculate_pass(pwb, current_physical_row, ncols, planter)
          if (pass %in% constraint_env$used_physical_cols_ck[[ck_id]] || pass %in% passes_for_this_layout) {
            is_physically_valid <- FALSE
            break
          }
          passes_for_this_layout <- c(passes_for_this_layout, pass)
        }
        if (is_physically_valid) {
          check_map_this_block <- data.frame(ped_id = shuffled_check_ids, position = chosen_positions)
          solution_found <- TRUE
          break
        }
      }
      if (!solution_found) stop(sprintf("Block %d: Check constraint solution not found", block))

      for (i in 1:nrow(check_map_this_block)) {
        ck_id <- as.character(check_map_this_block$ped_id[i])
        pwb <- check_map_this_block$position[i]
        pass <- calculate_pass(pwb, current_physical_row, ncols, planter)
        constraint_env$used_physical_cols_ck[[ck_id]] <- c(constraint_env$used_physical_cols_ck[[ck_id]], pass)
      }
    } else if (num_checks > 0) {
      check_map_this_block <- data.frame(ped_id = sample(check_varieties$ped_id),
                                          position = sample(1:num_ped, num_checks))
    }

    all_positions <- 1:num_ped
    check_pos <- if (nrow(check_map_this_block) > 0) check_map_this_block$position else integer(0)
    test_positions <- setdiff(all_positions, check_pos)
    test_map_this_block <- data.frame()


    if (nrow(non_check_varieties) > 0) {
      if (hyb_constrain) {
        max_tries <- 100
        success <- FALSE
        for (try in 1:max_tries) {
          shuffled_hybrids <- sample(non_check_varieties$ped_id)
          possible <- TRUE
          temp_used_passes <- list()
          for (k in seq_along(shuffled_hybrids)) {
            hyb_id <- shuffled_hybrids[k]
            pwb <- test_positions[k]
            pass <- calculate_pass(pwb, current_physical_row, ncols, planter)
            if (pass %in% constraint_env$used_physical_cols_hyb[[hyb_id]] ||
                (hyb_id %in% names(temp_used_passes) && pass %in% temp_used_passes[[hyb_id]])) {
              possible <- FALSE
              break
            }
            temp_used_passes[[hyb_id]] <- c(temp_used_passes[[hyb_id]], pass)
          }
          if (possible) {
            test_map_this_block <- data.frame(ped_id = shuffled_hybrids, position = test_positions)
            success <- TRUE
            break
          }
        }
        if (!success) stop(sprintf("Block %d: Hybrid constraint solution not found", block))
        for (k in 1:nrow(test_map_this_block)) {
          hyb_id <- test_map_this_block$ped_id[k]
          pwb <- test_map_this_block$position[k]
          pass <- calculate_pass(pwb, current_physical_row, ncols, planter)
          constraint_env$used_physical_cols_hyb[[hyb_id]] <- c(constraint_env$used_physical_cols_hyb[[hyb_id]], pass)
        }
      } else {
        test_map_this_block <- data.frame(ped_id = sample(non_check_varieties$ped_id), position = test_positions)
      }
    }

    final_map <- rbind(check_map_this_block, test_map_this_block)
    start_plot_id <- plot_id_start + (block - 1) * num_ped
    block_data <- data.frame(
      plot_id = start_plot_id:(start_plot_id + num_ped - 1),
      block = block,
      plot_within_block = 1:num_ped,
      ped_id = NA_character_,
      hyb_type = NA_character_,
      design_check = NA_character_,
      exp_group = ped_list_group$exp_group[1],
      stringsAsFactors = FALSE
    )
    lookup_dc <- setNames(as.character(ped_list_group$design_check), as.character(ped_list_group$ped_id))

    for (k in 1:nrow(final_map)) {
      pos <- final_map$position[k]
      id <- as.character(final_map$ped_id[k])
      if (!is.na(pos)) {
        block_data$ped_id[pos] <- id
        block_data$design_check[pos] <- lookup_dc[id]
        block_data$hyb_type[pos] <- if (lookup_dc[id] == "0") "hyb" else "ck"
      }
    }
    design_group <- rbind(design_group, block_data)
  }

  design_group <- design_group[order(design_group$plot_id), ]
  n_plots <- nrow(design_group)
  actual_nrows <- ceiling(n_plots / ncols)
  actual_row_num <- rep(1:actual_nrows, each = ncols)[1:n_plots]
  actual_col_num <- integer(n_plots)

  if (n_plots > 0) {
    for (i in 1:actual_nrows) {
      start_index <- (i - 1) * ncols + 1
      end_index <- min(i * ncols, n_plots)
      if (end_index < start_index) next
      plots_in_row <- end_index - start_index + 1
      if (planter == "serpentine" && i %% 2 == 0) {
        actual_col_num[start_index:end_index] <- ncols:(ncols - plots_in_row + 1)
      } else {
        actual_col_num[start_index:end_index] <- 1:plots_in_row
      }
    }
  }
  design_group$ranges <- actual_row_num
  design_group$pass <- actual_col_num

  parameters <- list(blocks = blocks, nrows = actual_nrows, ncols = ncols, seed = seed, planter = planter)
  return(list(out_design = design_group, design = "rcbd", parameters = parameters))
}

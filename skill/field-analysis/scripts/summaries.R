is_check_row <- function(x) {
  !is_blank(x)
}

core_check_data <- function(df) {
  check_df <- df[is_check_row(df$check_type), , drop = FALSE]
  core <- check_df[as.character(check_df$check_type) == "1", , drop = FALSE]
  if (nrow(core) == 0) core <- check_df
  core
}

make_trait_summary <- function(df) {
  rows <- lapply(split(df, df$trait, drop = TRUE), function(part) {
    core <- core_check_data(part)
    data.frame(
      trait = first_non_blank(part$trait),
      direction = trait_direction(df, first_non_blank(part$trait)),
      observations = nrow(part),
      material_count = length(unique(part$ped_id)),
      location_count = length(unique(part$loc_id)),
      rep_count = length(unique(part$rep_num)),
      mean = safe_mean(part$value),
      stddev = safe_sd(part$value),
      cv = safe_cv(part$value),
      min = safe_min(part$value),
      max = safe_max(part$value),
      check_mean = safe_mean(core$value),
      quality = quality_from_cv(safe_cv(part$value)),
      stringsAsFactors = FALSE
    )
  })
  do.call(rbind, rows)
}

location_check_means <- function(part) {
  core <- core_check_data(part)
  if (nrow(core) == 0) {
    out <- aggregate(value ~ loc_id, part, safe_mean)
  } else {
    out <- aggregate(value ~ loc_id, core, safe_mean)
  }
  names(out)[names(out) == "value"] <- "check_mean"
  out
}

make_material_summary <- function(df) {
  out <- NULL
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    direction <- trait_direction(df, trait)
    check_mean <- safe_mean(core_check_data(part)$value)
    trial_mean <- safe_mean(part$value)
    loc_ped <- aggregate(value ~ loc_id + ped_id, part, safe_mean)
    names(loc_ped)[names(loc_ped) == "value"] <- "loc_material_mean"
    loc_check <- location_check_means(part)
    loc_cmp <- merge(loc_ped, loc_check, by = "loc_id", all.x = TRUE)
    loc_cmp$above_check <- as.numeric(!is.na(loc_cmp$check_mean) & loc_cmp$loc_material_mean > loc_cmp$check_mean)
    above <- aggregate(above_check ~ ped_id, loc_cmp, sum, na.rm = TRUE)
    names(above)[names(above) == "above_check"] <- "locations_above_check"

    rows <- lapply(split(part, part$ped_id, drop = TRUE), function(ped_part) {
      ped <- first_non_blank(ped_part$ped_id)
      data.frame(
        trait = trait,
        ped_id = ped,
        entry_id = first_non_blank(ped_part$entry_id),
        mean = safe_mean(ped_part$value),
        stddev = safe_sd(ped_part$value),
        min = safe_min(ped_part$value),
        max = safe_max(ped_part$value),
        rep_count = sum(!is.na(ped_part$value)),
        location_count = length(unique(ped_part$loc_id)),
        pct_check_mean = if (is.na(check_mean) || check_mean == 0) NA_real_ else safe_mean(ped_part$value) / check_mean * 100,
        pct_trial_mean = if (is.na(trial_mean) || trial_mean == 0) NA_real_ else safe_mean(ped_part$value) / trial_mean * 100,
        stringsAsFactors = FALSE
      )
    })
    trait_out <- do.call(rbind, rows)
    trait_out <- merge(trait_out, above, by = "ped_id", all.x = TRUE)
    trait_out$pct_locations_above_check <- ifelse(trait_out$location_count > 0, trait_out$locations_above_check / trait_out$location_count * 100, NA_real_)
    trait_out$rank <- rank_values(trait_out$mean, direction)
    trait_out <- trait_out[order(trait_out$rank, trait_out$ped_id), , drop = FALSE]
    out <- rbind(out, trait_out)
  }
  row.names(out) <- NULL
  out
}

make_location_summary <- function(df) {
  out <- NULL
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    check <- location_check_means(part)
    rows <- lapply(split(part, part$loc_id, drop = TRUE), function(loc_part) {
      data.frame(
        trait = trait,
        loc_id = first_non_blank(loc_part$loc_id),
        observations = nrow(loc_part),
        material_count = length(unique(loc_part$ped_id)),
        mean = safe_mean(loc_part$value),
        stddev = safe_sd(loc_part$value),
        cv = safe_cv(loc_part$value),
        min = safe_min(loc_part$value),
        max = safe_max(loc_part$value),
        stringsAsFactors = FALSE
      )
    })
    trait_out <- do.call(rbind, rows)
    trait_out <- merge(trait_out, check, by = "loc_id", all.x = TRUE)
    out <- rbind(out, trait_out)
  }
  row.names(out) <- NULL
  out
}

make_material_location_summary <- function(df) {
  out <- NULL
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    direction <- trait_direction(df, trait)
    loc_mean <- aggregate(value ~ loc_id, part, safe_mean)
    names(loc_mean)[names(loc_mean) == "value"] <- "location_mean"
    loc_check <- location_check_means(part)
    rows <- lapply(split(part, interaction(part$loc_id, part$ped_id, drop = TRUE), drop = TRUE), function(x) {
      data.frame(
        trait = trait,
        loc_id = first_non_blank(x$loc_id),
        ped_id = first_non_blank(x$ped_id),
        entry_id = first_non_blank(x$entry_id),
        mean = safe_mean(x$value),
        rep_count = sum(!is.na(x$value)),
        stringsAsFactors = FALSE
      )
    })
    trait_out <- do.call(rbind, rows)
    trait_out <- merge(trait_out, loc_mean, by = "loc_id", all.x = TRUE)
    trait_out <- merge(trait_out, loc_check, by = "loc_id", all.x = TRUE)
    trait_out$pct_check_mean <- ifelse(is.na(trait_out$check_mean) | trait_out$check_mean == 0, NA_real_, trait_out$mean / trait_out$check_mean * 100)
    trait_out$pct_location_mean <- ifelse(is.na(trait_out$location_mean) | trait_out$location_mean == 0, NA_real_, trait_out$mean / trait_out$location_mean * 100)
    trait_out$rank <- NA_real_
    for (loc in unique(trait_out$loc_id)) {
      idx <- which(trait_out$loc_id == loc)
      trait_out$rank[idx] <- rank_values(trait_out$mean[idx], direction)
    }
    out <- rbind(out, trait_out[order(trait_out$loc_id, trait_out$rank), , drop = FALSE])
  }
  row.names(out) <- NULL
  out
}

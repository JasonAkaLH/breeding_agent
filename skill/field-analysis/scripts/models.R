choose_anova_formula <- function(df) {
  nloc <- length(unique(df$loc_id))
  n_loc_rep <- nrow(unique(df[, c("loc_id", "rep_num"), drop = FALSE]))
  reps_per_loc <- n_loc_rep / max(nloc, 1)
  if (nloc <= 1 && reps_per_loc > 1) return(value ~ ped_id + rep_num)
  if (nloc <= 1) return(value ~ ped_id)
  if (reps_per_loc > 1) return(value ~ loc_id / rep_num + ped_id + loc_id:ped_id)
  value ~ loc_id + ped_id + loc_id:ped_id
}

fit_anova_model <- function(df) {
  model_df <- df[complete.cases(df[, c("value", "ped_id", "loc_id", "rep_num"), drop = FALSE]), , drop = FALSE]
  if (nrow(model_df) < 3) stop("Not enough complete observations for ANOVA.")
  model_df$ped_id <- as.factor(model_df$ped_id)
  model_df$loc_id <- as.factor(model_df$loc_id)
  model_df$rep_num <- as.factor(model_df$rep_num)
  form <- choose_anova_formula(model_df)
  fit <- lm(form, data = model_df, na.action = na.omit)
  list(fit = fit, formula = paste(deparse(form), collapse = " "))
}

run_anova <- function(df) {
  by_trait <- list()
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    by_trait[[trait]] <- tryCatch({
      fitted <- fit_anova_model(part)
      a <- as.data.frame(anova(fitted$fit))
      a$term <- row.names(a)
      row.names(a) <- NULL
      names(a) <- sub("Df", "df", names(a), fixed = TRUE)
      names(a) <- sub("Sum Sq", "sum_sq", names(a), fixed = TRUE)
      names(a) <- sub("Mean Sq", "mean_sq", names(a), fixed = TRUE)
      names(a) <- sub("F value", "f_value", names(a), fixed = TRUE)
      names(a) <- sub("Pr(>F)", "p_value", names(a), fixed = TRUE)
      if (!"f_value" %in% names(a)) a$f_value <- NA_real_
      if (!"p_value" %in% names(a)) a$p_value <- NA_real_
      a$significance <- vapply(a$p_value, significance_label, character(1))
      a <- a[, c("term", "df", "sum_sq", "mean_sq", "f_value", "p_value", "significance")]
      list(
        status = "completed",
        model = fitted$formula,
        term_fields = compact_dataset(a)$fields,
        terms = compact_dataset(a)$records
      )
    }, error = function(e) {
      list(status = "failed", reason = e$message)
    })
  }
  list(by_trait = by_trait)
}

lsd_threshold <- function(mse, df_error, n1, n2, alpha) {
  if (is.na(mse) || is.na(df_error) || df_error <= 0 || is.na(n1) || is.na(n2) || n1 <= 0 || n2 <= 0) return(NA_real_)
  qt(1 - alpha / 2, df_error) * sqrt(mse * (1 / n1 + 1 / n2))
}

lsd_letter <- function(index) {
  pool <- c(letters, paste0("a", letters), paste0("b", letters), paste0("c", letters))
  if (index <= length(pool)) return(pool[[index]])
  paste0("g", index)
}

make_lsd_groups <- function(means, mse, df_error, alpha) {
  if (nrow(means) == 0) return(character(0))
  if (nrow(means) == 1) return("a")
  if (is.na(mse) || is.na(df_error) || df_error <= 0) return(rep(NA_character_, nrow(means)))
  groups <- list()
  labels <- rep("", nrow(means))
  for (i in seq_len(nrow(means))) {
    assigned <- FALSE
    if (length(groups) > 0) {
      for (g in seq_along(groups)) {
        compatible <- TRUE
        for (member in groups[[g]]) {
          threshold <- lsd_threshold(mse, df_error, means$n[i], means$n[member], alpha)
          if (is.na(threshold) || abs(means$lsmean[i] - means$lsmean[member]) > threshold) {
            compatible <- FALSE
            break
          }
        }
        if (compatible) {
          groups[[g]] <- c(groups[[g]], i)
          labels[i] <- paste0(labels[i], lsd_letter(g))
          assigned <- TRUE
        }
      }
    }
    if (!assigned) {
      groups[[length(groups) + 1]] <- i
      labels[i] <- lsd_letter(length(groups))
    }
  }
  labels
}

run_lsd_grouping <- function(df) {
  by_trait <- list()
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    by_trait[[trait]] <- tryCatch({
      fitted <- fit_anova_model(part)
      a <- anova(fitted$fit)
      mse <- tail(a$`Mean Sq`, 1)
      df_error <- tail(a$Df, 1)
      means <- aggregate(value ~ ped_id, part, safe_mean)
      names(means)[names(means) == "value"] <- "lsmean"
      counts <- aggregate(value ~ ped_id, part, function(x) sum(!is.na(x)))
      names(counts)[names(counts) == "value"] <- "n"
      means <- merge(means, counts, by = "ped_id", all.x = TRUE)
      means <- means[order(-means$lsmean, means$ped_id), , drop = FALSE]
      means$group_0_05 <- make_lsd_groups(means, mse, df_error, 0.05)
      means$group_0_01 <- make_lsd_groups(means, mse, df_error, 0.01)
      means$lsd_value_0_05 <- lsd_threshold(mse, df_error, safe_mean(means$n), safe_mean(means$n), 0.05)
      means$lsd_value_0_01 <- lsd_threshold(mse, df_error, safe_mean(means$n), safe_mean(means$n), 0.01)
      dataset <- compact_dataset(means, c("ped_id", "lsmean", "group_0_05", "group_0_01", "lsd_value_0_05", "lsd_value_0_01"))
      list(status = "completed", method = "local_lsd_grouping_from_lm", grouping_fields = dataset$fields, grouping = dataset$records)
    }, error = function(e) {
      list(status = "failed", reason = e$message)
    })
  }
  list(by_trait = by_trait)
}

has_spatial_coordinates <- function(df) {
  sum(!is.na(df$ranges) & !is.na(df$pass)) >= 5 &&
    length(unique(df$ranges[!is.na(df$ranges)])) >= 2 &&
    length(unique(df$pass[!is.na(df$pass)])) >= 2
}

run_spatial_adjustment <- function(df) {
  by_trait <- list()
  adjusted_by_trait <- list()
  adjusted_fields <- NULL
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    if (!has_spatial_coordinates(part)) {
      by_trait[[trait]] <- list(status = "not_applicable", reason = "Insufficient ranges/pass coverage.")
      next
    }
    res <- tryCatch({
      model_df <- part
      model_df$ranges_factor <- as.factor(model_df$ranges)
      model_df$pass_factor <- as.factor(model_df$pass)
      fit <- lm(value ~ ranges_factor + pass_factor, data = model_df, na.action = na.omit)
      model_df$adjusted_value <- residuals(fit) + mean(model_df$value, na.rm = TRUE)
      summ <- aggregate(adjusted_value ~ ped_id + entry_id, model_df, safe_mean)
      names(summ)[names(summ) == "adjusted_value"] <- "adjusted_mean"
      direction <- trait_direction(df, trait)
      summ$adjusted_rank <- rank_values(summ$adjusted_mean, direction)
      summ <- summ[order(summ$adjusted_rank, summ$ped_id), , drop = FALSE]
      dataset <- compact_dataset(summ, c("ped_id", "entry_id", "adjusted_mean", "adjusted_rank"))
      adjusted_fields <<- dataset$fields
      adjusted_by_trait[[trait]] <<- dataset$records
      list(status = "completed", method = "additive_range_pass_lm", r_squared = summary(fit)$r.squared)
    }, error = function(e) {
      list(status = "failed", reason = e$message)
    })
    by_trait[[trait]] <- res
  }
  out <- list(by_trait = by_trait)
  if (length(adjusted_by_trait) > 0) {
    out$adjusted_material_fields <- adjusted_fields
    out$adjusted_materials_by_trait <- adjusted_by_trait
  }
  out
}

run_stability <- function(df) {
  if (length(unique(df$loc_id)) < 2) {
    return(list(status = "not_applicable", reason = "Stability analysis requires at least two locations."))
  }
  by_trait <- list()
  fields <- NULL
  for (trait in unique(df$trait)) {
    part <- df[df$trait == trait, , drop = FALSE]
    loc_ped <- aggregate(value ~ loc_id + ped_id, part, safe_mean)
    names(loc_ped)[names(loc_ped) == "value"] <- "location_mean"
    loc_count <- aggregate(loc_id ~ ped_id, loc_ped, function(x) length(unique(x)))
    names(loc_count)[names(loc_count) == "loc_id"] <- "location_count"
    mean_by_ped <- aggregate(location_mean ~ ped_id, loc_ped, safe_mean)
    names(mean_by_ped)[names(mean_by_ped) == "location_mean"] <- "mean_across_locations"
    sd_by_ped <- aggregate(location_mean ~ ped_id, loc_ped, safe_sd)
    names(sd_by_ped)[names(sd_by_ped) == "location_mean"] <- "sd_across_locations"
    out <- merge(mean_by_ped, sd_by_ped, by = "ped_id", all = TRUE)
    out <- merge(out, loc_count, by = "ped_id", all = TRUE)
    out$cv_across_locations <- out$sd_across_locations / out$mean_across_locations * 100
    direction <- trait_direction(df, trait)
    out$performance_rank <- rank_values(out$mean_across_locations, direction)
    out$stability_rank <- rank_values(-out$cv_across_locations, "higher_is_better")
    out <- out[order(out$performance_rank, out$ped_id), , drop = FALSE]
    dataset <- compact_dataset(out, c("ped_id", "mean_across_locations", "sd_across_locations", "cv_across_locations", "location_count", "performance_rank", "stability_rank"))
    fields <- dataset$fields
    by_trait[[trait]] <- dataset$records
  }
  list(status = "completed", stability_fields = fields, by_trait = by_trait)
}

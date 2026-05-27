is_blank <- function(x) {
  is.na(x) | trimws(as.character(x)) == ""
}

first_non_blank <- function(x) {
  x <- x[!is_blank(x)]
  if (length(x) == 0) return(NA_character_)
  as.character(x[[1]])
}

safe_numeric <- function(x) {
  suppressWarnings(as.numeric(x))
}

safe_mean <- function(x) {
  x <- safe_numeric(x)
  if (all(is.na(x))) return(NA_real_)
  mean(x, na.rm = TRUE)
}

safe_sd <- function(x) {
  x <- safe_numeric(x)
  if (sum(!is.na(x)) < 2) return(NA_real_)
  sd(x, na.rm = TRUE)
}

safe_min <- function(x) {
  x <- safe_numeric(x)
  if (all(is.na(x))) return(NA_real_)
  min(x, na.rm = TRUE)
}

safe_max <- function(x) {
  x <- safe_numeric(x)
  if (all(is.na(x))) return(NA_real_)
  max(x, na.rm = TRUE)
}

safe_cv <- function(x) {
  m <- safe_mean(x)
  s <- safe_sd(x)
  if (is.na(m) || m == 0 || is.na(s)) return(NA_real_)
  s / m * 100
}

round_numeric <- function(x, digits = 4) {
  if (is.numeric(x)) return(round(x, digits))
  x
}

rank_values <- function(x, direction) {
  x <- safe_numeric(x)
  if (all(is.na(x))) return(rep(NA_integer_, length(x)))
  if (direction == "lower_is_better") {
    return(rank(x, ties.method = "min", na.last = "keep"))
  }
  rank(-x, ties.method = "min", na.last = "keep")
}

significance_label <- function(p) {
  if (is.na(p)) return(NA_character_)
  if (p < 0.001) return("***")
  if (p < 0.01) return("**")
  if (p < 0.05) return("*")
  if (p < 0.1) return(".")
  "ns"
}

quality_from_cv <- function(cv) {
  if (is.na(cv)) return("unknown")
  if (cv <= 10) return("good")
  if (cv <= 20) return("moderate")
  "high_variation"
}

all_empty <- function(x) {
  if (length(x) == 0) return(TRUE)
  if (is.list(x)) return(all(vapply(x, all_empty, logical(1))))
  all(is.na(x) | trimws(as.character(x)) == "")
}

compact_dataset <- function(df, fields = names(df), digits = 4, drop_empty_cols = TRUE) {
  if (is.null(df) || nrow(df) == 0) return(NULL)
  df <- as.data.frame(df, stringsAsFactors = FALSE)
  for (field in setdiff(fields, names(df))) df[[field]] <- NA
  df <- df[, fields, drop = FALSE]
  if (drop_empty_cols) {
    keep <- !vapply(df, all_empty, logical(1))
    df <- df[, keep, drop = FALSE]
  }
  fields <- names(df)
  for (field in fields) {
    if (is.numeric(df[[field]])) df[[field]] <- round(df[[field]], digits)
  }
  records <- lapply(seq_len(nrow(df)), function(i) unname(as.list(df[i, , drop = FALSE])))
  list(fields = fields, records = records)
}

compact_by_trait <- function(df, trait_col = "trait", fields = setdiff(names(df), trait_col), digits = 4) {
  if (is.null(df) || nrow(df) == 0 || !trait_col %in% names(df)) return(NULL)
  field_dataset <- compact_dataset(df, fields = fields, digits = digits, drop_empty_cols = TRUE)
  kept_fields <- field_dataset$fields
  out <- list()
  for (trait in unique(as.character(df[[trait_col]]))) {
    part <- df[as.character(df[[trait_col]]) == trait, , drop = FALSE]
    dataset <- compact_dataset(part, fields = kept_fields, digits = digits, drop_empty_cols = FALSE)
    if (!is.null(dataset)) out[[trait]] <- dataset$records
  }
  list(fields = kept_fields, by_trait = out)
}

drop_empty_nodes <- function(x) {
  if (is.list(x)) {
    unnamed <- is.null(names(x)) || all(names(x) == "")
    if (unnamed) {
      return(lapply(x, drop_empty_nodes))
    }
    x <- lapply(x, drop_empty_nodes)
    x <- x[!vapply(x, function(v) {
      is.null(v) || (is.list(v) && length(v) == 0) || (is.atomic(v) && length(v) == 0)
    }, logical(1))]
    return(x)
  }
  x
}

trait_direction <- function(df, trait) {
  if ("value_trend" %in% names(df)) {
    trend <- df$value_trend[df$trait == trait & !is.na(df$value_trend)]
    if (length(trend) > 0) {
      if (trend[[1]] < 0) return("lower_is_better")
      return("higher_is_better")
    }
  }
  if (trait == "T0166") return("lower_is_better")
  "higher_is_better"
}

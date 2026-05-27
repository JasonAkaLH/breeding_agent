read_analysis_input <- function(path) {
  ext <- tolower(tools::file_ext(path))
  if (ext == "csv") {
    return(read.csv(path, colClasses = "character", na.strings = "", check.names = FALSE, stringsAsFactors = FALSE))
  }
  if (ext == "json") {
    if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Package 'jsonlite' is required to read JSON input.")
    return(as.data.frame(jsonlite::fromJSON(path), stringsAsFactors = FALSE))
  }
  stop("Input must be a CSV or JSON file.")
}

required_input_fields <- function() {
  c("loc_id", "rep_num", "entry_id", "ped_id", "trait", "value", "check_type", "ranges", "pass")
}

validate_input <- function(df) {
  missing <- setdiff(required_input_fields(), names(df))
  if (length(missing) > 0) {
    stop(sprintf("Input file is missing required columns: %s", paste(missing, collapse = ", ")))
  }
  invisible(TRUE)
}

normalize_input <- function(df) {
  validate_input(df)
  id_cols <- c("loc_id", "rep_num", "entry_id", "ped_id", "trait", "check_type")
  for (col in intersect(id_cols, names(df))) df[[col]] <- as.character(df[[col]])
  df$value <- safe_numeric(df$value)
  df$ranges <- safe_numeric(df$ranges)
  df$pass <- safe_numeric(df$pass)
  if ("value_trend" %in% names(df)) df$value_trend <- safe_numeric(df$value_trend)
  df <- df[!is_blank(df$loc_id) & !is_blank(df$ped_id) & !is_blank(df$trait) & !is.na(df$value), , drop = FALSE]
  row.names(df) <- NULL
  df
}

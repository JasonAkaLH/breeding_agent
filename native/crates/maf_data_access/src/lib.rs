//! Readonly data-access policy and result-shaping kernel.

use thiserror::Error;

pub const DEFAULT_ROW_LIMIT: usize = 500;
pub const DEFAULT_COLUMN_LIMIT: usize = 100;
pub const DEFAULT_RESULT_BYTES_LIMIT: usize = 10 * 1024 * 1024;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct DataAccessError {
    pub code: String,
    pub message: String,
}

impl DataAccessError {
    fn write_denied() -> Self {
        Self {
            code: "data_access_write_denied".to_owned(),
            message: "SQL is not readonly".to_owned(),
        }
    }

    fn limit_exceeded(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            message: "data access result exceeds configured limit".to_owned(),
        }
    }
}

pub fn ensure_readonly_sql(sql: &str) -> Result<(), DataAccessError> {
    let normalized = sql.trim().trim_end_matches(';').to_ascii_lowercase();
    let first = normalized.split_whitespace().next().unwrap_or("");
    if first != "select" && first != "with" {
        return Err(DataAccessError::write_denied());
    }
    let padded = format!(" {normalized} ");
    for forbidden in [
        " insert ",
        " update ",
        " delete ",
        " drop ",
        " alter ",
        " truncate ",
        " create ",
        " grant ",
        " revoke ",
    ] {
        if padded.contains(forbidden) {
            return Err(DataAccessError::write_denied());
        }
    }
    Ok(())
}

pub fn validate_shape(
    row_count: usize,
    column_count: usize,
    result_bytes: usize,
) -> Result<(), DataAccessError> {
    if row_count > DEFAULT_ROW_LIMIT {
        return Err(DataAccessError::limit_exceeded(
            "data_access_row_limit_exceeded",
        ));
    }
    if column_count > DEFAULT_COLUMN_LIMIT {
        return Err(DataAccessError::limit_exceeded(
            "data_access_column_limit_exceeded",
        ));
    }
    if result_bytes > DEFAULT_RESULT_BYTES_LIMIT {
        return Err(DataAccessError::limit_exceeded(
            "data_access_result_too_large",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readonly_sql_denies_writes() {
        assert!(ensure_readonly_sql("select * from varieties").is_ok());
        assert_eq!(
            ensure_readonly_sql("delete from users").unwrap_err().code,
            "data_access_write_denied"
        );
    }

    #[test]
    fn result_shape_limits_are_enforced() {
        assert!(validate_shape(500, 100, 1024).is_ok());
        assert_eq!(
            validate_shape(501, 1, 1).unwrap_err().code,
            "data_access_row_limit_exceeded"
        );
        assert_eq!(
            validate_shape(1, 101, 1).unwrap_err().code,
            "data_access_column_limit_exceeded"
        );
    }
}

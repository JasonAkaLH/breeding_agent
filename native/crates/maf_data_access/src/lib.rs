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
    ensure_readonly_sql_tokens(&sql_policy_tokens(sql, true), true)?;
    let compact_tokens = sql_policy_tokens(sql, false);
    ensure_readonly_sql_tokens(&compact_tokens, false)?;
    Ok(())
}

fn ensure_readonly_sql_tokens(
    tokens: &[String],
    require_readonly_statement: bool,
) -> Result<(), DataAccessError> {
    if require_readonly_statement && (tokens.is_empty() || tokens.iter().any(|token| token == ";"))
    {
        return Err(DataAccessError::write_denied());
    }
    let first = tokens.first().map(String::as_str).unwrap_or("");
    if require_readonly_statement && first != "select" && first != "with" {
        return Err(DataAccessError::write_denied());
    }

    for token in tokens {
        if matches!(
            token.as_str(),
            "insert"
                | "update"
                | "delete"
                | "drop"
                | "alter"
                | "truncate"
                | "create"
                | "replace"
                | "grant"
                | "revoke"
                | "merge"
                | "call"
                | "__mysql_executable_comment__"
        ) {
            return Err(DataAccessError::write_denied());
        }
    }
    for pair in tokens.windows(2) {
        if matches!(
            (pair[0].as_str(), pair[1].as_str()),
            ("into", "outfile") | ("into", "dumpfile") | ("for", "update") | ("for", "share")
        ) {
            return Err(DataAccessError::write_denied());
        }
    }
    for triple in tokens.windows(3) {
        if matches!(
            (triple[0].as_str(), triple[1].as_str(), triple[2].as_str()),
            ("lock", "in", "share")
        ) {
            return Err(DataAccessError::write_denied());
        }
    }
    for pair in tokens.windows(2) {
        if matches!(pair[0].as_str(), "load_file" | "get_lock" | "release_lock") && pair[1] == "(" {
            return Err(DataAccessError::write_denied());
        }
    }
    Ok(())
}

fn sql_policy_tokens(sql: &str, comments_break_tokens: bool) -> Vec<String> {
    let chars: Vec<char> = sql.chars().collect();
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut idx = 0;
    while idx < chars.len() {
        let ch = chars[idx];
        if ch.is_whitespace() {
            push_token(&mut tokens, &mut current);
            idx += 1;
            continue;
        }
        if ch == '-' && chars.get(idx + 1) == Some(&'-') && is_mysql_line_comment(&chars, idx) {
            if comments_break_tokens {
                push_token(&mut tokens, &mut current);
            }
            idx += 2;
            idx = skip_line_comment(&chars, idx);
            continue;
        }
        if ch == '#' {
            if comments_break_tokens {
                push_token(&mut tokens, &mut current);
            }
            idx += 1;
            idx = skip_line_comment(&chars, idx);
            continue;
        }
        if ch == '/' && chars.get(idx + 1) == Some(&'*') {
            if chars.get(idx + 2) == Some(&'!') {
                push_token(&mut tokens, &mut current);
                tokens.push("__mysql_executable_comment__".to_owned());
            } else if comments_break_tokens {
                push_token(&mut tokens, &mut current);
            }
            idx += 2;
            while idx + 1 < chars.len() && !(chars[idx] == '*' && chars[idx + 1] == '/') {
                idx += 1;
            }
            idx = (idx + 2).min(chars.len());
            continue;
        }
        if matches!(ch, '\'' | '"' | '`') {
            push_token(&mut tokens, &mut current);
            idx = skip_quoted(&chars, idx, ch);
            continue;
        }
        if ch.is_ascii_alphanumeric() || ch == '_' {
            current.push(ch.to_ascii_lowercase());
            idx += 1;
            continue;
        }
        push_token(&mut tokens, &mut current);
        if ch == '(' || ch == ';' {
            tokens.push(ch.to_string());
        }
        idx += 1;
    }
    push_token(&mut tokens, &mut current);
    if tokens.last().is_some_and(|token| token == ";") {
        tokens.pop();
    }
    tokens
}

fn push_token(tokens: &mut Vec<String>, current: &mut String) {
    if !current.is_empty() {
        tokens.push(std::mem::take(current));
    }
}

fn skip_quoted(chars: &[char], start: usize, quote: char) -> usize {
    let mut idx = start + 1;
    while idx < chars.len() {
        if chars[idx] == '\\' {
            idx += 2;
            continue;
        }
        if chars[idx] == quote {
            if chars.get(idx + 1) == Some(&quote) {
                idx += 2;
            } else {
                return idx + 1;
            }
        } else {
            idx += 1;
        }
    }
    chars.len()
}

fn skip_line_comment(chars: &[char], mut idx: usize) -> usize {
    while idx < chars.len() && chars[idx] != '\n' && chars[idx] != '\r' {
        idx += 1;
    }
    if idx >= chars.len() {
        return chars.len();
    }
    if chars[idx] == '\r' && chars.get(idx + 1) == Some(&'\n') {
        return idx + 2;
    }
    idx + 1
}

fn is_mysql_line_comment(chars: &[char], idx: usize) -> bool {
    chars
        .get(idx + 2)
        .is_some_and(|ch| ch.is_whitespace() || ch.is_control())
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
        assert!(ensure_readonly_sql("select/* ordinary comment */ 1").is_ok());
        assert!(ensure_readonly_sql("select 1 -- harmless comment\n").is_ok());
        assert!(ensure_readonly_sql("select 1 -- harmless comment\r").is_ok());
        assert!(ensure_readonly_sql("select 1 # harmless comment\r\n").is_ok());
        assert_eq!(
            ensure_readonly_sql("delete from users").unwrap_err().code,
            "data_access_write_denied"
        );
        for sql in [
            "select * from users into outfile '/tmp/leak'",
            "select * from users into\noutfile '/tmp/leak'",
            "select * from users into   outfile '/tmp/leak'",
            "select * from users into/**/outfile '/tmp/leak'",
            "select * from users in/**/to out/**/file '/tmp/leak'",
            "select * from users /*!50000 into outfile '/tmp/leak' */",
            "select * from users into dumpfile '/tmp/leak'",
            "select * from users for update",
            "select * from users for\nupdate",
            "select * from users for/**/update",
            "select * from users fo/**/r up/**/date",
            "select * from users for share",
            "select * from users for/**/share",
            "select * from users fo/**/r sh/**/are",
            "select get_lock('x', 1)",
            "select get_lock ('x', 1)",
            "select get/**/_lock ('x', 1)",
            "select load_file/**/('/tmp/leak')",
            "select release_lock ('x')",
            "select 1; select 2",
            "select 1--1; delete from users",
            "select 1--x; update users set id = 1",
            "select 1--\r; delete from users",
            "select 1#\r; delete from users",
            "select 1-- \r; update users set id = 1",
        ] {
            assert_eq!(
                ensure_readonly_sql(sql).unwrap_err().code,
                "data_access_write_denied"
            );
        }
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

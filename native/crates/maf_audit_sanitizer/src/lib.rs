//! Audit/event sanitizer and aggregate safety contract artifact.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct AuditSanitizerError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorCodeEntry {
    pub code: String,
    pub category: String,
    pub retriable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SafetyContractArtifact {
    pub component: String,
    pub contract_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
    pub crates: Vec<String>,
    pub modes: Vec<String>,
    pub mode_env: BTreeMap<String, String>,
    pub resource_limits: BTreeMap<String, u64>,
    pub error_codes: Vec<ErrorCodeEntry>,
}

fn sensitive_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    if matches!(
        key.as_str(),
        "path"
            | "real_path"
            | "file_path"
            | "storage_path"
            | "absolute_path"
            | "filesystem_path"
            | "prompt"
            | "full_prompt"
            | "raw_prompt"
            | "system_prompt"
            | "user_prompt"
            | "llm_prompt"
            | "rows"
            | "raw_rows"
            | "result_rows"
            | "full_rows"
            | "candidate_rows"
    ) {
        return true;
    }
    if key.ends_with("_prompt") || key.ends_with("_rows") {
        return true;
    }
    ["secret", "token", "password", "base_url", "dsn"]
        .iter()
        .any(|needle| key.contains(needle))
}

impl AuditSanitizerError {
    fn event_too_large() -> Self {
        Self {
            code: "audit_sanitizer_event_too_large".to_owned(),
            message: "audit sanitizer output exceeds configured event size limit".to_owned(),
        }
    }
}

#[must_use]
pub fn sanitize_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, value)| {
                    if sensitive_key(key) {
                        (key.clone(), Value::String("[REDACTED]".to_owned()))
                    } else {
                        (key.clone(), sanitize_value(value))
                    }
                })
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(sanitize_value).collect()),
        Value::String(text) => Value::String(sanitize_text(text)),
        _ => value.clone(),
    }
}

#[must_use]
pub fn sanitize_text(text: &str) -> String {
    let mut output = text.to_owned();
    for prefix in [
        "token=",
        "secret=",
        "password=",
        "base_url=",
        "prompt=",
        "dsn=",
        "path=",
    ] {
        output = redact_assignment(&output, prefix);
    }
    for scheme in ["mysql://", "postgres://", "postgresql://"] {
        output = redact_bare_value(&output, scheme, "[REDACTED]");
    }
    output
}

fn redact_assignment(text: &str, prefix: &str) -> String {
    let mut output = String::with_capacity(text.len());
    let lower = text.to_ascii_lowercase();
    let mut cursor = 0;
    while let Some(relative) = lower[cursor..].find(prefix) {
        let start = cursor + relative;
        let value_start = start + prefix.len();
        output.push_str(&text[cursor..value_start]);
        output.push_str("[REDACTED]");
        let value_end = find_sensitive_value_end(text, value_start);
        cursor = value_end;
    }
    output.push_str(&text[cursor..]);
    output
}

fn redact_bare_value(text: &str, marker: &str, replacement: &str) -> String {
    let mut output = String::with_capacity(text.len());
    let lower = text.to_ascii_lowercase();
    let mut cursor = 0;
    while let Some(relative) = lower[cursor..].find(marker) {
        let start = cursor + relative;
        output.push_str(&text[cursor..start]);
        output.push_str(replacement);
        let value_end = find_sensitive_value_end(text, start);
        cursor = value_end;
    }
    output.push_str(&text[cursor..]);
    output
}

fn find_sensitive_value_end(text: &str, start: usize) -> usize {
    text[start..]
        .char_indices()
        .find_map(|(idx, ch)| {
            if ch.is_whitespace() || matches!(ch, ',' | ';' | '"' | '\'' | ')' | '}' | ']') {
                Some(start + idx)
            } else {
                None
            }
        })
        .unwrap_or(text.len())
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        ("artifact_path_escape", "security"),
        ("artifact_upload_too_large", "resource_limit"),
        ("artifact_structured_output_invalid", "contract"),
        ("artifact_contract_mismatch", "contract"),
        ("auth_token_invalid", "security"),
        ("auth_secret_missing", "security"),
        ("auth_structured_output_invalid", "contract"),
        ("data_access_write_denied", "security"),
        ("data_access_row_limit_exceeded", "resource_limit"),
        ("data_access_column_limit_exceeded", "resource_limit"),
        ("data_access_result_too_large", "resource_limit"),
        ("data_access_deadline_exceeded", "resource_limit"),
        ("data_access_structured_output_invalid", "contract"),
        ("audit_sanitizer_secret_redacted", "security"),
        ("audit_sanitizer_event_too_large", "resource_limit"),
        ("audit_sanitizer_structured_output_invalid", "contract"),
        ("audit_sanitizer_contract_mismatch", "contract"),
    ]
    .iter()
    .map(|(code, category)| ErrorCodeEntry {
        code: (*code).to_owned(),
        category: (*category).to_owned(),
        retriable: false,
    })
    .collect()
}

#[must_use]
pub fn safety_contract_artifact() -> SafetyContractArtifact {
    SafetyContractArtifact {
        component: "maf_safety_kernels".to_owned(),
        contract_version: "safety-kernels.v1".to_owned(),
        schema_hash: "maf_safety_kernels_schema_v1_20260517".to_owned(),
        error_code_table_hash: "maf_safety_kernels_error_table_v2_20260517".to_owned(),
        supported_features: vec![
            "artifact_store_kernel".to_owned(),
            "auth_core_kernel".to_owned(),
            "data_access_kernel".to_owned(),
            "audit_sanitizer_kernel".to_owned(),
            "pyo3_safety_facade".to_owned(),
        ],
        crates: vec![
            "maf_artifact_store".to_owned(),
            "maf_auth_core".to_owned(),
            "maf_data_access".to_owned(),
            "maf_audit_sanitizer".to_owned(),
        ],
        modes: vec!["off".to_owned(), "shadow".to_owned(), "enforce".to_owned()],
        mode_env: BTreeMap::from([
            (
                "artifact_store".to_owned(),
                "MAF_RUST_ARTIFACT_STORE_MODE".to_owned(),
            ),
            ("auth_core".to_owned(), "MAF_RUST_AUTH_CORE_MODE".to_owned()),
            (
                "data_access".to_owned(),
                "MAF_RUST_DATA_ACCESS_MODE".to_owned(),
            ),
            (
                "audit_sanitizer".to_owned(),
                "MAF_RUST_AUDIT_SANITIZER_MODE".to_owned(),
            ),
        ]),
        resource_limits: BTreeMap::from([
            ("auth_deadline_ms".to_owned(), 1_000),
            ("artifact_deadline_ms".to_owned(), 5_000),
            ("db_deadline_ms".to_owned(), 10_000),
            ("db_hard_cap_ms".to_owned(), 30_000),
            ("db_row_limit".to_owned(), 500),
            ("db_column_limit".to_owned(), 100),
            ("db_result_bytes".to_owned(), 10 * 1024 * 1024),
            ("upload_preview_bytes".to_owned(), 10 * 1024 * 1024),
            ("archive_hard_cap_ms".to_owned(), 60_000),
            ("audit_event_bytes".to_owned(), 64 * 1024),
        ]),
        error_codes: error_code_table(),
    }
}

pub fn safety_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&safety_contract_artifact())?;
    json.push('\n');
    Ok(json)
}

pub fn sanitize_event_json(payload: &str) -> Result<String, AuditSanitizerError> {
    let value: Value = serde_json::from_str(payload).map_err(|error| AuditSanitizerError {
        code: "audit_sanitizer_structured_output_invalid".to_owned(),
        message: format!("audit sanitizer input is invalid JSON: {error}"),
    })?;
    let sanitized = sanitize_value(&value);
    let output = serde_json::to_string(&sanitized).map_err(|error| AuditSanitizerError {
        code: "audit_sanitizer_structured_output_invalid".to_owned(),
        message: format!("audit sanitizer output is invalid JSON: {error}"),
    })?;
    if output.len() > 64 * 1024 {
        return Err(AuditSanitizerError::event_too_large());
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use std::path::PathBuf;

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    #[test]
    fn sanitizer_redacts_sensitive_fields() {
        let sanitized = sanitize_value(&json!({
            "safe": "ok",
            "token": "abc",
            "nested": {"base_url": "https://internal"},
            "rows": [{"id": 1}],
            "prompt": "full prompt should not log",
            "prompt_recorded": false,
            "message": "token=abc dsn=mysql://u:p@host/db path=/tmp/secret",
        }));
        assert_eq!(sanitized["safe"], "ok");
        assert_eq!(sanitized["token"], "[REDACTED]");
        assert_eq!(sanitized["nested"]["base_url"], "[REDACTED]");
        assert_eq!(sanitized["rows"], "[REDACTED]");
        assert_eq!(sanitized["prompt"], "[REDACTED]");
        assert_eq!(sanitized["prompt_recorded"], false);
        assert_eq!(
            sanitized["message"],
            "token=[REDACTED] dsn=[REDACTED] path=[REDACTED]",
        );
    }

    #[test]
    fn sanitizer_json_bridge_enforces_event_size() {
        let sanitized = sanitize_event_json(r#"{"token":"abc","safe":1}"#).unwrap();
        let parsed: Value = serde_json::from_str(&sanitized).unwrap();
        assert_eq!(parsed["token"], "[REDACTED]");
        assert_eq!(parsed["safe"], 1);

        let large = format!(r#"{{"safe":"{}"}}"#, "x".repeat(64 * 1024));
        assert_eq!(
            sanitize_event_json(&large).unwrap_err().code,
            "audit_sanitizer_event_too_large",
        );
    }

    #[test]
    fn checked_in_contract_artifact_matches_rust_canonical_export() {
        let artifact = fs::read_to_string(
            repo_root().join("src/integrations/rust_contracts/safety_contract.json"),
        )
        .expect("checked-in safety contract artifact must exist");
        assert_eq!(
            artifact,
            safety_contract_json().expect("serialize safety contract")
        );
    }
}

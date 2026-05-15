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
    pub crates: Vec<String>,
    pub modes: Vec<String>,
    pub mode_env: BTreeMap<String, String>,
    pub resource_limits: BTreeMap<String, u64>,
    pub error_codes: Vec<ErrorCodeEntry>,
}

fn sensitive_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    [
        "secret", "token", "password", "base_url", "prompt", "rows", "path", "dsn",
    ]
    .iter()
    .any(|needle| key.contains(needle))
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
        _ => value.clone(),
    }
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        ("artifact_path_escape", "security"),
        ("artifact_upload_too_large", "resource_limit"),
        ("auth_token_invalid", "security"),
        ("auth_secret_missing", "security"),
        ("data_access_write_denied", "security"),
        ("data_access_row_limit_exceeded", "resource_limit"),
        ("data_access_column_limit_exceeded", "resource_limit"),
        ("data_access_result_too_large", "resource_limit"),
        ("audit_sanitizer_secret_redacted", "security"),
        ("audit_sanitizer_event_too_large", "resource_limit"),
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
        }));
        assert_eq!(sanitized["safe"], "ok");
        assert_eq!(sanitized["token"], "[REDACTED]");
        assert_eq!(sanitized["nested"]["base_url"], "[REDACTED]");
        assert_eq!(sanitized["rows"], "[REDACTED]");
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

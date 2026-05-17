use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use serde::Deserialize;
use serde_json::json;

#[derive(Debug, Deserialize)]
struct StorageKeyRequest {
    key: String,
}

#[derive(Debug, Deserialize)]
struct TokenRequest {
    expected: String,
    actual: String,
}

#[derive(Debug, Deserialize)]
struct HmacRequest {
    secret: String,
    payload: String,
}

#[derive(Debug, Deserialize)]
struct TtlRequest {
    issued_at_ms: i64,
    ttl_ms: i64,
}

#[derive(Debug, Deserialize)]
struct ReadonlySqlRequest {
    sql: String,
}

#[derive(Debug, Deserialize)]
struct ShapeRequest {
    row_count: usize,
    column_count: usize,
    result_bytes: usize,
}

#[derive(Debug, Deserialize)]
struct SanitizeRequest {
    value: serde_json::Value,
}

#[pyfunction]
fn contract_json() -> PyResult<String> {
    maf_audit_sanitizer::safety_contract_json()
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
fn normalize_storage_key_json(payload: &str) -> String {
    match serde_json::from_str::<StorageKeyRequest>(payload) {
        Ok(request) => match maf_artifact_store::normalize_storage_key(&request.key) {
            Ok(value) => json!({"value": value, "error": null}).to_string(),
            Err(error) => json!({"value": null, "error": safety_error(&error.code, error.message)})
                .to_string(),
        },
        Err(error) => json!({
            "value": null,
            "error": safety_error(
                "artifact_structured_output_invalid",
                format!("Artifact storage-key request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn sha256_hex_bytes(content: &[u8]) -> String {
    maf_artifact_store::sha256_hex(content)
}

#[pyfunction]
fn verify_token_json(payload: &str) -> String {
    match serde_json::from_str::<TokenRequest>(payload) {
        Ok(request) => match maf_auth_core::verify_token(
            request.expected.as_bytes(),
            request.actual.as_bytes(),
        ) {
            Ok(()) => json!({"valid": true, "error": null}).to_string(),
            Err(error) => {
                json!({"valid": false, "error": safety_error(&error.code, error.message)})
                    .to_string()
            }
        },
        Err(error) => json!({
            "valid": false,
            "error": safety_error(
                "auth_structured_output_invalid",
                format!("Auth token request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn hmac_sha256_hex_json(payload: &str) -> String {
    match serde_json::from_str::<HmacRequest>(payload) {
        Ok(request) => match maf_auth_core::hmac_sha256_hex(
            request.secret.as_bytes(),
            request.payload.as_bytes(),
        ) {
            Ok(value) => json!({"value": value, "error": null}).to_string(),
            Err(error) => json!({"value": null, "error": safety_error(&error.code, error.message)})
                .to_string(),
        },
        Err(error) => json!({
            "value": null,
            "error": safety_error(
                "auth_structured_output_invalid",
                format!("Auth HMAC request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn expires_at_ms_json(payload: &str) -> String {
    match serde_json::from_str::<TtlRequest>(payload) {
        Ok(request) => json!({
            "value": maf_auth_core::expires_at_ms(request.issued_at_ms, request.ttl_ms),
            "error": null,
        })
        .to_string(),
        Err(error) => json!({
            "value": null,
            "error": safety_error(
                "auth_structured_output_invalid",
                format!("Auth TTL request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn ensure_readonly_sql_json(payload: &str) -> String {
    match serde_json::from_str::<ReadonlySqlRequest>(payload) {
        Ok(request) => match maf_data_access::ensure_readonly_sql(&request.sql) {
            Ok(()) => json!({"allowed": true, "error": null}).to_string(),
            Err(error) => {
                json!({"allowed": false, "error": safety_error(&error.code, error.message)})
                    .to_string()
            }
        },
        Err(error) => json!({
            "allowed": false,
            "error": safety_error(
                "data_access_structured_output_invalid",
                format!("Readonly SQL request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn validate_shape_json(payload: &str) -> String {
    match serde_json::from_str::<ShapeRequest>(payload) {
        Ok(request) => match maf_data_access::validate_shape(
            request.row_count,
            request.column_count,
            request.result_bytes,
        ) {
            Ok(()) => json!({"valid": true, "error": null}).to_string(),
            Err(error) => {
                json!({"valid": false, "error": safety_error(&error.code, error.message)})
                    .to_string()
            }
        },
        Err(error) => json!({
            "valid": false,
            "error": safety_error(
                "data_access_structured_output_invalid",
                format!("Readonly shape request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
fn sanitize_value_json(payload: &str) -> String {
    match serde_json::from_str::<SanitizeRequest>(payload) {
        Ok(request) => match maf_audit_sanitizer::sanitize_event_json(&request.value.to_string()) {
            Ok(value) => match serde_json::from_str::<serde_json::Value>(&value) {
                Ok(value) => json!({"value": value, "error": null}).to_string(),
                Err(error) => json!({
                    "value": null,
                    "error": safety_error(
                        "audit_sanitizer_structured_output_invalid",
                        format!("Audit sanitizer output is invalid JSON: {error}"),
                    ),
                })
                .to_string(),
            },
            Err(error) => json!({"value": null, "error": safety_error(&error.code, error.message)})
                .to_string(),
        },
        Err(error) => json!({
            "value": null,
            "error": safety_error(
                "audit_sanitizer_structured_output_invalid",
                format!("Audit sanitizer request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

fn safety_error(code: &str, message: impl Into<String>) -> serde_json::Value {
    json!({
        "code": code,
        "message": message.into(),
        "retriable": false,
        "category": error_category(code),
        "safe_metadata": {},
    })
}

fn error_category(code: &str) -> &'static str {
    if code.contains("too_large") || code.contains("limit_exceeded") {
        "resource_limit"
    } else if code.contains("structured") || code.contains("contract") {
        "contract"
    } else {
        "security"
    }
}

#[pymodule]
fn maf_safety_kernels_pyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(contract_json, m)?)?;
    m.add_function(wrap_pyfunction!(normalize_storage_key_json, m)?)?;
    m.add_function(wrap_pyfunction!(sha256_hex_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(verify_token_json, m)?)?;
    m.add_function(wrap_pyfunction!(hmac_sha256_hex_json, m)?)?;
    m.add_function(wrap_pyfunction!(expires_at_ms_json, m)?)?;
    m.add_function(wrap_pyfunction!(ensure_readonly_sql_json, m)?)?;
    m.add_function(wrap_pyfunction!(validate_shape_json, m)?)?;
    m.add_function(wrap_pyfunction!(sanitize_value_json, m)?)?;
    Ok(())
}

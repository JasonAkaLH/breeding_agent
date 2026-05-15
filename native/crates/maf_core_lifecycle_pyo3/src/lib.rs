use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use serde::Deserialize;
use serde_json::json;

#[derive(Debug, Deserialize)]
struct TransitionRequest {
    operation: String,
    current: String,
}

#[derive(Debug, Deserialize)]
struct OperationRequest {
    operation: String,
}

#[derive(Debug, Deserialize)]
struct StatusRequest {
    status: String,
}

#[derive(Debug, Deserialize)]
struct LateResultRequest {
    task_status: Option<String>,
}

#[pyfunction]
pub fn core_contract_json() -> PyResult<String> {
    maf_core_types::core_contract_json().map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
pub fn lifecycle_contract_json() -> PyResult<String> {
    maf_lifecycle::lifecycle_contract_json()
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
pub fn lifecycle_can_transition_json(payload: &str) -> String {
    match serde_json::from_str::<TransitionRequest>(payload) {
        Ok(request) => json!({
            "allowed": maf_lifecycle::can_transition(&request.operation, &request.current),
            "error": null,
        })
        .to_string(),
        Err(error) => json!({
            "allowed": false,
            "error": lifecycle_error(
                maf_lifecycle::LifecycleErrorCode::StructuredOutputInvalid.as_str(),
                format!("Lifecycle PyO3 transition request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
pub fn lifecycle_transition_target_json(payload: &str) -> String {
    match serde_json::from_str::<OperationRequest>(payload) {
        Ok(request) => match maf_lifecycle::transition_target(&request.operation) {
            Some(target) => json!({"target": target, "error": null}).to_string(),
            None => json!({
                "target": null,
                "error": lifecycle_error(
                    maf_lifecycle::LifecycleErrorCode::TransitionDenied.as_str(),
                    format!("Unknown lifecycle transition operation: {}", request.operation),
                ),
            })
            .to_string(),
        },
        Err(error) => json!({
            "target": null,
            "error": lifecycle_error(
                maf_lifecycle::LifecycleErrorCode::StructuredOutputInvalid.as_str(),
                format!("Lifecycle PyO3 transition target request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
pub fn lifecycle_cancel_node_target_json(payload: &str) -> String {
    match serde_json::from_str::<StatusRequest>(payload) {
        Ok(request) => json!({
            "target": maf_lifecycle::cancel_node_target(&request.status),
            "error": null,
        })
        .to_string(),
        Err(error) => json!({
            "target": null,
            "error": lifecycle_error(
                maf_lifecycle::LifecycleErrorCode::StructuredOutputInvalid.as_str(),
                format!("Lifecycle PyO3 cancel-node request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

#[pyfunction]
pub fn lifecycle_can_accept_late_result_json(payload: &str) -> String {
    match serde_json::from_str::<LateResultRequest>(payload) {
        Ok(request) => json!({
            "allowed": maf_lifecycle::can_accept_late_result(request.task_status.as_deref()),
            "error": null,
        })
        .to_string(),
        Err(error) => json!({
            "allowed": false,
            "error": lifecycle_error(
                maf_lifecycle::LifecycleErrorCode::StructuredOutputInvalid.as_str(),
                format!("Lifecycle PyO3 late-result request is invalid JSON: {error}"),
            ),
        })
        .to_string(),
    }
}

fn lifecycle_error(code: &str, message: impl Into<String>) -> serde_json::Value {
    json!({
        "code": code,
        "message": message.into(),
        "retriable": false,
        "category": "lifecycle",
        "safe_metadata": {},
    })
}

#[pymodule]
fn maf_core_lifecycle_pyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(core_contract_json, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_contract_json, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_can_transition_json, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_transition_target_json, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_cancel_node_target_json, m)?)?;
    m.add_function(wrap_pyfunction!(lifecycle_can_accept_late_result_json, m)?)?;
    Ok(())
}

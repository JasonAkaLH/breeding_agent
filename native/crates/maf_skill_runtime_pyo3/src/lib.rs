use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

#[pyfunction]
fn contract_json() -> PyResult<String> {
    maf_skill_runtime::skill_runtime_contract_json()
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
fn validate_policy_json(payload: &str) -> String {
    maf_skill_runtime::skill_policy_validate_json(payload)
}

#[pymodule]
fn maf_skill_runtime_pyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(contract_json, m)?)?;
    m.add_function(wrap_pyfunction!(validate_policy_json, m)?)?;
    Ok(())
}

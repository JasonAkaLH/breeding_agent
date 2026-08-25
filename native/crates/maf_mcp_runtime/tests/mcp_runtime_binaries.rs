use std::process::Command;

use maf_mcp_runtime::{COMPONENT_ID, PROTOCOL_VERSION};

#[test]
fn mcp_runtime_sidecar_binary_exposes_version_without_python() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-mcp-runtime-sidecar").expect("binary path");
    let output = Command::new(binary).output().expect("run sidecar binary");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert_eq!(
        stdout,
        format!(
            "{COMPONENT_ID} {} {PROTOCOL_VERSION}\n",
            env!("CARGO_PKG_VERSION")
        )
    );
}

#[test]
fn export_mcp_runtime_contract_binary_prints_contract_json() {
    let binary = std::env::var("CARGO_BIN_EXE_export-mcp-runtime-contract").expect("binary path");
    let output = Command::new(binary)
        .output()
        .expect("run contract export binary");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert_eq!(
        stdout,
        maf_mcp_runtime::mcp_runtime_contract_json().expect("serialize contract")
    );
}

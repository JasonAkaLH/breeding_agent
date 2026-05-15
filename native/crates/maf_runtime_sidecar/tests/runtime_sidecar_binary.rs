use maf_runtime_sidecar::COMPONENT_ID;
use std::process::Command;

#[test]
fn runtime_sidecar_binary_exposes_version_without_starting_python() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let output = Command::new(binary)
        .arg("--version")
        .output()
        .expect("run runtime sidecar binary");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert!(stdout.contains(COMPONENT_ID));
    assert!(stdout.contains("maf.runtime.v1"));
}

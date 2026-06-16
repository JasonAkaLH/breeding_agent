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

#[test]
fn runtime_sidecar_binary_rejects_partial_mtls_flags_without_serving() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let output = Command::new(binary)
        .args(["--serve", "127.0.0.1:0", "--tls-cert", "/tmp/server.crt"])
        .output()
        .expect("run runtime sidecar binary");

    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).expect("utf8 stderr");
    assert!(stderr.contains("mTLS requires --tls-cert, --tls-key, and --client-ca"));
}

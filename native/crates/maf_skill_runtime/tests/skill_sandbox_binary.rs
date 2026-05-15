use maf_skill_runtime::pb::skill::v1 as skill_pb;
use maf_skill_runtime::{COMPONENT_ID, SkillSandboxServeConfig};
use skill_pb::skill_sandbox_server::SkillSandbox;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn skill_sandbox_binary_exposes_version_without_starting_python() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-skill-sandbox").expect("binary path");
    let output = Command::new(binary)
        .arg("--version")
        .output()
        .expect("run skill sandbox binary");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert!(stdout.contains(COMPONENT_ID));
    assert!(stdout.contains("maf.skill.v1"));
}

#[test]
fn skill_sandbox_serve_config_rejects_public_bind_until_mtls_lands() {
    let config = SkillSandboxServeConfig::from_listen_addr("127.0.0.1:50052")
        .expect("loopback bind is allowed");
    assert_eq!(config.listen_addr.to_string(), "127.0.0.1:50052");

    let error = SkillSandboxServeConfig::from_listen_addr("0.0.0.0:50052")
        .expect_err("public bind must fail closed until mTLS is implemented");
    assert_eq!(error.code, "skill_runtime_sandbox_policy_denied");
}

#[tokio::test]
async fn skill_sandbox_serve_config_can_enable_process_manager_from_root() {
    let root = temp_sandbox_root("serve-config-process");
    write_script(&root, "echo.sh", "#!/bin/sh\nprintf 'from-config\\n'\n");
    let service = SkillSandboxServeConfig::from_listen_addr("127.0.0.1:50052")
        .expect("loopback bind is allowed")
        .with_sandbox_root(&root)
        .expect("sandbox root")
        .build_service();

    let response = service
        .execute_sandboxed(tonic::Request::new(skill_pb::ExecuteSandboxedRequest {
            skill_name: "example".to_owned(),
            execution_mode: "python_subprocess".to_owned(),
            cwd_under_public_root: ".".to_owned(),
            argv: vec!["./echo.sh".to_owned()],
            timeout_ms: 5_000,
            stdout_limit_bytes: 1024,
            stderr_limit_bytes: 1024,
            stdin_payload: Vec::new(),
        }))
        .await
        .expect("execute through grpc service")
        .into_inner();

    assert_eq!(response.stdout_prefix, b"from-config\n".to_vec());
    assert!(response.error.is_none());
    let _ = fs::remove_dir_all(root);
}

fn temp_sandbox_root(label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let root = std::env::temp_dir().join(format!("maf-skill-sandbox-{label}-{nanos}"));
    fs::create_dir_all(&root).expect("create sandbox root");
    root
}

fn write_script(root: &Path, name: &str, content: &str) {
    let path = root.join(name);
    fs::write(&path, content).expect("write script");
    let mut permissions = fs::metadata(&path).expect("metadata").permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).expect("chmod");
}

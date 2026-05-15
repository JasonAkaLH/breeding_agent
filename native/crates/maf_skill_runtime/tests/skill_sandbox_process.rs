use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use maf_skill_runtime::{
    ExecuteSandboxedRequest, SandboxProcessManager, SkillRuntimeErrorCode, SkillSandboxService,
};

const NORMAL_TIMEOUT_MS: u32 = 5_000;

#[test]
fn process_manager_runs_relative_program_with_bounded_stdio() {
    let root = temp_sandbox_root("run");
    write_script(&root, "echo.sh", "#!/bin/sh\ncat\necho err >&2\n");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./echo.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: b"hello\n".to_vec(),
    });

    assert_eq!(response.exit_code, 0);
    assert_eq!(response.stdout_prefix, b"hello\n".to_vec());
    assert_eq!(response.stderr_prefix, b"err\n".to_vec());
    assert!(response.error.is_none());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_enforces_stdout_limit_without_returning_unbounded_output() {
    let root = temp_sandbox_root("stdout-limit");
    write_script(&root, "loud.sh", "#!/bin/sh\nprintf abcdef\n");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./loud.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 3,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    assert_eq!(response.stdout_prefix, b"abc".to_vec());
    assert!(response.stdout_truncated);
    let error = response
        .error
        .expect("stdout over limit must be typed error");
    assert_eq!(error.code, SkillRuntimeErrorCode::OutputTooLarge.as_str());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_drains_large_stdout_without_waiting_for_timeout() {
    let root = temp_sandbox_root("large-stdout");
    write_script(
        &root,
        "large.sh",
        // Keep the payload comfortably above common pipe buffer sizes so the
        // test still proves concurrent draining, while avoiding a 2 MiB
        // pipeline that can hit the sandbox timeout on contended CI hosts.
        "#!/bin/sh\ndd if=/dev/zero bs=1024 count=256 2>/dev/null | tr '\\000' a\n",
    );
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./large.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 16,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    assert_eq!(response.exit_code, 0);
    assert_eq!(response.stdout_prefix, b"aaaaaaaaaaaaaaaa".to_vec());
    assert!(response.stdout_truncated);
    let error = response
        .error
        .expect("large stdout must be bounded by output error");
    assert_eq!(error.code, SkillRuntimeErrorCode::OutputTooLarge.as_str());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_rejects_stdin_larger_than_rust_contract_limit() {
    let root = temp_sandbox_root("stdin-limit");
    write_script(&root, "cat.sh", "#!/bin/sh\ncat >/dev/null\n");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./cat.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: vec![b'x'; 1024 * 1024 + 1],
    });

    let error = response
        .error
        .expect("oversized stdin must be rejected before process execution");
    assert_eq!(error.code, SkillRuntimeErrorCode::OutputTooLarge.as_str());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_does_not_inherit_parent_secret_environment() {
    let root = temp_sandbox_root("env-isolation");
    write_script(
        &root,
        "print-secret.sh",
        "#!/bin/sh\nprintf '%s' \"$MAF_SKILL_SANDBOX_SECRET_LEAK_TEST\"\n",
    );
    unsafe {
        std::env::set_var("MAF_SKILL_SANDBOX_SECRET_LEAK_TEST", "leaked-secret");
    }
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./print-secret.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    unsafe {
        std::env::remove_var("MAF_SKILL_SANDBOX_SECRET_LEAK_TEST");
    }
    assert_eq!(response.exit_code, 0);
    assert!(response.stdout_prefix.is_empty());
    assert!(response.error.is_none());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_times_out_and_fails_closed() {
    let root = temp_sandbox_root("timeout");
    write_script(&root, "sleep.sh", "#!/bin/sh\nsleep 2\n");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./sleep.sh".to_owned()],
        timeout_ms: 50,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    assert_eq!(response.exit_code, -1);
    let error = response.error.expect("timeout must be typed error");
    assert_eq!(error.code, SkillRuntimeErrorCode::SandboxTimeout.as_str());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_rejects_argv_path_escape() {
    let root = temp_sandbox_root("escape");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["../escape.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    let error = response.error.expect("argv escape must be typed error");
    assert_eq!(error.code, SkillRuntimeErrorCode::PublicRootEscape.as_str());
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_rejects_symlink_escape_after_canonicalization() {
    let root = temp_sandbox_root("symlink-escape");
    let outside = temp_sandbox_root("outside");
    write_script(&outside, "outside.sh", "#!/bin/sh\necho outside\n");
    symlink(outside.join("outside.sh"), root.join("link.sh")).expect("create symlink");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./link.sh".to_owned()],
        timeout_ms: NORMAL_TIMEOUT_MS,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });

    let error = response
        .error
        .expect("canonical symlink escape must be denied");
    assert_eq!(error.code, SkillRuntimeErrorCode::PublicRootEscape.as_str());
    let _ = fs::remove_dir_all(root);
    let _ = fs::remove_dir_all(outside);
}

#[test]
fn process_manager_rejects_zero_and_hard_timeout_overrides() {
    let root = temp_sandbox_root("timeout-bounds");
    write_script(&root, "echo.sh", "#!/bin/sh\necho ok\n");
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    for timeout_ms in [0, 300_001] {
        let response = service.execute_sandboxed(ExecuteSandboxedRequest {
            skill_name: "example".to_owned(),
            execution_mode: "python_subprocess".to_owned(),
            cwd_under_public_root: ".".to_owned(),
            argv: vec!["./echo.sh".to_owned()],
            timeout_ms,
            stdout_limit_bytes: 1024,
            stderr_limit_bytes: 1024,
            stdin_payload: Vec::new(),
        });

        let error = response
            .error
            .expect("invalid timeout must fail closed before execution");
        assert_eq!(
            error.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );
    }
    let _ = fs::remove_dir_all(root);
}

#[test]
fn process_manager_does_not_wait_for_lingering_descendant_stdio() {
    let root = temp_sandbox_root("lingering-stdio");
    write_script(
        &root,
        "linger.sh",
        "#!/bin/sh\n(sleep 3; echo late) &\necho done\nexit 0\n",
    );
    let service = SkillSandboxService::with_process_manager(SandboxProcessManager::new(&root));

    let started_at = Instant::now();
    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: ".".to_owned(),
        argv: vec!["./linger.sh".to_owned()],
        timeout_ms: 1_500,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: Vec::new(),
    });
    let elapsed = started_at.elapsed();

    assert!(response.stdout_prefix.starts_with(b"done\n"));
    assert!(
        !response
            .stdout_prefix
            .windows(4)
            .any(|window| window == b"late")
    );
    assert!(
        elapsed < Duration::from_millis(2_500),
        "sandbox waited for lingering descendant stdio for {elapsed:?}"
    );
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

fn write_script(root: &PathBuf, name: &str, content: &str) {
    let path = root.join(name);
    fs::write(&path, content).expect("write script");
    let mut permissions = fs::metadata(&path).expect("metadata").permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).expect("chmod");
}

use std::collections::BTreeMap;

use maf_skill_runtime::{
    CompatibilityCheck, ExecuteSandboxedRequest, HealthState, ReadinessState, SkillPolicyInput,
    SkillRuntimeErrorCode, SkillSandboxService,
};

fn policy_input() -> SkillPolicyInput {
    SkillPolicyInput {
        skill_name: "example".to_owned(),
        capability_id: "skill.example".to_owned(),
        execution_mode: "platform_service".to_owned(),
        trust_scope: "project".to_owned(),
        handler: "skill.example.platform_handler".to_owned(),
        manifest_services: vec!["mysql_readonly".to_owned()],
        runtime_allowlist_services: vec!["mysql_readonly".to_owned()],
        requested_services: vec!["mysql_readonly".to_owned()],
        runtime_allowlist_handlers: vec!["skill.example.platform_handler".to_owned()],
        x_runtime_rust: BTreeMap::from([("adapter".to_owned(), "pyo3".to_owned())]),
    }
}

#[test]
fn version_compatibility_and_readiness_are_owned_by_skill_sandbox_service() {
    let mut service = SkillSandboxService::new();
    let version = service.version();
    assert_eq!(version.component, "maf_skill_runtime");
    assert_eq!(version.protocol_version, "maf.skill.v1");
    assert!(
        version
            .supported_features
            .contains(&"sandbox_sidecar".to_owned())
    );
    assert_eq!(service.readiness().state, ReadinessState::NotReady);

    let result = service
        .accept_compatibility_handshake(CompatibilityCheck {
            client_version: "0.1.0".to_owned(),
            expected_component: version.component.clone(),
            expected_protocol_version: version.protocol_version.clone(),
            expected_schema_hash: version.schema_hash.clone(),
            expected_error_code_table_hash: version.error_code_table_hash.clone(),
            required_features: version.supported_features.clone(),
        })
        .expect("compatible skill sandbox handshake");
    assert!(result.compatible);
    assert_eq!(service.readiness().state, ReadinessState::Ready);
    assert_eq!(service.health().state, HealthState::Serving);
}

#[test]
fn compatibility_rejects_client_versions_outside_supported_range() {
    let mut service = SkillSandboxService::new();
    let version = service.version();
    let error = service
        .accept_compatibility_handshake(CompatibilityCheck {
            client_version: "0.2.0".to_owned(),
            expected_component: version.component,
            expected_protocol_version: version.protocol_version,
            expected_schema_hash: version.schema_hash,
            expected_error_code_table_hash: version.error_code_table_hash,
            required_features: version.supported_features,
        })
        .expect_err("unsupported client version must fail compatibility");
    assert_eq!(error.code, SkillRuntimeErrorCode::ContractMismatch.as_str());
    assert_eq!(service.readiness().state, ReadinessState::NotReady);
    assert!(!service.readiness().compatibility_handshake_passed);
}

#[test]
fn validate_policy_maps_policy_kernel_decision_and_typed_error_envelope() {
    let service = SkillSandboxService::new();
    let accepted = service.validate_policy(policy_input());
    assert!(accepted.allowed);
    assert!(accepted.error.is_none());
    assert_eq!(accepted.bundle_fingerprint.len(), 64);

    let mut denied_input = policy_input();
    denied_input
        .x_runtime_rust
        .insert("endpoint".to_owned(), "http://127.0.0.1:9000".to_owned());
    let denied = service.validate_policy(denied_input);
    let error = denied
        .error
        .expect("forbidden endpoint must be reported as typed error");
    assert_eq!(
        error.code,
        SkillRuntimeErrorCode::RustAdapterInvalid.as_str()
    );
    assert!(!denied.allowed);
}

#[test]
fn validate_policy_rejects_handlers_not_allowlisted_by_runtime() {
    let service = SkillSandboxService::new();
    let mut denied_input = policy_input();
    denied_input.runtime_allowlist_handlers = vec!["other.handler".to_owned()];

    let denied = service.validate_policy(denied_input);
    let error = denied
        .error
        .expect("handler missing from runtime allowlist must be denied");
    assert_eq!(
        error.code,
        SkillRuntimeErrorCode::HandlerNotAllowlisted.as_str()
    );
    assert!(!denied.allowed);
}

#[test]
fn execute_sandboxed_fails_closed_without_configured_process_manager() {
    let service = SkillSandboxService::new();
    let response = service.execute_sandboxed(ExecuteSandboxedRequest {
        skill_name: "example".to_owned(),
        execution_mode: "python_subprocess".to_owned(),
        cwd_under_public_root: "runtime/example".to_owned(),
        argv: vec!["./handler.sh".to_owned()],
        timeout_ms: 1_000,
        stdout_limit_bytes: 1024,
        stderr_limit_bytes: 1024,
        stdin_payload: b"{}".to_vec(),
    });
    let error = response
        .error
        .expect("sandbox execution must fail closed when process manager is not configured");
    assert_eq!(
        error.code,
        SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
    );
    assert_eq!(response.exit_code, -1);
    assert!(response.stdout_prefix.is_empty());
    assert!(response.stderr_prefix.is_empty());
}

use maf_skill_runtime::pb::common::v1 as common_pb;
use maf_skill_runtime::pb::skill::v1 as skill_pb;
use maf_skill_runtime::{COMPONENT_ID, SkillSandboxGrpcService};
use skill_pb::skill_sandbox_server::SkillSandbox;
use tonic::Request;

#[tokio::test]
async fn tonic_service_maps_skill_sandbox_requests_to_rust_kernel_envelopes() {
    let service = SkillSandboxGrpcService::new();

    let version = service
        .version(Request::new(skill_pb::VersionRequest {}))
        .await
        .expect("version")
        .into_inner()
        .version
        .expect("version info");
    assert_eq!(version.component, COMPONENT_ID);

    let compatibility = service
        .check_compatibility(Request::new(skill_pb::CompatibilityCheckRequest {
            client_version: "0.1.0".to_owned(),
            expected_component: version.component.clone(),
            expected_protocol_version: version.protocol_version.clone(),
            expected_schema_hash: version.schema_hash.clone(),
            expected_error_code_table_hash: version.error_code_table_hash.clone(),
            required_features: version.supported_features.clone(),
        }))
        .await
        .expect("compatibility")
        .into_inner();
    assert!(compatibility.compatible);
    assert!(compatibility.error.is_none());

    let readiness = service
        .readiness(Request::new(skill_pb::ReadinessRequest {}))
        .await
        .expect("readiness")
        .into_inner();
    assert_eq!(readiness.state, common_pb::ReadinessState::Ready as i32);
    assert!(readiness.compatibility_handshake_passed);

    let incompatible = service
        .check_compatibility(Request::new(skill_pb::CompatibilityCheckRequest {
            client_version: "0.2.0".to_owned(),
            expected_component: version.component.clone(),
            expected_protocol_version: version.protocol_version.clone(),
            expected_schema_hash: version.schema_hash.clone(),
            expected_error_code_table_hash: version.error_code_table_hash.clone(),
            required_features: version.supported_features.clone(),
        }))
        .await
        .expect("incompatible client version response")
        .into_inner();
    assert!(!incompatible.compatible);
    assert_eq!(
        incompatible.error.expect("typed error").code,
        "skill_runtime_contract_mismatch"
    );

    let policy = service
        .validate_policy(Request::new(skill_pb::ValidatePolicyRequest {
            skill_name: "example".to_owned(),
            capability_id: "skill.example".to_owned(),
            execution_mode: "platform_service".to_owned(),
            trust_scope: "project".to_owned(),
            handler: "skill.example.platform_handler".to_owned(),
            manifest_services: vec!["mysql_readonly".to_owned()],
            runtime_allowlist_services: vec!["mysql_readonly".to_owned()],
            requested_services: vec!["mysql_readonly".to_owned()],
            runtime_allowlist_handlers: vec!["skill.example.platform_handler".to_owned()],
            x_runtime_rust: [
                ("adapter".to_owned(), "pyo3".to_owned()),
                ("contract_version".to_owned(), "1".to_owned()),
            ]
            .into_iter()
            .collect(),
        }))
        .await
        .expect("policy")
        .into_inner();
    assert!(policy.allowed);
    assert_eq!(policy.bundle_fingerprint.len(), 64);

    let execution = service
        .execute_sandboxed(Request::new(skill_pb::ExecuteSandboxedRequest {
            skill_name: "example".to_owned(),
            execution_mode: "python_subprocess".to_owned(),
            cwd_under_public_root: "runtime/example".to_owned(),
            timeout_ms: 1_000,
            stdout_limit_bytes: 1024,
            stderr_limit_bytes: 1024,
            stdin_payload: b"{}".to_vec(),
            argv: vec!["./handler.sh".to_owned()],
        }))
        .await
        .expect("execute fail-closed")
        .into_inner();
    let error = execution.error.expect("typed error");
    assert_eq!(error.code, "skill_runtime_sandbox_policy_denied");
    assert_eq!(error.category, common_pb::ErrorCategory::Security as i32);
    assert_eq!(execution.exit_code, -1);
}

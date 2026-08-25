use std::collections::BTreeMap;

use super::pb::common::v1 as common_pb;
use super::pb::skill::v1 as skill_pb;
use super::{
    CompatibilityCheck, ExecuteSandboxedRequest, HealthState, MAX_CLIENT_VERSION,
    MIN_CLIENT_VERSION, ReadinessState, SandboxProcessManager, SkillPolicyInput, SkillRuntimeError,
    SkillRuntimeVersion, SkillSandboxGrpcService, SkillSandboxService, TypedErrorEnvelope,
};

impl SkillSandboxGrpcService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            inner: std::sync::Arc::new(std::sync::Mutex::new(SkillSandboxService::new())),
        }
    }

    #[must_use]
    pub fn with_process_manager(process_manager: SandboxProcessManager) -> Self {
        Self {
            inner: std::sync::Arc::new(std::sync::Mutex::new(
                SkillSandboxService::with_process_manager(process_manager),
            )),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, SkillSandboxService>, tonic::Status> {
        self.inner
            .lock()
            .map_err(|_| tonic::Status::internal("skill sandbox service lock is poisoned"))
    }
}

#[tonic::async_trait]
impl skill_pb::skill_sandbox_server::SkillSandbox for SkillSandboxGrpcService {
    async fn health(
        &self,
        _request: tonic::Request<skill_pb::HealthRequest>,
    ) -> Result<tonic::Response<skill_pb::HealthResponse>, tonic::Status> {
        let status = self.lock()?.health();
        Ok(tonic::Response::new(skill_pb::HealthResponse {
            state: health_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            error: None,
        }))
    }

    async fn readiness(
        &self,
        _request: tonic::Request<skill_pb::ReadinessRequest>,
    ) -> Result<tonic::Response<skill_pb::ReadinessResponse>, tonic::Status> {
        let status = self.lock()?.readiness();
        Ok(tonic::Response::new(skill_pb::ReadinessResponse {
            state: readiness_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            compatibility_handshake_passed: status.compatibility_handshake_passed,
            error: None,
        }))
    }

    async fn version(
        &self,
        _request: tonic::Request<skill_pb::VersionRequest>,
    ) -> Result<tonic::Response<skill_pb::VersionResponse>, tonic::Status> {
        let version = self.lock()?.version();
        Ok(tonic::Response::new(skill_pb::VersionResponse {
            version: Some(version_to_pb(version)),
        }))
    }

    async fn check_compatibility(
        &self,
        request: tonic::Request<skill_pb::CompatibilityCheckRequest>,
    ) -> Result<tonic::Response<skill_pb::CompatibilityCheckResponse>, tonic::Status> {
        let request = request.into_inner();
        let mut service = self.lock()?;
        let version = service.version();
        let result = service.accept_compatibility_handshake(CompatibilityCheck {
            client_version: request.client_version,
            expected_component: request.expected_component,
            expected_protocol_version: request.expected_protocol_version,
            expected_schema_hash: request.expected_schema_hash,
            expected_error_code_table_hash: request.expected_error_code_table_hash,
            required_features: request.required_features,
        });
        match result {
            Ok(result) => Ok(tonic::Response::new(skill_pb::CompatibilityCheckResponse {
                compatible: result.compatible,
                version: Some(version_to_pb(version)),
                missing_features: result.missing_features,
                error: None,
            })),
            Err(error) => Ok(tonic::Response::new(skill_pb::CompatibilityCheckResponse {
                compatible: false,
                version: Some(version_to_pb(version)),
                missing_features: missing_features_from_error(&error),
                error: Some(typed_error_to_pb(TypedErrorEnvelope::from(error))),
            })),
        }
    }

    async fn validate_policy(
        &self,
        request: tonic::Request<skill_pb::ValidatePolicyRequest>,
    ) -> Result<tonic::Response<skill_pb::ValidatePolicyResponse>, tonic::Status> {
        let request = request.into_inner();
        let x_runtime_rust = request
            .x_runtime_rust
            .into_iter()
            .collect::<BTreeMap<_, _>>();
        let input = SkillPolicyInput {
            skill_name: request.skill_name,
            capability_id: request.capability_id,
            execution_mode: request.execution_mode,
            trust_scope: request.trust_scope,
            handler: request.handler,
            requested_services: if request.requested_services.is_empty() {
                request.manifest_services.clone()
            } else {
                request.requested_services
            },
            manifest_services: request.manifest_services,
            runtime_allowlist_services: request.runtime_allowlist_services,
            runtime_allowlist_handlers: request.runtime_allowlist_handlers,
            x_runtime_rust,
        };
        let response = self.lock()?.validate_policy(input);
        Ok(tonic::Response::new(skill_pb::ValidatePolicyResponse {
            allowed: response.allowed,
            bundle_fingerprint: response.bundle_fingerprint,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn execute_sandboxed(
        &self,
        request: tonic::Request<skill_pb::ExecuteSandboxedRequest>,
    ) -> Result<tonic::Response<skill_pb::ExecuteSandboxedResponse>, tonic::Status> {
        let request = request.into_inner();
        let response = self.lock()?.execute_sandboxed(ExecuteSandboxedRequest {
            skill_name: request.skill_name,
            execution_mode: request.execution_mode,
            cwd_under_public_root: request.cwd_under_public_root,
            argv: request.argv,
            timeout_ms: request.timeout_ms,
            stdout_limit_bytes: request.stdout_limit_bytes,
            stderr_limit_bytes: request.stderr_limit_bytes,
            stdin_payload: request.stdin_payload,
        });
        Ok(tonic::Response::new(skill_pb::ExecuteSandboxedResponse {
            exit_code: response.exit_code,
            stdout_prefix: response.stdout_prefix,
            stderr_prefix: response.stderr_prefix,
            stdout_truncated: response.stdout_truncated,
            stderr_truncated: response.stderr_truncated,
            error: response.error.map(typed_error_to_pb),
        }))
    }
}

fn version_to_pb(version: SkillRuntimeVersion) -> common_pb::VersionInfo {
    common_pb::VersionInfo {
        component: version.component,
        build_version: env!("CARGO_PKG_VERSION").to_owned(),
        protocol_version: version.protocol_version,
        schema_hash: version.schema_hash,
        error_code_table_hash: version.error_code_table_hash,
        supported_features: version.supported_features,
        min_client_version: MIN_CLIENT_VERSION.to_owned(),
        max_client_version: MAX_CLIENT_VERSION.to_owned(),
    }
}

pub(super) fn health_state_to_pb(state: HealthState) -> common_pb::HealthState {
    match state {
        HealthState::Serving => common_pb::HealthState::Serving,
        HealthState::NotServing => common_pb::HealthState::NotServing,
        HealthState::Degraded => common_pb::HealthState::Degraded,
    }
}

pub(super) fn readiness_state_to_pb(state: ReadinessState) -> common_pb::ReadinessState {
    match state {
        ReadinessState::Ready => common_pb::ReadinessState::Ready,
        ReadinessState::NotReady => common_pb::ReadinessState::NotReady,
    }
}

fn typed_error_to_pb(error: TypedErrorEnvelope) -> common_pb::TypedError {
    common_pb::TypedError {
        code: error.code,
        message: error.message,
        retriable: error.retriable,
        category: error_category_to_pb(&error.category) as i32,
        safe_metadata: error.safe_metadata.into_iter().collect(),
    }
}

pub(super) fn error_category_to_pb(category: &str) -> common_pb::ErrorCategory {
    match category {
        "configuration" => common_pb::ErrorCategory::Configuration,
        "compatibility" => common_pb::ErrorCategory::Compatibility,
        "security" => common_pb::ErrorCategory::Security,
        "resource_limit" => common_pb::ErrorCategory::ResourceLimit,
        "protocol" => common_pb::ErrorCategory::Protocol,
        "upstream" => common_pb::ErrorCategory::Upstream,
        "cancellation" => common_pb::ErrorCategory::Cancellation,
        _ => common_pb::ErrorCategory::Internal,
    }
}

pub(super) fn missing_features_from_error(error: &SkillRuntimeError) -> Vec<String> {
    error
        .safe_metadata
        .get("missing_features")
        .map(|features| {
            features
                .split(',')
                .filter(|feature| !feature.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

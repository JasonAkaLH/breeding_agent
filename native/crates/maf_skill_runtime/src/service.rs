use std::collections::BTreeSet;

use super::{
    CompatibilityCheck, CompatibilityResult, ExecuteSandboxedRequest, ExecuteSandboxedResponse,
    HealthState, HealthStatus, ReadinessState, ReadinessStatus, SandboxProcessManager,
    SkillPolicyInput, SkillRuntimeError, SkillRuntimeErrorCode, SkillRuntimeVersion,
    SkillSandboxService, TypedErrorEnvelope, ValidatePolicyResponse, allowed_execution_modes,
    guard_public_root_path, sandbox_error_response, skill_runtime_contract_artifact,
    validate_client_version, validate_policy,
};

impl SkillSandboxService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            compatibility_handshake_passed: false,
            process_manager: None,
        }
    }

    #[must_use]
    pub fn with_process_manager(process_manager: SandboxProcessManager) -> Self {
        Self {
            compatibility_handshake_passed: false,
            process_manager: Some(process_manager),
        }
    }

    #[must_use]
    pub fn version(&self) -> SkillRuntimeVersion {
        let artifact = skill_runtime_contract_artifact();
        SkillRuntimeVersion {
            component: artifact.component,
            protocol_version: artifact.protocol_version,
            schema_hash: artifact.schema_hash,
            error_code_table_hash: artifact.error_code_table_hash,
            supported_features: artifact.supported_features,
        }
    }

    #[must_use]
    pub fn health(&self) -> HealthStatus {
        HealthStatus {
            state: HealthState::Serving,
            version: self.version(),
        }
    }

    #[must_use]
    pub fn readiness(&self) -> ReadinessStatus {
        ReadinessStatus {
            state: if self.compatibility_handshake_passed {
                ReadinessState::Ready
            } else {
                ReadinessState::NotReady
            },
            version: self.version(),
            compatibility_handshake_passed: self.compatibility_handshake_passed,
        }
    }

    pub fn check_compatibility(
        &self,
        check: CompatibilityCheck,
    ) -> Result<CompatibilityResult, SkillRuntimeError> {
        let version = self.version();
        if check.expected_component != version.component
            || check.expected_protocol_version != version.protocol_version
            || check.expected_schema_hash != version.schema_hash
            || check.expected_error_code_table_hash != version.error_code_table_hash
        {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::ContractMismatch,
                "skill sandbox protocol handshake is incompatible",
            ));
        }
        validate_client_version(&check.client_version)?;

        let supported = version
            .supported_features
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let missing_features = check
            .required_features
            .into_iter()
            .filter(|feature| !supported.contains(feature))
            .collect::<Vec<_>>();
        if !missing_features.is_empty() {
            let mut error = SkillRuntimeError::new(
                SkillRuntimeErrorCode::ContractMismatch,
                "skill sandbox required features are missing",
            );
            error
                .safe_metadata
                .insert("missing_features".to_owned(), missing_features.join(","));
            return Err(error);
        }

        Ok(CompatibilityResult {
            compatible: true,
            missing_features,
        })
    }

    pub fn accept_compatibility_handshake(
        &mut self,
        check: CompatibilityCheck,
    ) -> Result<CompatibilityResult, SkillRuntimeError> {
        let result = self.check_compatibility(check)?;
        self.compatibility_handshake_passed = true;
        Ok(result)
    }

    #[must_use]
    pub fn validate_policy(&self, input: SkillPolicyInput) -> ValidatePolicyResponse {
        match validate_policy(&input) {
            Ok(decision) => ValidatePolicyResponse {
                allowed: decision.allowed,
                bundle_fingerprint: decision.bundle_fingerprint,
                error: None,
            },
            Err(error) => ValidatePolicyResponse {
                allowed: false,
                bundle_fingerprint: String::new(),
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    #[must_use]
    pub fn execute_sandboxed(&self, request: ExecuteSandboxedRequest) -> ExecuteSandboxedResponse {
        if let Err(error) = guard_public_root_path(&request.cwd_under_public_root) {
            return sandbox_error_response(error);
        }
        if !allowed_execution_modes().contains(&request.execution_mode) {
            return sandbox_error_response(SkillRuntimeError::new(
                SkillRuntimeErrorCode::ManifestInvalid,
                "execution mode is not supported by generic skill runtime",
            ));
        }
        if let Some(process_manager) = &self.process_manager {
            return process_manager.execute(&request);
        }
        sandbox_error_response(SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "rust skill sandbox process manager is not enabled yet",
        ))
    }
}

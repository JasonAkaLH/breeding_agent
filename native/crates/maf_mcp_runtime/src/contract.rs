use super::{
    COMPONENT_ID, CompatibilityCheckRequest, CompatibilityCheckResponse, ERROR_CODE_TABLE_HASH,
    HealthResponse, HealthState, MAX_CLIENT_VERSION, MIN_CLIENT_VERSION,
    McpRuntimeContractArtifact, McpRuntimeErrorCode, PROTOCOL_VERSION, RELATED_TASK_META_KEY,
    ReadinessResponse, ReadinessState, SCHEMA_HASH, SUPPORTED_FEATURES,
    SUPPORTED_MCP_PROTOCOL_VERSIONS, TypedError, VersionInfo,
};

pub(super) fn string_values(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

pub(super) fn task_terminal_states() -> Vec<String> {
    string_values(&["completed", "failed", "cancelled"])
}

pub(super) fn mcp_runtime_contract_artifact() -> McpRuntimeContractArtifact {
    McpRuntimeContractArtifact {
        component: COMPONENT_ID.to_owned(),
        protocol_version: PROTOCOL_VERSION.to_owned(),
        schema_hash: SCHEMA_HASH.to_owned(),
        error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        task_terminal_states: task_terminal_states(),
        task_cancelled_state: "cancelled".to_owned(),
        task_completed_state: "completed".to_owned(),
        task_failed_state: "failed".to_owned(),
        task_input_required_state: "input_required".to_owned(),
        task_default_state: "working".to_owned(),
        related_task_meta_key: RELATED_TASK_META_KEY.to_owned(),
    }
}

pub(super) fn mcp_runtime_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&mcp_runtime_contract_artifact())?;
    json.push('\n');
    Ok(json)
}

pub(super) fn approved_mcp_protocol_versions() -> Vec<String> {
    string_values(SUPPORTED_MCP_PROTOCOL_VERSIONS)
}

impl VersionInfo {
    #[must_use]
    pub fn current() -> Self {
        Self {
            component: COMPONENT_ID.to_owned(),
            build_version: env!("CARGO_PKG_VERSION").to_owned(),
            protocol_version: PROTOCOL_VERSION.to_owned(),
            schema_hash: SCHEMA_HASH.to_owned(),
            error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
            supported_features: SUPPORTED_FEATURES
                .iter()
                .map(|feature| (*feature).to_owned())
                .collect(),
            min_client_version: MIN_CLIENT_VERSION.to_owned(),
            max_client_version: MAX_CLIENT_VERSION.to_owned(),
        }
    }

    #[must_use]
    pub fn supports(&self, feature: &str) -> bool {
        self.supported_features
            .iter()
            .any(|supported| supported == feature)
    }
}

pub(super) fn health() -> HealthResponse {
    HealthResponse {
        state: HealthState::Serving,
        version: VersionInfo::current(),
    }
}

pub(super) fn readiness(compatibility_handshake_passed: bool) -> ReadinessResponse {
    let error = (!compatibility_handshake_passed).then(|| {
        TypedError::new(
            McpRuntimeErrorCode::CompatibilityHandshakeRequired,
            "compatibility handshake has not passed",
        )
    });

    ReadinessResponse {
        state: if compatibility_handshake_passed {
            ReadinessState::Ready
        } else {
            ReadinessState::NotReady
        },
        version: VersionInfo::current(),
        compatibility_handshake_passed,
        error,
    }
}

pub(super) fn check_compatibility(
    request: &CompatibilityCheckRequest,
) -> CompatibilityCheckResponse {
    let version = VersionInfo::current();
    let missing_features = request
        .required_features
        .iter()
        .filter(|feature| !version.supports(feature))
        .cloned()
        .collect::<Vec<_>>();

    let incompatible_reason = if request.expected_component != COMPONENT_ID {
        Some("component mismatch")
    } else if request.expected_protocol_version != PROTOCOL_VERSION {
        Some("protocol version mismatch")
    } else if request.expected_schema_hash != SCHEMA_HASH {
        Some("schema hash mismatch")
    } else if request.expected_error_code_table_hash != ERROR_CODE_TABLE_HASH {
        Some("error code table hash mismatch")
    } else if !missing_features.is_empty() {
        Some("required feature is not supported")
    } else {
        None
    };

    CompatibilityCheckResponse {
        compatible: incompatible_reason.is_none(),
        version,
        missing_features,
        error: incompatible_reason
            .map(|message| TypedError::new(McpRuntimeErrorCode::ProtocolIncompatible, message)),
    }
}

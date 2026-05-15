//! Phase 1 Rust MCP runtime sidecar contract skeleton.
//!
//! This crate intentionally exposes only stable health, version, readiness,
//! feature, and typed-error structures. Full MCP transport, JSON-RPC, tool
//! execution, task registry, and streaming behavior are reserved for later MCP
//! phases.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const COMPONENT_ID: &str = "maf_mcp_runtime_sidecar";
pub const PROTOCOL_VERSION: &str = "maf.mcp.sidecar.v1";
pub const SCHEMA_HASH: &str = "maf_mcp_v1_phase1_schema_hash_pending_ci";
pub const ERROR_CODE_TABLE_HASH: &str = "maf_mcp_runtime_error_table_v1_phase1";
pub const MIN_CLIENT_VERSION: &str = "0.1.0";
pub const MAX_CLIENT_VERSION: &str = "0.1.x";

pub const FEATURE_HEALTH: &str = "health";
pub const FEATURE_READINESS: &str = "readiness";
pub const FEATURE_VERSION: &str = "version";
pub const FEATURE_COMPATIBILITY_HANDSHAKE: &str = "compatibility_handshake";
pub const FEATURE_SHORT_CALL: &str = "short_call";
pub const FEATURE_STREAMABLE_HTTP: &str = "streamable_http";
pub const FEATURE_SSE_STREAM: &str = "sse_stream";
pub const FEATURE_SERVER_TO_CLIENT_GET: &str = "server_to_client_get";
pub const FEATURE_MCP_TASKS: &str = "mcp_tasks";
pub const FEATURE_TASK_AUGMENTED_TOOLS_CALL: &str = "task_augmented_tools_call";
pub const FEATURE_REMOTE_CANCEL: &str = "remote_cancel";
pub const RELATED_TASK_META_KEY: &str = "io.modelcontextprotocol/related-task";

pub const SUPPORTED_FEATURES: &[&str] = &[
    FEATURE_HEALTH,
    FEATURE_READINESS,
    FEATURE_VERSION,
    FEATURE_COMPATIBILITY_HANDSHAKE,
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VersionInfo {
    pub component: String,
    pub build_version: String,
    pub protocol_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
    pub min_client_version: String,
    pub max_client_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct McpRuntimeContractArtifact {
    pub component: String,
    pub protocol_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub task_terminal_states: Vec<String>,
    pub task_cancelled_state: String,
    pub task_completed_state: String,
    pub task_failed_state: String,
    pub task_input_required_state: String,
    pub task_default_state: String,
    pub related_task_meta_key: String,
}

fn string_values(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

#[must_use]
pub fn task_terminal_states() -> Vec<String> {
    string_values(&["completed", "failed", "cancelled"])
}

#[must_use]
pub fn mcp_runtime_contract_artifact() -> McpRuntimeContractArtifact {
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

pub fn mcp_runtime_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&mcp_runtime_contract_artifact())?;
    json.push('\n');
    Ok(json)
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum HealthState {
    Serving,
    NotServing,
    Degraded,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HealthResponse {
    pub state: HealthState,
    pub version: VersionInfo,
}

#[must_use]
pub fn health() -> HealthResponse {
    HealthResponse {
        state: HealthState::Serving,
        version: VersionInfo::current(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReadinessState {
    Ready,
    NotReady,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadinessResponse {
    pub state: ReadinessState,
    pub version: VersionInfo,
    pub compatibility_handshake_passed: bool,
    pub error: Option<TypedError>,
}

#[must_use]
pub fn readiness(compatibility_handshake_passed: bool) -> ReadinessResponse {
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheckRequest {
    pub client_version: String,
    pub expected_component: String,
    pub expected_protocol_version: String,
    pub expected_schema_hash: String,
    pub expected_error_code_table_hash: String,
    pub required_features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheckResponse {
    pub compatible: bool,
    pub version: VersionInfo,
    pub missing_features: Vec<String>,
    pub error: Option<TypedError>,
}

#[must_use]
pub fn check_compatibility(request: &CompatibilityCheckRequest) -> CompatibilityCheckResponse {
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ErrorCategory {
    Configuration,
    Compatibility,
    Security,
    ResourceLimit,
    Protocol,
    Upstream,
    Internal,
    Cancellation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum McpRuntimeErrorCode {
    ConfigurationInvalid,
    ProtocolIncompatible,
    CompatibilityHandshakeRequired,
    IdentityMismatch,
    PublicBindDenied,
    EndpointNotAllowlisted,
    QueueFull,
    PerServerConcurrencyExceeded,
    DeadlineExceeded,
    PayloadTooLarge,
    StreamIdleTimeout,
    JsonRpcInvalid,
    SchemaValidationFailed,
    OutputSanitizationFailed,
    BundleActivationFailed,
    RemoteServerError,
    Cancelled,
    Internal,
}

impl McpRuntimeErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ConfigurationInvalid => "mcp_runtime_configuration_invalid",
            Self::ProtocolIncompatible => "mcp_runtime_protocol_incompatible",
            Self::CompatibilityHandshakeRequired => "mcp_runtime_compatibility_handshake_required",
            Self::IdentityMismatch => "mcp_runtime_identity_mismatch",
            Self::PublicBindDenied => "mcp_runtime_public_bind_denied",
            Self::EndpointNotAllowlisted => "mcp_runtime_endpoint_not_allowlisted",
            Self::QueueFull => "mcp_runtime_queue_full",
            Self::PerServerConcurrencyExceeded => "mcp_runtime_per_server_concurrency_exceeded",
            Self::DeadlineExceeded => "mcp_runtime_deadline_exceeded",
            Self::PayloadTooLarge => "mcp_runtime_payload_too_large",
            Self::StreamIdleTimeout => "mcp_runtime_stream_idle_timeout",
            Self::JsonRpcInvalid => "mcp_runtime_json_rpc_invalid",
            Self::SchemaValidationFailed => "mcp_runtime_schema_validation_failed",
            Self::OutputSanitizationFailed => "mcp_runtime_output_sanitization_failed",
            Self::BundleActivationFailed => "mcp_runtime_bundle_activation_failed",
            Self::RemoteServerError => "mcp_runtime_remote_server_error",
            Self::Cancelled => "mcp_runtime_cancelled",
            Self::Internal => "mcp_runtime_internal",
        }
    }

    #[must_use]
    pub const fn category(self) -> ErrorCategory {
        match self {
            Self::ConfigurationInvalid => ErrorCategory::Configuration,
            Self::ProtocolIncompatible | Self::CompatibilityHandshakeRequired => {
                ErrorCategory::Compatibility
            }
            Self::IdentityMismatch | Self::PublicBindDenied | Self::EndpointNotAllowlisted => {
                ErrorCategory::Security
            }
            Self::QueueFull
            | Self::PerServerConcurrencyExceeded
            | Self::DeadlineExceeded
            | Self::PayloadTooLarge
            | Self::StreamIdleTimeout => ErrorCategory::ResourceLimit,
            Self::JsonRpcInvalid
            | Self::SchemaValidationFailed
            | Self::OutputSanitizationFailed
            | Self::BundleActivationFailed => ErrorCategory::Protocol,
            Self::RemoteServerError => ErrorCategory::Upstream,
            Self::Cancelled => ErrorCategory::Cancellation,
            Self::Internal => ErrorCategory::Internal,
        }
    }

    #[must_use]
    pub const fn retriable(self) -> bool {
        matches!(
            self,
            Self::QueueFull
                | Self::PerServerConcurrencyExceeded
                | Self::DeadlineExceeded
                | Self::StreamIdleTimeout
                | Self::RemoteServerError
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: ErrorCategory,
    pub safe_metadata: BTreeMap<String, String>,
}

impl TypedError {
    #[must_use]
    pub fn new(code: McpRuntimeErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code.as_str().to_owned(),
            message: message.into(),
            retriable: code.retriable(),
            category: code.category(),
            safe_metadata: BTreeMap::new(),
        }
    }

    #[must_use]
    pub fn with_safe_metadata(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.safe_metadata.insert(key.into(), value.into());
        self
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{typed_error:?}")]
pub struct McpRuntimeError {
    pub typed_error: TypedError,
}

impl From<McpRuntimeErrorCode> for McpRuntimeError {
    fn from(code: McpRuntimeErrorCode) -> Self {
        Self {
            typed_error: TypedError::new(code, code.as_str()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExternalMcpContext {
    pub external_protocol_version: String,
    pub server_capabilities: BTreeMap<String, String>,
    pub tool_execution_task_support: BTreeMap<String, bool>,
    pub session_id_fingerprint: Option<String>,
    pub last_event_id_fingerprint: Option<String>,
    pub progress_token_fingerprint: Option<String>,
    pub task_safe_ref: Option<String>,
    pub remote_error_code: Option<String>,
    pub json_rpc_id_correlation: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequestEnvelope {
    pub jsonrpc: String,
    pub method: String,
    pub id: Option<serde_json::Value>,
    pub params: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcRawEnvelope {
    jsonrpc: Option<String>,
    method: Option<String>,
    id: Option<serde_json::Value>,
    params: Option<serde_json::Value>,
    result: Option<serde_json::Value>,
    error: Option<serde_json::Value>,
}

pub const MAX_JSON_RPC_BYTES: usize = 1024 * 1024;
pub const MAX_RAW_TOOL_OUTPUT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_SANITIZED_TOOL_OUTPUT_BYTES: usize = 4 * 1024 * 1024;

pub fn validate_json_rpc_request(
    payload: &[u8],
) -> Result<JsonRpcRequestEnvelope, McpRuntimeError> {
    if payload.len() > MAX_JSON_RPC_BYTES {
        return Err(McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::PayloadTooLarge,
                "JSON-RPC payload exceeds limit",
            ),
        });
    }
    let raw: JsonRpcRawEnvelope = serde_json::from_slice(payload).map_err(|_| McpRuntimeError {
        typed_error: TypedError::new(
            McpRuntimeErrorCode::JsonRpcInvalid,
            "invalid JSON-RPC payload",
        ),
    })?;
    if raw.jsonrpc.as_deref() != Some("2.0") {
        return Err(McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::JsonRpcInvalid,
                "JSON-RPC version must be 2.0",
            ),
        });
    }
    if raw.result.is_some() || raw.error.is_some() {
        return Err(McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::JsonRpcInvalid,
                "request cannot contain result or error",
            ),
        });
    }
    let method = raw
        .method
        .filter(|method| !method.trim().is_empty())
        .ok_or_else(|| McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::JsonRpcInvalid,
                "request method is required",
            ),
        })?;
    Ok(JsonRpcRequestEnvelope {
        jsonrpc: "2.0".to_owned(),
        method,
        id: raw.id,
        params: raw.params,
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SanitizedToolOutput {
    pub text: String,
    pub truncated: bool,
    pub redaction_count: usize,
}

fn redact_authority_tokens(raw: &str) -> (String, usize) {
    let mut output = Vec::new();
    let mut redactions = 0usize;
    for token in raw.split_whitespace() {
        let lower = token.to_ascii_lowercase();
        if lower.starts_with("token=")
            || lower.starts_with("secret=")
            || lower.starts_with("api_key=")
            || lower.starts_with("authorization:")
        {
            output.push("[REDACTED]");
            redactions += 1;
        } else {
            output.push(token);
        }
    }
    (output.join(" "), redactions)
}

pub fn sanitize_tool_output(raw: &str) -> Result<SanitizedToolOutput, McpRuntimeError> {
    if raw.len() > MAX_RAW_TOOL_OUTPUT_BYTES {
        return Err(McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::PayloadTooLarge,
                "raw tool output exceeds limit",
            ),
        });
    }
    let (mut text, redaction_count) = redact_authority_tokens(raw);
    let truncated = text.len() > MAX_SANITIZED_TOOL_OUTPUT_BYTES;
    if truncated {
        text.truncate(MAX_SANITIZED_TOOL_OUTPUT_BYTES);
    }
    Ok(SanitizedToolOutput {
        text,
        truncated,
        redaction_count,
    })
}

#[must_use]
pub fn can_retry_tool_call(read_only: bool, idempotent: bool, side_effecting: bool) -> bool {
    !side_effecting && (read_only || idempotent)
}

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BundleRegistry {
    pub active_revision: Option<String>,
    pub pending_revision: Option<String>,
}

impl BundleRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn begin_activation(&mut self, revision: impl Into<String>) {
        self.pending_revision = Some(revision.into());
    }

    pub fn commit_pending(
        &mut self,
        validation_passed: bool,
    ) -> Result<Option<String>, McpRuntimeError> {
        if !validation_passed {
            self.pending_revision = None;
            return Err(McpRuntimeError {
                typed_error: TypedError::new(
                    McpRuntimeErrorCode::BundleActivationFailed,
                    "bundle validation failed",
                ),
            });
        }
        self.active_revision = self.pending_revision.take();
        Ok(self.active_revision.clone())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum McpTaskState {
    Pending,
    Running,
    Cancelled,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct McpTaskRecord {
    pub task_id: String,
    pub state: McpTaskState,
}

#[derive(Debug, Default)]
pub struct McpTaskRegistry {
    tasks: BTreeMap<String, McpTaskRecord>,
}

impl McpTaskRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn create_task(&mut self, task_id: impl Into<String>) -> McpTaskRecord {
        let task_id = task_id.into();
        let record = McpTaskRecord {
            task_id: task_id.clone(),
            state: McpTaskState::Pending,
        };
        self.tasks.insert(task_id, record.clone());
        record
    }

    pub fn cancel_task(&mut self, task_id: &str) -> Result<McpTaskRecord, McpRuntimeError> {
        let record = self.tasks.get_mut(task_id).ok_or_else(|| McpRuntimeError {
            typed_error: TypedError::new(
                McpRuntimeErrorCode::Cancelled,
                "task is unknown or already gone",
            ),
        })?;
        if matches!(
            record.state,
            McpTaskState::Completed | McpTaskState::Failed | McpTaskState::Cancelled
        ) {
            return Err(McpRuntimeError {
                typed_error: TypedError::new(
                    McpRuntimeErrorCode::Cancelled,
                    "terminal task cannot be cancelled",
                ),
            });
        }
        record.state = McpTaskState::Cancelled;
        Ok(record.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matching_request(required_features: Vec<String>) -> CompatibilityCheckRequest {
        CompatibilityCheckRequest {
            client_version: MIN_CLIENT_VERSION.to_owned(),
            expected_component: COMPONENT_ID.to_owned(),
            expected_protocol_version: PROTOCOL_VERSION.to_owned(),
            expected_schema_hash: SCHEMA_HASH.to_owned(),
            expected_error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
            required_features,
        }
    }

    #[test]
    fn version_contains_required_handshake_fields_and_features() {
        let version = VersionInfo::current();

        assert_eq!(version.component, COMPONENT_ID);
        assert_eq!(version.protocol_version, PROTOCOL_VERSION);
        assert_eq!(version.schema_hash, SCHEMA_HASH);
        assert_eq!(version.error_code_table_hash, ERROR_CODE_TABLE_HASH);
        assert_eq!(version.min_client_version, MIN_CLIENT_VERSION);
        assert_eq!(version.max_client_version, MAX_CLIENT_VERSION);

        for feature in [
            FEATURE_HEALTH,
            FEATURE_READINESS,
            FEATURE_VERSION,
            FEATURE_COMPATIBILITY_HANDSHAKE,
        ] {
            assert!(version.supports(feature), "missing feature {feature}");
        }
        assert!(!version.supports(FEATURE_MCP_TASKS));
        assert!(!version.supports(FEATURE_REMOTE_CANCEL));
    }

    #[test]
    fn readiness_is_not_ready_until_compatibility_handshake_passes() {
        let before = readiness(false);
        assert_eq!(before.state, ReadinessState::NotReady);
        assert!(!before.compatibility_handshake_passed);
        assert_eq!(
            before.error.as_ref().map(|error| error.code.as_str()),
            Some("mcp_runtime_compatibility_handshake_required")
        );

        let after = readiness(true);
        assert_eq!(after.state, ReadinessState::Ready);
        assert!(after.compatibility_handshake_passed);
        assert_eq!(after.error, None);
    }

    #[test]
    fn health_reports_serving_version_without_python_runtime() {
        let response = health();

        assert_eq!(response.state, HealthState::Serving);
        assert_eq!(response.version.component, COMPONENT_ID);
        assert_eq!(response.version.protocol_version, PROTOCOL_VERSION);
    }

    #[test]
    fn compatibility_check_rejects_protocol_mismatch() {
        let mut request = matching_request(vec![FEATURE_VERSION.to_owned()]);
        request.expected_protocol_version = "external-mcp-2025-11-25".to_owned();

        let response = check_compatibility(&request);

        assert!(!response.compatible);
        assert_eq!(response.missing_features, Vec::<String>::new());
        assert_eq!(
            response.error.as_ref().map(|error| error.code.as_str()),
            Some("mcp_runtime_protocol_incompatible")
        );
    }

    #[test]
    fn compatibility_check_covers_all_mismatch_reasons_and_success() {
        for mutate in [
            |request: &mut CompatibilityCheckRequest| {
                request.expected_component = "wrong-component".to_owned();
            },
            |request: &mut CompatibilityCheckRequest| {
                request.expected_schema_hash = "wrong-schema".to_owned();
            },
            |request: &mut CompatibilityCheckRequest| {
                request.expected_error_code_table_hash = "wrong-error-table".to_owned();
            },
        ] {
            let mut request = matching_request(vec![FEATURE_VERSION.to_owned()]);
            mutate(&mut request);
            let response = check_compatibility(&request);
            assert!(!response.compatible);
            assert_eq!(
                response.error.as_ref().map(|error| error.code.as_str()),
                Some("mcp_runtime_protocol_incompatible")
            );
        }

        let response = check_compatibility(&matching_request(vec![FEATURE_VERSION.to_owned()]));
        assert!(response.compatible);
        assert!(response.error.is_none());
        assert!(response.missing_features.is_empty());
    }

    #[test]
    fn compatibility_check_reports_missing_features() {
        let request = matching_request(vec![FEATURE_MCP_TASKS.to_owned()]);
        let response = check_compatibility(&request);

        assert!(!response.compatible);
        assert_eq!(
            response.missing_features,
            vec![FEATURE_MCP_TASKS.to_owned()]
        );
        assert_eq!(
            response.error.as_ref().map(|error| error.category),
            Some(ErrorCategory::Compatibility)
        );
    }

    #[test]
    fn typed_error_table_covers_all_stable_codes() {
        let expected = [
            (
                McpRuntimeErrorCode::ConfigurationInvalid,
                "mcp_runtime_configuration_invalid",
                ErrorCategory::Configuration,
                false,
            ),
            (
                McpRuntimeErrorCode::ProtocolIncompatible,
                "mcp_runtime_protocol_incompatible",
                ErrorCategory::Compatibility,
                false,
            ),
            (
                McpRuntimeErrorCode::CompatibilityHandshakeRequired,
                "mcp_runtime_compatibility_handshake_required",
                ErrorCategory::Compatibility,
                false,
            ),
            (
                McpRuntimeErrorCode::IdentityMismatch,
                "mcp_runtime_identity_mismatch",
                ErrorCategory::Security,
                false,
            ),
            (
                McpRuntimeErrorCode::PublicBindDenied,
                "mcp_runtime_public_bind_denied",
                ErrorCategory::Security,
                false,
            ),
            (
                McpRuntimeErrorCode::EndpointNotAllowlisted,
                "mcp_runtime_endpoint_not_allowlisted",
                ErrorCategory::Security,
                false,
            ),
            (
                McpRuntimeErrorCode::QueueFull,
                "mcp_runtime_queue_full",
                ErrorCategory::ResourceLimit,
                true,
            ),
            (
                McpRuntimeErrorCode::PerServerConcurrencyExceeded,
                "mcp_runtime_per_server_concurrency_exceeded",
                ErrorCategory::ResourceLimit,
                true,
            ),
            (
                McpRuntimeErrorCode::DeadlineExceeded,
                "mcp_runtime_deadline_exceeded",
                ErrorCategory::ResourceLimit,
                true,
            ),
            (
                McpRuntimeErrorCode::PayloadTooLarge,
                "mcp_runtime_payload_too_large",
                ErrorCategory::ResourceLimit,
                false,
            ),
            (
                McpRuntimeErrorCode::StreamIdleTimeout,
                "mcp_runtime_stream_idle_timeout",
                ErrorCategory::ResourceLimit,
                true,
            ),
            (
                McpRuntimeErrorCode::JsonRpcInvalid,
                "mcp_runtime_json_rpc_invalid",
                ErrorCategory::Protocol,
                false,
            ),
            (
                McpRuntimeErrorCode::SchemaValidationFailed,
                "mcp_runtime_schema_validation_failed",
                ErrorCategory::Protocol,
                false,
            ),
            (
                McpRuntimeErrorCode::OutputSanitizationFailed,
                "mcp_runtime_output_sanitization_failed",
                ErrorCategory::Protocol,
                false,
            ),
            (
                McpRuntimeErrorCode::BundleActivationFailed,
                "mcp_runtime_bundle_activation_failed",
                ErrorCategory::Protocol,
                false,
            ),
            (
                McpRuntimeErrorCode::RemoteServerError,
                "mcp_runtime_remote_server_error",
                ErrorCategory::Upstream,
                true,
            ),
            (
                McpRuntimeErrorCode::Cancelled,
                "mcp_runtime_cancelled",
                ErrorCategory::Cancellation,
                false,
            ),
            (
                McpRuntimeErrorCode::Internal,
                "mcp_runtime_internal",
                ErrorCategory::Internal,
                false,
            ),
        ];

        for (code, stable_code, category, retriable) in expected {
            let error = TypedError::new(code, "message");
            assert_eq!(error.code, stable_code);
            assert_eq!(error.category, category);
            assert_eq!(error.retriable, retriable);
        }

        let runtime_error = McpRuntimeError::from(McpRuntimeErrorCode::Internal);
        assert_eq!(runtime_error.typed_error.code, "mcp_runtime_internal");
    }

    #[test]
    fn typed_error_codes_are_stable_prefixed_and_categorized() {
        let error = TypedError::new(McpRuntimeErrorCode::QueueFull, "queue full")
            .with_safe_metadata("component", COMPONENT_ID);

        assert_eq!(error.code, "mcp_runtime_queue_full");
        assert!(error.retriable);
        assert_eq!(error.category, ErrorCategory::ResourceLimit);
        assert_eq!(
            error.safe_metadata.get("component").map(String::as_str),
            Some(COMPONENT_ID)
        );
    }

    #[test]
    fn external_mcp_context_separates_external_protocol_from_sidecar_protocol() {
        let context = ExternalMcpContext {
            external_protocol_version: "2025-11-25".to_owned(),
            server_capabilities: BTreeMap::new(),
            tool_execution_task_support: BTreeMap::new(),
            session_id_fingerprint: Some("session-fp".to_owned()),
            last_event_id_fingerprint: Some("last-event-fp".to_owned()),
            progress_token_fingerprint: Some("progress-fp".to_owned()),
            task_safe_ref: Some("task-ref".to_owned()),
            remote_error_code: Some("remote-error".to_owned()),
            json_rpc_id_correlation: Some("json-rpc-id-fp".to_owned()),
        };

        assert_eq!(context.external_protocol_version, "2025-11-25");
        assert_ne!(context.external_protocol_version, PROTOCOL_VERSION);
    }

    #[test]
    fn json_rpc_request_validation_fails_closed_on_malformed_payload() {
        let invalid = validate_json_rpc_request(br#"{"jsonrpc":"2.0","result":{}}"#)
            .expect_err("request carrying result must fail");
        assert_eq!(invalid.typed_error.code, "mcp_runtime_json_rpc_invalid");

        for payload in [
            vec![b'x'; MAX_JSON_RPC_BYTES + 1],
            b"{not-json".to_vec(),
            br#"{"jsonrpc":"1.0","method":"tools/call"}"#.to_vec(),
            br#"{"jsonrpc":"2.0","method":"   "}"#.to_vec(),
        ] {
            let error =
                validate_json_rpc_request(&payload).expect_err("bad JSON-RPC must fail closed");
            assert!(
                error.typed_error.code == "mcp_runtime_json_rpc_invalid"
                    || error.typed_error.code == "mcp_runtime_payload_too_large"
            );
        }

        let valid = validate_json_rpc_request(
            br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}"#,
        )
        .expect("valid request");
        assert_eq!(valid.method, "tools/call");
    }

    #[test]
    fn tool_output_sanitizer_redacts_authority_tokens() {
        let sanitized = sanitize_tool_output("hello token=abc secret=def api_key=ghi world")
            .expect("sanitize output");
        assert_eq!(sanitized.redaction_count, 3);
        assert!(!sanitized.text.contains("abc"));
        assert!(sanitized.text.contains("[REDACTED]"));
    }

    #[test]
    fn tool_output_sanitizer_enforces_size_limits_and_truncation() {
        let oversized = "x".repeat(MAX_RAW_TOOL_OUTPUT_BYTES + 1);
        let error = sanitize_tool_output(&oversized).expect_err("raw output limit");
        assert_eq!(error.typed_error.code, "mcp_runtime_payload_too_large");

        let truncated_input = format!(
            "{} token=secret",
            "x".repeat(MAX_SANITIZED_TOOL_OUTPUT_BYTES)
        );
        let sanitized = sanitize_tool_output(&truncated_input).expect("sanitize truncated output");
        assert!(sanitized.truncated);
        assert_eq!(sanitized.text.len(), MAX_SANITIZED_TOOL_OUTPUT_BYTES);
        assert_eq!(sanitized.redaction_count, 1);
    }

    #[test]
    fn side_effecting_tool_calls_are_not_retried_by_default() {
        assert!(can_retry_tool_call(true, false, false));
        assert!(can_retry_tool_call(false, true, false));
        assert!(!can_retry_tool_call(true, true, true));
        assert!(!can_retry_tool_call(false, false, false));
    }

    #[test]
    fn bundle_activation_failure_preserves_active_revision() {
        let mut registry = BundleRegistry::new();
        registry.begin_activation("rev-1");
        assert_eq!(
            registry.commit_pending(true).expect("commit"),
            Some("rev-1".to_owned())
        );
        registry.begin_activation("bad-rev");
        assert!(registry.commit_pending(false).is_err());
        assert_eq!(registry.active_revision, Some("rev-1".to_owned()));
        assert_eq!(registry.pending_revision, None);
    }

    #[test]
    fn task_registry_cancels_non_terminal_tasks() {
        let mut registry = McpTaskRegistry::new();
        registry.create_task("task-1");
        let cancelled = registry.cancel_task("task-1").expect("cancel task");
        assert_eq!(cancelled.state, McpTaskState::Cancelled);
    }

    #[test]
    fn task_registry_rejects_unknown_and_terminal_cancellation() {
        let mut registry = McpTaskRegistry::new();
        let unknown = registry
            .cancel_task("missing")
            .expect_err("unknown task cancellation must fail");
        assert_eq!(unknown.typed_error.code, "mcp_runtime_cancelled");

        registry.tasks.insert(
            "completed".to_owned(),
            McpTaskRecord {
                task_id: "completed".to_owned(),
                state: McpTaskState::Completed,
            },
        );
        let terminal = registry
            .cancel_task("completed")
            .expect_err("terminal task cancellation must fail");
        assert_eq!(terminal.typed_error.code, "mcp_runtime_cancelled");
    }

    #[test]
    fn mcp_runtime_contract_artifact_matches_checked_in_copy() {
        let artifact = std::fs::read_to_string(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../../src/integrations/mcp/rust_contracts/mcp_runtime_contract.json"),
        )
        .expect("checked-in mcp runtime contract artifact must exist");
        assert_eq!(
            artifact,
            mcp_runtime_contract_json().expect("serialize mcp runtime contract")
        );
    }
}

//! Phase 1 Rust MCP runtime sidecar contract skeleton.
//!
//! This crate intentionally exposes only stable health, version, readiness,
//! feature, and typed-error structures. Full MCP transport, JSON-RPC, tool
//! execution, task registry, and streaming behavior are reserved for later MCP
//! phases.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

mod contract;
mod error;
mod json_rpc;
mod official_sdk;
mod registry;
mod sanitizer;

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
pub const ADAPTER_PYTHON_LEGACY: &str = "python_legacy";
pub const ADAPTER_OFFICIAL_RUST_SDK: &str = "official_rust_sdk";
pub const OFFICIAL_RUST_SDK_CRATE_NAME: &str = "rmcp";
pub const OFFICIAL_RUST_SDK_DEPENDENCY_MODE: &str = "linked_client_streamable_http_shadow_adapter";
pub const OFFICIAL_RUST_SDK_VERSION_REQUIREMENT: &str = "1.7.0";
pub const OFFICIAL_RUST_SDK_LICENSE: &str = "Apache-2.0";
pub const OFFICIAL_RUST_SDK_METADATA_STATUS: &str =
    "linked_client_streamable_http_shadow_compare_only";
pub const OFFICIAL_RUST_SDK_FEATURE_FLAGS: &[&str] = &[
    "client",
    "transport-streamable-http-client-reqwest",
    "reqwest",
];
pub const OFFICIAL_RUST_SDK_API_MARKERS: &[&str] = &[
    "rmcp::model::CallToolRequestParams",
    "rmcp::transport::StreamableHttpClientTransportConfig",
];
pub const OFFICIAL_RUST_SDK_OPERATIONAL_METHODS: &[&str] = &[
    "initialize",
    "list_tools",
    "call_tool",
    "close",
    "diagnostics",
];
pub const SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION: &str = "negotiated_protocol_version";
pub const SHADOW_FIELD_SERVER_INFO: &str = "server_info";
pub const SHADOW_FIELD_CAPABILITIES: &str = "capabilities";
pub const SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE: &str = "tools_descriptor_shape";
pub const SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE: &str = "safe_tool_call_result_shape";
pub const SHADOW_FIELD_ERROR_CATEGORY: &str = "error_category";
pub const SUPPORTED_MCP_PROTOCOL_VERSIONS: &[&str] =
    &["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];

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

#[must_use]
pub fn task_terminal_states() -> Vec<String> {
    contract::task_terminal_states()
}

#[must_use]
pub fn mcp_runtime_contract_artifact() -> McpRuntimeContractArtifact {
    contract::mcp_runtime_contract_artifact()
}

pub fn mcp_runtime_contract_json() -> Result<String, serde_json::Error> {
    contract::mcp_runtime_contract_json()
}

#[must_use]
pub fn approved_mcp_protocol_versions() -> Vec<String> {
    contract::approved_mcp_protocol_versions()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkAdapterMetadata {
    pub adapter_name: String,
    pub sdk_crate_name: String,
    pub dependency_mode: String,
    pub version_requirement: String,
    pub license: String,
    pub feature_flags: Vec<String>,
    pub api_markers: Vec<String>,
    pub operational_methods: Vec<String>,
    pub compile_time_markers: Vec<String>,
    pub metadata_status: String,
    pub expands_approved_protocol_versions: bool,
    pub approved_mcp_protocol_versions: Vec<String>,
}

#[must_use]
pub fn official_rust_sdk_compile_time_markers() -> Vec<String> {
    official_sdk::official_rust_sdk_compile_time_markers()
}

#[must_use]
pub fn official_rust_sdk_adapter_metadata() -> OfficialRustSdkAdapterMetadata {
    official_sdk::official_rust_sdk_adapter_metadata()
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkClientConfig {
    pub endpoint: String,
    pub protocol_version: String,
    #[serde(skip_serializing)]
    pub authorization_bearer_token: Option<String>,
    #[serde(skip_serializing)]
    pub custom_headers: BTreeMap<String, String>,
    pub allow_stateless: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkToolDescriptor {
    pub name: String,
    pub title: Option<String>,
    pub description: Option<String>,
    pub input_schema_keys: Vec<String>,
    pub output_schema_present: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkToolCallResult {
    pub is_error: bool,
    pub content_count: usize,
    pub structured_content_present: bool,
    pub text: SanitizedToolOutput,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkDiagnostics {
    pub adapter_name: String,
    pub transport: String,
    pub endpoint_fingerprint: String,
    pub negotiated_protocol_version: Option<String>,
    pub server_name: Option<String>,
    pub server_version: Option<String>,
    pub server_capabilities_shape: Vec<String>,
    pub closed: bool,
}

#[derive(Debug)]
pub struct OfficialRustSdkClientSession {
    service: rmcp::service::RunningService<rmcp::RoleClient, rmcp::model::ClientInfo>,
    transport: String,
    endpoint_fingerprint: String,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct OfficialRustSdkAdapter;

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
    contract::health()
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
    contract::readiness(compatibility_handshake_passed)
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
    contract::check_compatibility(request)
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: ErrorCategory,
    pub safe_metadata: BTreeMap<String, String>,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{typed_error:?}")]
pub struct McpRuntimeError {
    pub typed_error: TypedError,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NormalizedToolDescriptorShape {
    pub name: String,
    pub input_schema_fingerprint: Option<String>,
    pub output_schema_fingerprint: Option<String>,
    pub annotations: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SafeToolCallResultShape {
    pub content_kinds: Vec<String>,
    pub is_error: bool,
    pub structured_content_fingerprint: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NormalizedMcpAdapterSnapshot {
    pub adapter_name: String,
    pub negotiated_protocol_version: Option<String>,
    pub server_info: BTreeMap<String, String>,
    pub capabilities: BTreeMap<String, String>,
    pub tool_descriptor_shapes: Vec<NormalizedToolDescriptorShape>,
    pub safe_tool_call_result_shape: Option<SafeToolCallResultShape>,
    pub error_category: Option<ErrorCategory>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ShadowCompareStatus {
    Matched,
    Mismatched,
    Skipped,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShadowRedactionEvidence {
    pub header_values: String,
    pub raw_payload: String,
    pub raw_error: String,
    pub server_ref: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkShadowCompareRequest {
    pub server_ref: String,
    pub transport: String,
    pub visible: NormalizedMcpAdapterSnapshot,
    pub shadow: Option<NormalizedMcpAdapterSnapshot>,
    pub skip_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OfficialRustSdkShadowCompareEvidence {
    pub server_fingerprint: String,
    pub visible_adapter: String,
    pub shadow_adapter: String,
    pub protocol_version: Option<String>,
    pub transport: String,
    pub status: ShadowCompareStatus,
    pub compared_fields: Vec<String>,
    pub mismatched_fields: Vec<String>,
    pub skip_reason: Option<String>,
    pub redaction: ShadowRedactionEvidence,
    pub error: Option<TypedError>,
    pub adapter_metadata: OfficialRustSdkAdapterMetadata,
    pub approved_mcp_protocol_versions: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OfficialRustSdkErrorKind {
    InitializeFailed,
    UnsupportedCombination,
    InvalidJsonRpc,
    SchemaValidationFailed,
    RemoteServerError,
    DeadlineExceeded,
    Cancelled,
    Internal,
}

#[must_use]
pub fn map_official_rust_sdk_error(
    kind: OfficialRustSdkErrorKind,
    raw_message: &str,
) -> TypedError {
    official_sdk::map_official_rust_sdk_error(kind, raw_message)
}

#[must_use]
pub fn compare_official_rust_sdk_shadow(
    request: OfficialRustSdkShadowCompareRequest,
) -> OfficialRustSdkShadowCompareEvidence {
    official_sdk::compare_official_rust_sdk_shadow(request)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequestEnvelope {
    pub jsonrpc: String,
    pub method: String,
    pub id: Option<serde_json::Value>,
    pub params: Option<serde_json::Value>,
}

pub const MAX_JSON_RPC_BYTES: usize = 1024 * 1024;
pub const MAX_RAW_TOOL_OUTPUT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_SANITIZED_TOOL_OUTPUT_BYTES: usize = 4 * 1024 * 1024;

pub fn validate_json_rpc_request(
    payload: &[u8],
) -> Result<JsonRpcRequestEnvelope, McpRuntimeError> {
    json_rpc::validate_json_rpc_request_impl(payload)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SanitizedToolOutput {
    pub text: String,
    pub truncated: bool,
    pub redaction_count: usize,
}

pub fn sanitize_tool_output(raw: &str) -> Result<SanitizedToolOutput, McpRuntimeError> {
    sanitizer::sanitize_tool_output_impl(raw)
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

    fn normalized_snapshot(
        adapter_name: &str,
        version: &str,
        capability_value: &str,
    ) -> NormalizedMcpAdapterSnapshot {
        let mut server_info = BTreeMap::new();
        server_info.insert("name".to_owned(), "fixture-server".to_owned());
        server_info.insert("version".to_owned(), "1.0.0".to_owned());
        let mut capabilities = BTreeMap::new();
        capabilities.insert("tools".to_owned(), capability_value.to_owned());
        NormalizedMcpAdapterSnapshot {
            adapter_name: adapter_name.to_owned(),
            negotiated_protocol_version: Some(version.to_owned()),
            server_info,
            capabilities,
            tool_descriptor_shapes: vec![NormalizedToolDescriptorShape {
                name: "echo".to_owned(),
                input_schema_fingerprint: Some("sha256:input".to_owned()),
                output_schema_fingerprint: None,
                annotations: vec!["readOnlyHint".to_owned()],
            }],
            safe_tool_call_result_shape: Some(SafeToolCallResultShape {
                content_kinds: vec!["text".to_owned()],
                is_error: false,
                structured_content_fingerprint: None,
            }),
            error_category: None,
        }
    }

    fn spawn_fake_rmcp_streamable_http_server(
        protocol_version: &'static str,
    ) -> (String, std::thread::JoinHandle<()>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind fake server");
        listener
            .set_nonblocking(true)
            .expect("fake server nonblocking");
        let endpoint = format!("http://{}/mcp", listener.local_addr().expect("local addr"));
        let handle = std::thread::spawn(move || {
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            let mut saw_call_tool = false;
            let mut saw_delete = false;
            while std::time::Instant::now() < deadline && !(saw_call_tool && saw_delete) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let request = read_http_request(&mut stream);
                        let method = request.request_line.split_whitespace().next().unwrap_or("");
                        if method == "GET" {
                            write_http_response(&mut stream, 405, &[], "");
                            continue;
                        }
                        if method == "DELETE" {
                            saw_delete = true;
                            write_http_response(&mut stream, 200, &[], "");
                            continue;
                        }
                        let payload: serde_json::Value =
                            serde_json::from_slice(&request.body).unwrap_or_default();
                        let jsonrpc_method = payload
                            .get("method")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("");
                        let id = payload
                            .get("id")
                            .cloned()
                            .unwrap_or(serde_json::Value::Null);
                        match jsonrpc_method {
                            "initialize" => {
                                let body = serde_json::json!({
                                    "jsonrpc": "2.0",
                                    "id": id,
                                    "result": {
                                        "protocolVersion": protocol_version,
                                        "capabilities": {"tools": {}},
                                        "serverInfo": {"name": "fake-rmcp-server", "version": "0.1.0"}
                                    }
                                });
                                write_http_response(
                                    &mut stream,
                                    200,
                                    &[("mcp-session-id", "fake-session")],
                                    &body.to_string(),
                                );
                            }
                            "notifications/initialized" => {
                                write_http_response(&mut stream, 202, &[], "");
                            }
                            "tools/list" => {
                                let body = serde_json::json!({
                                    "jsonrpc": "2.0",
                                    "id": id,
                                    "result": {
                                        "tools": [{
                                            "name": "echo",
                                            "description": "Echo test tool",
                                            "inputSchema": {
                                                "type": "object",
                                                "properties": {},
                                                "additionalProperties": true
                                            }
                                        }]
                                    }
                                });
                                write_http_response(&mut stream, 200, &[], &body.to_string());
                            }
                            "tools/call" => {
                                saw_call_tool = true;
                                let body = serde_json::json!({
                                    "jsonrpc": "2.0",
                                    "id": id,
                                    "result": {
                                        "content": [{"type": "text", "text": "echo ok"}],
                                        "isError": false
                                    }
                                });
                                write_http_response(&mut stream, 200, &[], &body.to_string());
                            }
                            _ => {
                                let body = serde_json::json!({
                                    "jsonrpc": "2.0",
                                    "id": id,
                                    "error": {"code": -32601, "message": "method not found"}
                                });
                                write_http_response(&mut stream, 200, &[], &body.to_string());
                            }
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(std::time::Duration::from_millis(10));
                    }
                    Err(error) => panic!("fake server accept failed: {error}"),
                }
            }
            assert!(saw_call_tool, "fake server observed tools/call");
        });
        (endpoint, handle)
    }

    struct HttpRequest {
        request_line: String,
        body: Vec<u8>,
    }

    fn read_http_request(stream: &mut std::net::TcpStream) -> HttpRequest {
        use std::io::Read as _;
        stream
            .set_read_timeout(Some(std::time::Duration::from_secs(2)))
            .expect("set fake server read timeout");
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 1024];
        let header_end = loop {
            let bytes_read = stream.read(&mut chunk).expect("read fake request");
            assert_ne!(bytes_read, 0, "client closed before headers");
            buffer.extend_from_slice(&chunk[..bytes_read]);
            if let Some(pos) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
                break pos + 4;
            }
        };
        let header_text = String::from_utf8_lossy(&buffer[..header_end]);
        let request_line = header_text.lines().next().unwrap_or("").to_owned();
        let content_length = header_text
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            })
            .unwrap_or(0);
        while buffer.len() < header_end + content_length {
            let bytes_read = stream.read(&mut chunk).expect("read fake body");
            assert_ne!(bytes_read, 0, "client closed before body");
            buffer.extend_from_slice(&chunk[..bytes_read]);
        }
        HttpRequest {
            request_line,
            body: buffer[header_end..header_end + content_length].to_vec(),
        }
    }

    fn write_http_response(
        stream: &mut std::net::TcpStream,
        status: u16,
        extra_headers: &[(&str, &str)],
        body: &str,
    ) {
        use std::io::Write as _;
        let reason = match status {
            200 => "OK",
            202 => "Accepted",
            405 => "Method Not Allowed",
            _ => "OK",
        };
        let mut response = format!(
            "HTTP/1.1 {status} {reason}\r\ncontent-length: {}\r\nconnection: close\r\n",
            body.len()
        );
        if !body.is_empty() {
            response.push_str("content-type: application/json\r\n");
        }
        for (name, value) in extra_headers {
            response.push_str(name);
            response.push_str(": ");
            response.push_str(value);
            response.push_str("\r\n");
        }
        response.push_str("\r\n");
        response.push_str(body);
        stream
            .write_all(response.as_bytes())
            .expect("write fake response");
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
    fn official_rust_sdk_metadata_is_traceable_without_expanding_approved_versions() {
        let metadata = official_rust_sdk_adapter_metadata();
        let adapter_metadata = OfficialRustSdkAdapter.metadata();

        assert_eq!(metadata.adapter_name, ADAPTER_OFFICIAL_RUST_SDK);
        assert_eq!(adapter_metadata, metadata);
        assert_eq!(metadata.sdk_crate_name, OFFICIAL_RUST_SDK_CRATE_NAME);
        assert_eq!(
            metadata.dependency_mode,
            "linked_client_streamable_http_shadow_adapter"
        );
        assert_eq!(metadata.version_requirement, "1.7.0");
        assert_eq!(metadata.license, "Apache-2.0");
        assert_eq!(
            metadata.feature_flags,
            vec![
                "client".to_owned(),
                "transport-streamable-http-client-reqwest".to_owned(),
                "reqwest".to_owned(),
            ]
        );
        assert_eq!(
            metadata.metadata_status,
            "linked_client_streamable_http_shadow_compare_only"
        );
        assert!(
            metadata
                .api_markers
                .contains(&"rmcp::model::CallToolRequestParams".to_owned())
        );
        assert!(
            metadata
                .api_markers
                .contains(&"rmcp::transport::StreamableHttpClientTransportConfig".to_owned())
        );
        assert_eq!(
            metadata.operational_methods,
            vec![
                "initialize".to_owned(),
                "list_tools".to_owned(),
                "call_tool".to_owned(),
                "close".to_owned(),
                "diagnostics".to_owned(),
            ]
        );
        assert!(
            metadata
                .compile_time_markers
                .iter()
                .any(|marker| marker.contains("CallToolRequestParams"))
        );
        assert!(
            metadata
                .compile_time_markers
                .iter()
                .any(|marker| marker.contains("StreamableHttpClientTransportConfig"))
        );
        assert!(!metadata.expands_approved_protocol_versions);
        assert_eq!(
            metadata.approved_mcp_protocol_versions,
            vec![
                "2024-11-05".to_owned(),
                "2025-03-26".to_owned(),
                "2025-06-18".to_owned(),
                "2025-11-25".to_owned(),
            ]
        );
        assert_eq!(
            approved_mcp_protocol_versions(),
            metadata.approved_mcp_protocol_versions
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn official_rust_sdk_adapter_executes_streamable_http_list_call_and_close() {
        for protocol_version in ["2025-03-26", "2025-06-18", "2025-11-25"] {
            let (endpoint, handle) = spawn_fake_rmcp_streamable_http_server(protocol_version);
            let config = OfficialRustSdkClientConfig::streamable_http(endpoint, protocol_version)
                .with_custom_header("x-maf-test", "ok");

            let mut session = OfficialRustSdkAdapter
                .initialize(config)
                .await
                .expect("official SDK initializes against fake Streamable HTTP server");
            let diagnostics = session.diagnostics();

            assert_eq!(
                diagnostics.negotiated_protocol_version.as_deref(),
                Some(protocol_version)
            );
            assert_eq!(diagnostics.server_name.as_deref(), Some("fake-rmcp-server"));
            assert_eq!(diagnostics.server_version.as_deref(), Some("0.1.0"));
            assert!(
                diagnostics
                    .server_capabilities_shape
                    .contains(&"tools".to_owned())
            );

            let tools = session
                .list_tools()
                .await
                .expect("official SDK lists tools");
            assert_eq!(tools.len(), 1);
            assert_eq!(tools[0].name, "echo");
            assert!(tools[0].input_schema_keys.contains(&"type".to_owned()));

            let result = session
                .call_tool("echo", serde_json::Map::new())
                .await
                .expect("official SDK calls a tool");
            assert!(!result.is_error);
            assert_eq!(result.content_count, 1);
            assert_eq!(result.text.text, "echo ok");

            session.close().await.expect("official SDK session closes");
            handle.join().expect("fake server thread joins");
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn official_rust_sdk_streamable_http_rejects_2024_legacy_protocol() {
        let error = OfficialRustSdkAdapter
            .initialize(OfficialRustSdkClientConfig::streamable_http(
                "http://127.0.0.1:9/mcp",
                "2024-11-05",
            ))
            .await
            .expect_err("2024 legacy HTTP+SSE must not enter the Streamable HTTP SDK adapter");

        assert_eq!(error.code, "mcp_runtime_protocol_incompatible");
        assert!(error.message.contains("2024-11-05"));
        assert!(error.message.contains("legacy HTTP+SSE"));
        assert_eq!(error.category, ErrorCategory::Compatibility);
    }

    #[test]
    fn official_rust_sdk_client_config_redacts_bearer_token_from_debug_and_json() {
        let config = OfficialRustSdkClientConfig::streamable_http(
            "https://example.invalid/mcp",
            "2025-11-25",
        )
        .with_bearer_token("secret-token")
        .with_custom_header("x-api-key", "secret-header-value");
        let debug = format!("{config:?}");
        let serialized = serde_json::to_string(&config).expect("serialize SDK config");

        assert!(debug.contains("[REDACTED]"));
        assert!(debug.contains("x-api-key"));
        assert!(!debug.contains("secret-token"));
        assert!(!debug.contains("secret-header-value"));
        assert!(!serialized.contains("secret-token"));
        assert!(!serialized.contains("secret-header-value"));
        assert!(!serialized.contains("authorization_bearer_token"));
        assert!(!serialized.contains("custom_headers"));
    }

    #[test]
    fn official_rust_sdk_shadow_compare_records_matched_evidence() {
        let visible = normalized_snapshot(ADAPTER_PYTHON_LEGACY, "2025-11-25", "list+call");
        let shadow = normalized_snapshot(ADAPTER_OFFICIAL_RUST_SDK, "2025-11-25", "list+call");
        let evidence = compare_official_rust_sdk_shadow(OfficialRustSdkShadowCompareRequest {
            server_ref: "https://example.invalid/mcp?token=secret".to_owned(),
            transport: "streamable_http".to_owned(),
            visible,
            shadow: Some(shadow),
            skip_reason: None,
        });

        assert_eq!(evidence.status, ShadowCompareStatus::Matched);
        assert_eq!(evidence.mismatched_fields, Vec::<String>::new());
        assert_eq!(evidence.error, None);
        assert_eq!(evidence.visible_adapter, ADAPTER_PYTHON_LEGACY);
        assert_eq!(evidence.shadow_adapter, ADAPTER_OFFICIAL_RUST_SDK);
        assert_eq!(
            evidence.compared_fields,
            vec![
                SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION.to_owned(),
                SHADOW_FIELD_SERVER_INFO.to_owned(),
                SHADOW_FIELD_CAPABILITIES.to_owned(),
                SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE.to_owned(),
                SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE.to_owned(),
                SHADOW_FIELD_ERROR_CATEGORY.to_owned(),
            ]
        );
    }

    #[test]
    fn official_rust_sdk_shadow_compare_records_mismatched_evidence() {
        let visible = normalized_snapshot(ADAPTER_PYTHON_LEGACY, "2024-11-05", "list+call");
        let shadow = normalized_snapshot(ADAPTER_OFFICIAL_RUST_SDK, "2024-11-05", "list-only");
        let evidence = compare_official_rust_sdk_shadow(OfficialRustSdkShadowCompareRequest {
            server_ref: "fixture-server".to_owned(),
            transport: "legacy_http_sse".to_owned(),
            visible,
            shadow: Some(shadow),
            skip_reason: None,
        });

        assert_eq!(evidence.status, ShadowCompareStatus::Mismatched);
        assert_eq!(
            evidence.mismatched_fields,
            vec![SHADOW_FIELD_CAPABILITIES.to_owned()]
        );
        let error = evidence.error.expect("mismatch has typed error");
        assert_eq!(error.code, "mcp_runtime_protocol_incompatible");
        assert_eq!(error.category, ErrorCategory::Compatibility);
        assert_eq!(
            error.safe_metadata.get("adapter").map(String::as_str),
            Some(ADAPTER_OFFICIAL_RUST_SDK)
        );
    }

    #[test]
    fn official_rust_sdk_shadow_compare_records_skipped_evidence() {
        let visible = normalized_snapshot(ADAPTER_PYTHON_LEGACY, "2025-03-26", "list+call");
        let evidence = compare_official_rust_sdk_shadow(OfficialRustSdkShadowCompareRequest {
            server_ref: "fixture-server".to_owned(),
            transport: "streamable_http".to_owned(),
            visible,
            shadow: None,
            skip_reason: Some("sdk_unsupported_combination".to_owned()),
        });

        assert_eq!(evidence.status, ShadowCompareStatus::Skipped);
        assert_eq!(
            evidence.skip_reason.as_deref(),
            Some("sdk_unsupported_combination")
        );
        assert_eq!(evidence.mismatched_fields, Vec::<String>::new());
        assert_eq!(
            evidence.error.as_ref().map(|error| error.code.as_str()),
            Some("mcp_runtime_protocol_incompatible")
        );
        assert_eq!(evidence.shadow_adapter, ADAPTER_OFFICIAL_RUST_SDK);
    }

    #[test]
    fn official_rust_sdk_shadow_evidence_redacts_server_ref_and_raw_payloads() {
        let visible = normalized_snapshot(ADAPTER_PYTHON_LEGACY, "2025-06-18", "list+call");
        let shadow = normalized_snapshot(ADAPTER_OFFICIAL_RUST_SDK, "2025-06-18", "list+call");
        let evidence = compare_official_rust_sdk_shadow(OfficialRustSdkShadowCompareRequest {
            server_ref: "https://example.invalid/mcp?api_key=secret-value".to_owned(),
            transport: "streamable_http".to_owned(),
            visible,
            shadow: Some(shadow),
            skip_reason: None,
        });
        let serialized = serde_json::to_string(&evidence).expect("serialize evidence");

        assert!(evidence.server_fingerprint.starts_with("fnv1a64:"));
        assert_eq!(evidence.redaction.header_values, "redacted");
        assert_eq!(evidence.redaction.raw_payload, "omitted");
        assert_eq!(evidence.redaction.raw_error, "redacted");
        assert!(!serialized.contains("secret-value"));
        assert!(!serialized.contains("api_key="));
    }

    #[test]
    fn official_rust_sdk_error_mapping_uses_typed_errors_and_redacts_raw_secret() {
        let error = map_official_rust_sdk_error(
            OfficialRustSdkErrorKind::RemoteServerError,
            "upstream failed Authorization: Bearer secret-token token=abc",
        );

        assert_eq!(error.code, "mcp_runtime_remote_server_error");
        assert_eq!(error.category, ErrorCategory::Upstream);
        assert!(error.retriable);
        assert!(error.message.contains("[REDACTED]"));
        assert!(!error.message.contains("secret-token"));
        assert!(!error.message.contains("token=abc"));
        assert_eq!(
            error.safe_metadata.get("sdk_crate").map(String::as_str),
            Some(OFFICIAL_RUST_SDK_CRATE_NAME)
        );

        let unsupported = map_official_rust_sdk_error(
            OfficialRustSdkErrorKind::UnsupportedCombination,
            "unsupported",
        );
        assert_eq!(unsupported.code, "mcp_runtime_protocol_incompatible");
        assert_eq!(unsupported.category, ErrorCategory::Compatibility);
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

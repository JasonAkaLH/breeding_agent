use std::collections::BTreeMap;

use super::contract::{approved_mcp_protocol_versions, string_values};
use super::sanitizer::{
    redact_authority_tokens, sanitize_tool_output_impl as sanitize_tool_output,
};
use super::{
    ADAPTER_OFFICIAL_RUST_SDK, McpRuntimeErrorCode, OFFICIAL_RUST_SDK_API_MARKERS,
    OFFICIAL_RUST_SDK_CRATE_NAME, OFFICIAL_RUST_SDK_DEPENDENCY_MODE,
    OFFICIAL_RUST_SDK_FEATURE_FLAGS, OFFICIAL_RUST_SDK_LICENSE, OFFICIAL_RUST_SDK_METADATA_STATUS,
    OFFICIAL_RUST_SDK_OPERATIONAL_METHODS, OFFICIAL_RUST_SDK_VERSION_REQUIREMENT,
    OfficialRustSdkAdapter, OfficialRustSdkAdapterMetadata, OfficialRustSdkClientConfig,
    OfficialRustSdkClientSession, OfficialRustSdkDiagnostics, OfficialRustSdkErrorKind,
    OfficialRustSdkShadowCompareEvidence, OfficialRustSdkShadowCompareRequest,
    OfficialRustSdkToolCallResult, OfficialRustSdkToolDescriptor, SHADOW_FIELD_CAPABILITIES,
    SHADOW_FIELD_ERROR_CATEGORY, SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION,
    SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE, SHADOW_FIELD_SERVER_INFO,
    SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE, ShadowCompareStatus, ShadowRedactionEvidence, TypedError,
};

pub(super) fn official_rust_sdk_compile_time_markers() -> Vec<String> {
    vec![
        std::any::type_name::<rmcp::model::CallToolRequestParams>().to_owned(),
        std::any::type_name::<
            rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig,
        >()
        .to_owned(),
    ]
}

pub(super) fn official_rust_sdk_adapter_metadata() -> OfficialRustSdkAdapterMetadata {
    OfficialRustSdkAdapterMetadata {
        adapter_name: ADAPTER_OFFICIAL_RUST_SDK.to_owned(),
        sdk_crate_name: OFFICIAL_RUST_SDK_CRATE_NAME.to_owned(),
        dependency_mode: OFFICIAL_RUST_SDK_DEPENDENCY_MODE.to_owned(),
        version_requirement: OFFICIAL_RUST_SDK_VERSION_REQUIREMENT.to_owned(),
        license: OFFICIAL_RUST_SDK_LICENSE.to_owned(),
        feature_flags: string_values(OFFICIAL_RUST_SDK_FEATURE_FLAGS),
        api_markers: string_values(OFFICIAL_RUST_SDK_API_MARKERS),
        operational_methods: string_values(OFFICIAL_RUST_SDK_OPERATIONAL_METHODS),
        compile_time_markers: official_rust_sdk_compile_time_markers(),
        metadata_status: OFFICIAL_RUST_SDK_METADATA_STATUS.to_owned(),
        expands_approved_protocol_versions: false,
        approved_mcp_protocol_versions: approved_mcp_protocol_versions(),
    }
}

impl std::fmt::Debug for OfficialRustSdkClientConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("OfficialRustSdkClientConfig")
            .field("endpoint", &self.endpoint)
            .field("protocol_version", &self.protocol_version)
            .field(
                "authorization_bearer_token",
                &self
                    .authorization_bearer_token
                    .as_ref()
                    .map(|_| "[REDACTED]"),
            )
            .field(
                "custom_header_names",
                &self.custom_headers.keys().collect::<Vec<_>>(),
            )
            .field("allow_stateless", &self.allow_stateless)
            .finish()
    }
}

impl OfficialRustSdkClientConfig {
    #[must_use]
    pub fn streamable_http(
        endpoint: impl Into<String>,
        protocol_version: impl Into<String>,
    ) -> Self {
        Self {
            endpoint: endpoint.into(),
            protocol_version: protocol_version.into(),
            authorization_bearer_token: None,
            custom_headers: BTreeMap::new(),
            allow_stateless: true,
        }
    }

    #[must_use]
    pub fn with_bearer_token(mut self, token: impl Into<String>) -> Self {
        self.authorization_bearer_token = Some(token.into());
        self
    }

    #[must_use]
    pub fn with_custom_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.custom_headers.insert(name.into(), value.into());
        self
    }
}

impl OfficialRustSdkAdapter {
    #[must_use]
    pub fn metadata(self) -> OfficialRustSdkAdapterMetadata {
        official_rust_sdk_adapter_metadata()
    }

    #[must_use]
    pub fn compare_shadow(
        self,
        request: OfficialRustSdkShadowCompareRequest,
    ) -> OfficialRustSdkShadowCompareEvidence {
        compare_official_rust_sdk_shadow(request)
    }

    #[must_use]
    pub fn map_error(self, kind: OfficialRustSdkErrorKind, raw_message: &str) -> TypedError {
        map_official_rust_sdk_error(kind, raw_message)
    }

    pub async fn initialize(
        self,
        config: OfficialRustSdkClientConfig,
    ) -> Result<OfficialRustSdkClientSession, TypedError> {
        use rmcp::{
            ServiceExt as _,
            model::{ClientCapabilities, ClientInfo, Implementation},
            transport::{
                StreamableHttpClientTransport,
                streamable_http_client::StreamableHttpClientTransportConfig,
            },
        };

        let endpoint = config.endpoint.trim();
        if endpoint.is_empty() {
            return Err(map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::InitializeFailed,
                "official Rust SDK endpoint is required",
            ));
        }
        let protocol_version = protocol_version_from_str(&config.protocol_version)?;
        let custom_headers = http_headers_from_config(&config.custom_headers)?;

        let mut transport_config =
            StreamableHttpClientTransportConfig::with_uri(endpoint.to_owned());
        transport_config.allow_stateless = config.allow_stateless;
        transport_config.auth_header = config.authorization_bearer_token;
        transport_config.custom_headers = custom_headers;

        let client_info = ClientInfo::new(
            ClientCapabilities::default(),
            Implementation::new("breeding_agent", env!("CARGO_PKG_VERSION")),
        )
        .with_protocol_version(protocol_version);
        let transport = StreamableHttpClientTransport::from_config(transport_config);
        let service = client_info.serve(transport).await.map_err(|error| {
            map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::InitializeFailed,
                &error.to_string(),
            )
        })?;

        Ok(OfficialRustSdkClientSession {
            service,
            transport: "streamable_http".to_owned(),
            endpoint_fingerprint: stable_fingerprint(endpoint),
        })
    }
}

impl OfficialRustSdkClientSession {
    pub async fn list_tools(&self) -> Result<Vec<OfficialRustSdkToolDescriptor>, TypedError> {
        let tools = self.service.list_all_tools().await.map_err(|error| {
            map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::RemoteServerError,
                &error.to_string(),
            )
        })?;
        Ok(tools.into_iter().map(tool_descriptor_from_rmcp).collect())
    }

    pub async fn call_tool(
        &self,
        name: impl Into<String>,
        arguments: serde_json::Map<String, serde_json::Value>,
    ) -> Result<OfficialRustSdkToolCallResult, TypedError> {
        let params = rmcp::model::CallToolRequestParams::new(name.into()).with_arguments(arguments);
        let result = self.service.call_tool(params).await.map_err(|error| {
            map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::RemoteServerError,
                &error.to_string(),
            )
        })?;
        tool_call_result_from_rmcp(result)
    }

    #[must_use]
    pub fn diagnostics(&self) -> OfficialRustSdkDiagnostics {
        let server_info = self.service.peer_info();
        OfficialRustSdkDiagnostics {
            adapter_name: ADAPTER_OFFICIAL_RUST_SDK.to_owned(),
            transport: self.transport.clone(),
            endpoint_fingerprint: self.endpoint_fingerprint.clone(),
            negotiated_protocol_version: server_info
                .as_ref()
                .map(|info| info.protocol_version.as_str().to_owned()),
            server_name: server_info
                .as_ref()
                .map(|info| info.server_info.name.clone()),
            server_version: server_info
                .as_ref()
                .map(|info| info.server_info.version.clone()),
            server_capabilities_shape: server_info
                .as_ref()
                .map(|info| {
                    json_object_shape(&serde_json::to_value(&info.capabilities).unwrap_or_default())
                })
                .unwrap_or_default(),
            closed: self.service.is_closed(),
        }
    }

    pub async fn close(&mut self) -> Result<(), TypedError> {
        self.service.close().await.map(|_| ()).map_err(|error| {
            map_official_rust_sdk_error(OfficialRustSdkErrorKind::Internal, &error.to_string())
        })
    }
}

fn stable_fingerprint(value: &str) -> String {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in value.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("fnv1a64:{hash:016x}")
}

fn compared_shadow_fields() -> Vec<String> {
    string_values(&[
        SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION,
        SHADOW_FIELD_SERVER_INFO,
        SHADOW_FIELD_CAPABILITIES,
        SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE,
        SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE,
        SHADOW_FIELD_ERROR_CATEGORY,
    ])
}

fn default_shadow_redaction(server_ref: &str) -> ShadowRedactionEvidence {
    ShadowRedactionEvidence {
        header_values: "redacted".to_owned(),
        raw_payload: "omitted".to_owned(),
        raw_error: "redacted".to_owned(),
        server_ref: stable_fingerprint(server_ref),
    }
}

pub(super) fn map_official_rust_sdk_error(
    kind: OfficialRustSdkErrorKind,
    raw_message: &str,
) -> TypedError {
    let (safe_message, _) = redact_authority_tokens(raw_message);
    let (code, default_safe_message) = match kind {
        OfficialRustSdkErrorKind::InitializeFailed => (
            McpRuntimeErrorCode::ProtocolIncompatible,
            "official Rust SDK initialization failed",
        ),
        OfficialRustSdkErrorKind::UnsupportedCombination => (
            McpRuntimeErrorCode::ProtocolIncompatible,
            "official Rust SDK unsupported version or transport",
        ),
        OfficialRustSdkErrorKind::InvalidJsonRpc => (
            McpRuntimeErrorCode::JsonRpcInvalid,
            "official Rust SDK returned invalid JSON-RPC",
        ),
        OfficialRustSdkErrorKind::SchemaValidationFailed => (
            McpRuntimeErrorCode::SchemaValidationFailed,
            "official Rust SDK schema validation failed",
        ),
        OfficialRustSdkErrorKind::RemoteServerError => (
            McpRuntimeErrorCode::RemoteServerError,
            "official Rust SDK remote server error",
        ),
        OfficialRustSdkErrorKind::DeadlineExceeded => (
            McpRuntimeErrorCode::DeadlineExceeded,
            "official Rust SDK deadline exceeded",
        ),
        OfficialRustSdkErrorKind::Cancelled => (
            McpRuntimeErrorCode::Cancelled,
            "official Rust SDK call cancelled",
        ),
        OfficialRustSdkErrorKind::Internal => (
            McpRuntimeErrorCode::Internal,
            "official Rust SDK internal error",
        ),
    };
    let message = if safe_message.trim().is_empty() {
        default_safe_message.to_owned()
    } else {
        safe_message
    };
    TypedError::new(code, message)
        .with_safe_metadata("adapter", ADAPTER_OFFICIAL_RUST_SDK)
        .with_safe_metadata("sdk_crate", OFFICIAL_RUST_SDK_CRATE_NAME)
}

fn protocol_version_from_str(version: &str) -> Result<rmcp::model::ProtocolVersion, TypedError> {
    match version {
        "2024-11-05" => Err(map_official_rust_sdk_error(
            OfficialRustSdkErrorKind::UnsupportedCombination,
            "2024-11-05 requires the legacy HTTP+SSE adapter; the official Rust SDK Streamable HTTP adapter is disabled for this combination",
        )),
        "2025-03-26" => Ok(rmcp::model::ProtocolVersion::V_2025_03_26),
        "2025-06-18" => Ok(rmcp::model::ProtocolVersion::V_2025_06_18),
        "2025-11-25" => Ok(rmcp::model::ProtocolVersion::V_2025_11_25),
        _ => Err(map_official_rust_sdk_error(
            OfficialRustSdkErrorKind::UnsupportedCombination,
            "unsupported MCP protocol version for official Rust SDK adapter",
        )),
    }
}

fn http_headers_from_config(
    headers: &BTreeMap<String, String>,
) -> Result<std::collections::HashMap<http::HeaderName, http::HeaderValue>, TypedError> {
    let mut parsed = std::collections::HashMap::new();
    for (name, value) in headers {
        let header_name = name.parse::<http::HeaderName>().map_err(|_| {
            map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::SchemaValidationFailed,
                "invalid official Rust SDK custom header name",
            )
        })?;
        let header_value = http::HeaderValue::from_str(value).map_err(|_| {
            map_official_rust_sdk_error(
                OfficialRustSdkErrorKind::SchemaValidationFailed,
                "invalid official Rust SDK custom header value",
            )
        })?;
        parsed.insert(header_name, header_value);
    }
    Ok(parsed)
}

fn json_object_shape(value: &serde_json::Value) -> Vec<String> {
    let mut keys = value
        .as_object()
        .map(|object| object.keys().cloned().collect::<Vec<_>>())
        .unwrap_or_default();
    keys.sort();
    keys
}

fn tool_descriptor_from_rmcp(tool: rmcp::model::Tool) -> OfficialRustSdkToolDescriptor {
    let mut input_schema_keys = tool.input_schema.keys().cloned().collect::<Vec<_>>();
    input_schema_keys.sort();
    OfficialRustSdkToolDescriptor {
        name: tool.name.into_owned(),
        title: tool.title,
        description: tool.description.map(std::borrow::Cow::into_owned),
        input_schema_keys,
        output_schema_present: tool.output_schema.is_some(),
    }
}

fn tool_call_result_from_rmcp(
    result: rmcp::model::CallToolResult,
) -> Result<OfficialRustSdkToolCallResult, TypedError> {
    let text = result
        .content
        .iter()
        .filter_map(|content| content.as_text().map(|text| text.text.as_str()))
        .collect::<Vec<_>>()
        .join("\n");
    let sanitized = sanitize_tool_output(&text).map_err(|error| error.typed_error)?;
    Ok(OfficialRustSdkToolCallResult {
        is_error: result.is_error.unwrap_or(false),
        content_count: result.content.len(),
        structured_content_present: result.structured_content.is_some(),
        text: sanitized,
    })
}

pub(super) fn compare_official_rust_sdk_shadow(
    request: OfficialRustSdkShadowCompareRequest,
) -> OfficialRustSdkShadowCompareEvidence {
    let compared_fields = compared_shadow_fields();
    let adapter_metadata = official_rust_sdk_adapter_metadata();
    let protocol_version = request.visible.negotiated_protocol_version.clone();
    let redaction = default_shadow_redaction(&request.server_ref);

    let Some(shadow) = request.shadow else {
        let reason = request
            .skip_reason
            .unwrap_or_else(|| "official Rust SDK shadow adapter unavailable".to_owned());
        return OfficialRustSdkShadowCompareEvidence {
            server_fingerprint: redaction.server_ref.clone(),
            visible_adapter: request.visible.adapter_name,
            shadow_adapter: ADAPTER_OFFICIAL_RUST_SDK.to_owned(),
            protocol_version,
            transport: request.transport,
            status: ShadowCompareStatus::Skipped,
            compared_fields,
            mismatched_fields: Vec::new(),
            skip_reason: Some(reason.clone()),
            redaction,
            error: Some(
                TypedError::new(McpRuntimeErrorCode::ProtocolIncompatible, reason)
                    .with_safe_metadata("adapter", ADAPTER_OFFICIAL_RUST_SDK),
            ),
            approved_mcp_protocol_versions: adapter_metadata.approved_mcp_protocol_versions.clone(),
            adapter_metadata,
        };
    };

    let mut mismatched_fields = Vec::new();
    if request.visible.negotiated_protocol_version != shadow.negotiated_protocol_version {
        mismatched_fields.push(SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION.to_owned());
    }
    if request.visible.server_info != shadow.server_info {
        mismatched_fields.push(SHADOW_FIELD_SERVER_INFO.to_owned());
    }
    if request.visible.capabilities != shadow.capabilities {
        mismatched_fields.push(SHADOW_FIELD_CAPABILITIES.to_owned());
    }
    if request.visible.tool_descriptor_shapes != shadow.tool_descriptor_shapes {
        mismatched_fields.push(SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE.to_owned());
    }
    if request.visible.safe_tool_call_result_shape != shadow.safe_tool_call_result_shape {
        mismatched_fields.push(SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE.to_owned());
    }
    if request.visible.error_category != shadow.error_category {
        mismatched_fields.push(SHADOW_FIELD_ERROR_CATEGORY.to_owned());
    }

    let status = if mismatched_fields.is_empty() {
        ShadowCompareStatus::Matched
    } else {
        ShadowCompareStatus::Mismatched
    };
    let error = (!mismatched_fields.is_empty()).then(|| {
        TypedError::new(
            McpRuntimeErrorCode::ProtocolIncompatible,
            "official Rust SDK shadow compare mismatch",
        )
        .with_safe_metadata("adapter", ADAPTER_OFFICIAL_RUST_SDK)
        .with_safe_metadata("mismatch_count", mismatched_fields.len().to_string())
    });

    OfficialRustSdkShadowCompareEvidence {
        server_fingerprint: redaction.server_ref.clone(),
        visible_adapter: request.visible.adapter_name,
        shadow_adapter: shadow.adapter_name,
        protocol_version,
        transport: request.transport,
        status,
        compared_fields,
        mismatched_fields,
        skip_reason: request.skip_reason,
        redaction,
        error,
        approved_mcp_protocol_versions: adapter_metadata.approved_mcp_protocol_versions.clone(),
        adapter_metadata,
    }
}

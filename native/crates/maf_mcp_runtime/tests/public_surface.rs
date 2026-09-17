use maf_mcp_runtime as mcp;

macro_rules! assert_type {
    ($expected:ty; $($value:expr),+ $(,)?) => {
        $(let _: $expected = $value;)+
    };
}

#[test]
fn crate_root_public_constants_and_free_function_signatures_are_stable() {
    assert_type!(&str;
        mcp::COMPONENT_ID,
        mcp::PROTOCOL_VERSION,
        mcp::SCHEMA_HASH,
        mcp::ERROR_CODE_TABLE_HASH,
        mcp::MIN_CLIENT_VERSION,
        mcp::MAX_CLIENT_VERSION,
        mcp::FEATURE_HEALTH,
        mcp::FEATURE_READINESS,
        mcp::FEATURE_VERSION,
        mcp::FEATURE_COMPATIBILITY_HANDSHAKE,
        mcp::FEATURE_SHORT_CALL,
        mcp::FEATURE_STREAMABLE_HTTP,
        mcp::FEATURE_SSE_STREAM,
        mcp::FEATURE_SERVER_TO_CLIENT_GET,
        mcp::FEATURE_MCP_TASKS,
        mcp::FEATURE_TASK_AUGMENTED_TOOLS_CALL,
        mcp::FEATURE_REMOTE_CANCEL,
        mcp::RELATED_TASK_META_KEY,
        mcp::ADAPTER_PYTHON_LEGACY,
        mcp::ADAPTER_OFFICIAL_RUST_SDK,
        mcp::OFFICIAL_RUST_SDK_CRATE_NAME,
        mcp::OFFICIAL_RUST_SDK_DEPENDENCY_MODE,
        mcp::OFFICIAL_RUST_SDK_VERSION_REQUIREMENT,
        mcp::OFFICIAL_RUST_SDK_LICENSE,
        mcp::OFFICIAL_RUST_SDK_METADATA_STATUS,
        mcp::SHADOW_FIELD_NEGOTIATED_PROTOCOL_VERSION,
        mcp::SHADOW_FIELD_SERVER_INFO,
        mcp::SHADOW_FIELD_CAPABILITIES,
        mcp::SHADOW_FIELD_TOOL_DESCRIPTOR_SHAPE,
        mcp::SHADOW_FIELD_SAFE_TOOL_CALL_RESULT_SHAPE,
        mcp::SHADOW_FIELD_ERROR_CATEGORY,
    );
    assert_type!(&[&str];
        mcp::OFFICIAL_RUST_SDK_FEATURE_FLAGS,
        mcp::OFFICIAL_RUST_SDK_API_MARKERS,
        mcp::OFFICIAL_RUST_SDK_OPERATIONAL_METHODS,
        mcp::SUPPORTED_MCP_PROTOCOL_VERSIONS,
        mcp::SUPPORTED_FEATURES,
    );
    assert_type!(usize;
        mcp::MAX_JSON_RPC_BYTES,
        mcp::MAX_RAW_TOOL_OUTPUT_BYTES,
        mcp::MAX_SANITIZED_TOOL_OUTPUT_BYTES,
    );

    let _: fn() -> Vec<String> = mcp::task_terminal_states;
    let _: fn() -> mcp::McpRuntimeContractArtifact = mcp::mcp_runtime_contract_artifact;
    let _: fn() -> Result<String, serde_json::Error> = mcp::mcp_runtime_contract_json;
    let _: fn() -> Vec<String> = mcp::approved_mcp_protocol_versions;
    let _: fn() -> Vec<String> = mcp::official_rust_sdk_compile_time_markers;
    let _: fn() -> mcp::OfficialRustSdkAdapterMetadata = mcp::official_rust_sdk_adapter_metadata;
    let _: fn() -> mcp::HealthResponse = mcp::health;
    let _: fn(bool) -> mcp::ReadinessResponse = mcp::readiness;
    let _: fn(&mcp::CompatibilityCheckRequest) -> mcp::CompatibilityCheckResponse =
        mcp::check_compatibility;
    let _: fn(mcp::OfficialRustSdkErrorKind, &str) -> mcp::TypedError =
        mcp::map_official_rust_sdk_error;
    let _: fn(
        mcp::OfficialRustSdkShadowCompareRequest,
    ) -> mcp::OfficialRustSdkShadowCompareEvidence = mcp::compare_official_rust_sdk_shadow;
    let _: fn(&[u8]) -> Result<mcp::JsonRpcRequestEnvelope, mcp::McpRuntimeError> =
        mcp::validate_json_rpc_request;
    let _: fn(&str) -> Result<mcp::SanitizedToolOutput, mcp::McpRuntimeError> =
        mcp::sanitize_tool_output;
    let _: fn(bool, bool, bool) -> bool = mcp::can_retry_tool_call;
}

#[test]
fn crate_root_public_type_names_are_stable() {
    let expected = [
        std::any::type_name::<mcp::VersionInfo>(),
        std::any::type_name::<mcp::McpRuntimeContractArtifact>(),
        std::any::type_name::<mcp::OfficialRustSdkAdapterMetadata>(),
        std::any::type_name::<mcp::OfficialRustSdkClientConfig>(),
        std::any::type_name::<mcp::OfficialRustSdkToolDescriptor>(),
        std::any::type_name::<mcp::OfficialRustSdkToolCallResult>(),
        std::any::type_name::<mcp::OfficialRustSdkDiagnostics>(),
        std::any::type_name::<mcp::OfficialRustSdkClientSession>(),
        std::any::type_name::<mcp::OfficialRustSdkAdapter>(),
        std::any::type_name::<mcp::HealthState>(),
        std::any::type_name::<mcp::HealthResponse>(),
        std::any::type_name::<mcp::ReadinessState>(),
        std::any::type_name::<mcp::ReadinessResponse>(),
        std::any::type_name::<mcp::CompatibilityCheckRequest>(),
        std::any::type_name::<mcp::CompatibilityCheckResponse>(),
        std::any::type_name::<mcp::ErrorCategory>(),
        std::any::type_name::<mcp::McpRuntimeErrorCode>(),
        std::any::type_name::<mcp::TypedError>(),
        std::any::type_name::<mcp::McpRuntimeError>(),
        std::any::type_name::<mcp::ExternalMcpContext>(),
        std::any::type_name::<mcp::NormalizedToolDescriptorShape>(),
        std::any::type_name::<mcp::SafeToolCallResultShape>(),
        std::any::type_name::<mcp::NormalizedMcpAdapterSnapshot>(),
        std::any::type_name::<mcp::ShadowCompareStatus>(),
        std::any::type_name::<mcp::ShadowRedactionEvidence>(),
        std::any::type_name::<mcp::OfficialRustSdkShadowCompareRequest>(),
        std::any::type_name::<mcp::OfficialRustSdkShadowCompareEvidence>(),
        std::any::type_name::<mcp::OfficialRustSdkErrorKind>(),
        std::any::type_name::<mcp::JsonRpcRequestEnvelope>(),
        std::any::type_name::<mcp::SanitizedToolOutput>(),
        std::any::type_name::<mcp::BundleRegistry>(),
        std::any::type_name::<mcp::McpTaskState>(),
        std::any::type_name::<mcp::McpTaskRecord>(),
        std::any::type_name::<mcp::McpTaskRegistry>(),
    ];
    let actual = [
        "maf_mcp_runtime::VersionInfo",
        "maf_mcp_runtime::McpRuntimeContractArtifact",
        "maf_mcp_runtime::OfficialRustSdkAdapterMetadata",
        "maf_mcp_runtime::OfficialRustSdkClientConfig",
        "maf_mcp_runtime::OfficialRustSdkToolDescriptor",
        "maf_mcp_runtime::OfficialRustSdkToolCallResult",
        "maf_mcp_runtime::OfficialRustSdkDiagnostics",
        "maf_mcp_runtime::OfficialRustSdkClientSession",
        "maf_mcp_runtime::OfficialRustSdkAdapter",
        "maf_mcp_runtime::HealthState",
        "maf_mcp_runtime::HealthResponse",
        "maf_mcp_runtime::ReadinessState",
        "maf_mcp_runtime::ReadinessResponse",
        "maf_mcp_runtime::CompatibilityCheckRequest",
        "maf_mcp_runtime::CompatibilityCheckResponse",
        "maf_mcp_runtime::ErrorCategory",
        "maf_mcp_runtime::McpRuntimeErrorCode",
        "maf_mcp_runtime::TypedError",
        "maf_mcp_runtime::McpRuntimeError",
        "maf_mcp_runtime::ExternalMcpContext",
        "maf_mcp_runtime::NormalizedToolDescriptorShape",
        "maf_mcp_runtime::SafeToolCallResultShape",
        "maf_mcp_runtime::NormalizedMcpAdapterSnapshot",
        "maf_mcp_runtime::ShadowCompareStatus",
        "maf_mcp_runtime::ShadowRedactionEvidence",
        "maf_mcp_runtime::OfficialRustSdkShadowCompareRequest",
        "maf_mcp_runtime::OfficialRustSdkShadowCompareEvidence",
        "maf_mcp_runtime::OfficialRustSdkErrorKind",
        "maf_mcp_runtime::JsonRpcRequestEnvelope",
        "maf_mcp_runtime::SanitizedToolOutput",
        "maf_mcp_runtime::BundleRegistry",
        "maf_mcp_runtime::McpTaskState",
        "maf_mcp_runtime::McpTaskRecord",
        "maf_mcp_runtime::McpTaskRegistry",
    ];
    assert_eq!(expected, actual);
}

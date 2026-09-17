use std::collections::BTreeMap;

use super::{ErrorCategory, McpRuntimeError, McpRuntimeErrorCode, TypedError};

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

impl From<McpRuntimeErrorCode> for McpRuntimeError {
    fn from(code: McpRuntimeErrorCode) -> Self {
        Self {
            typed_error: TypedError::new(code, code.as_str()),
        }
    }
}

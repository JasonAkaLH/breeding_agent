use serde::Deserialize;

use super::{
    JsonRpcRequestEnvelope, MAX_JSON_RPC_BYTES, McpRuntimeError, McpRuntimeErrorCode, TypedError,
};

#[derive(Debug, Deserialize)]
struct JsonRpcRawEnvelope {
    jsonrpc: Option<String>,
    method: Option<String>,
    id: Option<serde_json::Value>,
    params: Option<serde_json::Value>,
    result: Option<serde_json::Value>,
    error: Option<serde_json::Value>,
}

pub(super) fn validate_json_rpc_request_impl(
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

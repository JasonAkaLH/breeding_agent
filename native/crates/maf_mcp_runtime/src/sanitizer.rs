use super::{
    MAX_RAW_TOOL_OUTPUT_BYTES, MAX_SANITIZED_TOOL_OUTPUT_BYTES, McpRuntimeError,
    McpRuntimeErrorCode, SanitizedToolOutput, TypedError,
};

pub(super) fn redact_authority_tokens(raw: &str) -> (String, usize) {
    let mut output = Vec::new();
    let mut redactions = 0usize;
    let mut skip_next_authorization_value = false;
    for token in raw.split_whitespace() {
        let lower = token.to_ascii_lowercase();
        if skip_next_authorization_value {
            output.push("[REDACTED]");
            redactions += 1;
            skip_next_authorization_value = lower == "bearer";
        } else if lower.starts_with("token=")
            || lower.starts_with("secret=")
            || lower.starts_with("api_key=")
            || lower.contains("token=")
            || lower.contains("secret=")
            || lower.contains("api_key=")
            || lower.starts_with("authorization:")
        {
            output.push("[REDACTED]");
            redactions += 1;
            if lower == "authorization:" {
                skip_next_authorization_value = true;
            }
        } else if lower == "bearer" {
            output.push("[REDACTED]");
            redactions += 1;
            skip_next_authorization_value = true;
        } else {
            output.push(token);
        }
    }
    (output.join(" "), redactions)
}

pub(super) fn sanitize_tool_output_impl(raw: &str) -> Result<SanitizedToolOutput, McpRuntimeError> {
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

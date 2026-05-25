//! Rust canonical contract artifacts for framework core types.
//!
//! Python `src.core` keeps the public import paths, but enum values, model
//! field snapshots, and stable core error codes are exported from this crate.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const COMPONENT_ID: &str = "maf_core_types";
pub const CONTRACT_VERSION: &str = "core.v1";
pub const SCHEMA_HASH: &str =
    "maf_core_types_core_v1_schema_20260525_username_token_legacy_auth_removed";
pub const ERROR_CODE_TABLE_HASH: &str = "maf_core_types_error_table_v1_20260515";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NamedValue {
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorCodeEntry {
    pub code: String,
    pub category: String,
    pub retriable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CoreContractArtifact {
    pub component: String,
    pub contract_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
    pub enums: BTreeMap<String, Vec<NamedValue>>,
    pub models: BTreeMap<String, Vec<String>>,
    pub error_codes: Vec<ErrorCodeEntry>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CoreErrorCode {
    ContractValidationFailed,
    BoundaryViolation,
    ContractMismatch,
    StructuredOutputInvalid,
}

impl CoreErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ContractValidationFailed => "core_contract_validation_failed",
            Self::BoundaryViolation => "core_boundary_violation",
            Self::ContractMismatch => "core_contract_mismatch",
            Self::StructuredOutputInvalid => "core_structured_output_invalid",
        }
    }

    #[must_use]
    pub const fn category(self) -> &'static str {
        match self {
            Self::ContractValidationFailed
            | Self::ContractMismatch
            | Self::StructuredOutputInvalid => "contract",
            Self::BoundaryViolation => "boundary",
        }
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct CoreTypedError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

impl CoreTypedError {
    #[must_use]
    pub fn new(code: CoreErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code.as_str().to_owned(),
            message: message.into(),
            retriable: false,
            category: code.category().to_owned(),
            safe_metadata: BTreeMap::new(),
        }
    }
}

fn named(values: &[(&str, &str)]) -> Vec<NamedValue> {
    values
        .iter()
        .map(|(name, value)| NamedValue {
            name: (*name).to_owned(),
            value: (*value).to_owned(),
        })
        .collect()
}

fn fields(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

#[must_use]
pub fn supported_features() -> Vec<String> {
    fields(&[
        "core_contract_artifact",
        "core_enum_snapshot",
        "core_model_snapshot",
        "core_typed_error_table",
        "pyo3_core_facade",
    ])
}

#[must_use]
pub fn enum_contracts() -> BTreeMap<String, Vec<NamedValue>> {
    BTreeMap::from([
        (
            "AckPolicy".to_owned(),
            named(&[("STRONG", "strong"), ("LIGHT", "light")]),
        ),
        (
            "ArtifactType".to_owned(),
            named(&[
                ("TEXT", "text"),
                ("JSON", "json"),
                ("FILE", "file"),
                ("DATASET", "dataset"),
                ("SUMMARY", "summary"),
            ]),
        ),
        (
            "ConversationStatus".to_owned(),
            named(&[
                ("ACTIVE", "active"),
                ("ARCHIVED", "archived"),
                ("LOCKED", "locked"),
            ]),
        ),
        (
            "DependencyType".to_owned(),
            named(&[("HARD", "hard"), ("SOFT", "soft")]),
        ),
        (
            "EdgeType".to_owned(),
            named(&[
                ("DATA", "data"),
                ("CONTROL", "control"),
                ("FALLBACK", "fallback"),
            ]),
        ),
        (
            "EventVisibility".to_owned(),
            named(&[
                ("FRONTEND", "frontend"),
                ("INTERNAL", "internal"),
                ("AUDIT_ONLY", "audit_only"),
            ]),
        ),
        (
            "InterruptStatus".to_owned(),
            named(&[
                ("OPEN", "open"),
                ("ANSWERED", "answered"),
                ("EXPIRED", "expired"),
                ("CANCELLED", "cancelled"),
            ]),
        ),
        (
            "MailboxChannel".to_owned(),
            named(&[
                ("ORCHESTRATOR_CONTROL", "orchestrator_control"),
                ("PEER_COLLABORATION", "peer_collaboration"),
                ("INTERRUPT_RESUME", "interrupt_resume"),
            ]),
        ),
        (
            "MailboxDeliveryStatus".to_owned(),
            named(&[
                ("PENDING", "pending"),
                ("DELIVERED", "delivered"),
                ("ACKNOWLEDGED", "acknowledged"),
                ("RESOLVED", "resolved"),
                ("EXPIRED", "expired"),
                ("CANCELLED", "cancelled"),
            ]),
        ),
        (
            "MessageRole".to_owned(),
            named(&[
                ("USER", "user"),
                ("ASSISTANT", "assistant"),
                ("SYSTEM", "system"),
            ]),
        ),
        (
            "NodeCriticality".to_owned(),
            named(&[
                ("REQUIRED", "required"),
                ("OPTIONAL", "optional"),
                ("FALLBACK", "fallback"),
            ]),
        ),
        (
            "NodeStatus".to_owned(),
            named(&[
                ("PENDING", "pending"),
                ("READY", "ready"),
                ("RUNNING", "running"),
                ("WAITING_FOR_DEPENDENCY", "waiting_for_dependency"),
                ("WAITING_FOR_INPUT", "waiting_for_input"),
                ("READY_TO_RESUME", "ready_to_resume"),
                ("RESUMING", "resuming"),
                ("CANCELLING", "cancelling"),
                ("COMPLETED", "completed"),
                ("FAILED", "failed"),
                ("CANCELLED", "cancelled"),
                ("BLOCKED_BY_CANCELLATION", "blocked_by_cancellation"),
                ("ORPHANED", "orphaned"),
            ]),
        ),
        (
            "RoutingMode".to_owned(),
            named(&[
                ("AUTO", "auto"),
                ("HINT", "hint"),
                ("FORCE_CAPABILITY", "force_capability"),
            ]),
        ),
        (
            "TaskStatus".to_owned(),
            named(&[
                ("ACCEPTED", "accepted"),
                ("PLANNING", "planning"),
                ("RUNNING", "running"),
                ("CANCELLING", "cancelling"),
                ("CANCELLED", "cancelled"),
                ("COMPLETED", "completed"),
                ("FAILED", "failed"),
            ]),
        ),
    ])
}

#[must_use]
pub fn model_contracts() -> BTreeMap<String, Vec<String>> {
    BTreeMap::from([
        (
            "Artifact".to_owned(),
            fields(&[
                "artifact_id",
                "task_id",
                "producer_node_id",
                "artifact_type",
                "storage_ref",
                "summary",
                "is_complete",
                "created_at",
            ]),
        ),
        (
            "AuthUserToken".to_owned(),
            fields(&[
                "username",
                "api_token_hash",
                "token_issued_at",
                "token_last_used_at",
                "created_at",
                "updated_at",
            ]),
        ),
        (
            "CapabilityExecutionError".to_owned(),
            fields(&["code", "message", "retriable", "metadata"]),
        ),
        (
            "CapabilityExecutionRequest".to_owned(),
            fields(&[
                "capability_id",
                "conversation_id",
                "task_id",
                "node_id",
                "input_payload",
                "context_refs",
                "dependency_outputs",
                "metadata",
            ]),
        ),
        (
            "CapabilityExecutionResult".to_owned(),
            fields(&[
                "capability_id",
                "task_id",
                "node_id",
                "output_payload",
                "artifacts",
                "events",
                "interrupt",
                "error",
                "metadata",
            ]),
        ),
        (
            "Checkpoint".to_owned(),
            fields(&[
                "checkpoint_id",
                "task_id",
                "node_id",
                "agent_id",
                "snapshot_ref",
                "snapshot_kind",
                "resume_token",
                "source_message_id",
                "created_at",
                "invalidated_at",
            ]),
        ),
        (
            "Conversation".to_owned(),
            fields(&[
                "conversation_id",
                "username",
                "status",
                "current_task_id",
                "title",
                "created_at",
                "updated_at",
            ]),
        ),
        (
            "ConversationMemorySummary".to_owned(),
            fields(&[
                "summary_id",
                "conversation_id",
                "username",
                "covered_until_turn_id",
                "covered_until_message_id",
                "covered_until_created_at",
                "summary_text",
                "source_message_count",
                "source_message_ids_hash",
                "estimated_tokens",
                "summary_version",
                "compression_policy_version",
                "model_metadata_safe",
                "last_error",
                "created_at",
                "updated_at",
            ]),
        ),
        (
            "EventRecord".to_owned(),
            fields(&[
                "event_id",
                "conversation_id",
                "task_id",
                "node_id",
                "agent_id",
                "event_type",
                "payload",
                "visibility",
                "created_at",
            ]),
        ),
        (
            "Interrupt".to_owned(),
            fields(&[
                "interrupt_id",
                "conversation_id",
                "task_id",
                "node_id",
                "source_agent",
                "source_message_id",
                "question",
                "reason_code",
                "required_fields",
                "status",
                "expires_at",
                "created_at",
                "answered_at",
                "cancelled_at",
            ]),
        ),
        (
            "InterruptAnswer".to_owned(),
            fields(&[
                "interrupt_answer_id",
                "interrupt_id",
                "answer_payload",
                "source_message_id",
                "accepted",
                "created_at",
                "accepted_at",
            ]),
        ),
        (
            "MailboxDelivery".to_owned(),
            fields(&[
                "delivery_id",
                "message_id",
                "recipient_agent",
                "recipient_role",
                "status",
                "attempt_count",
                "max_attempts",
                "ttl_seconds",
                "expires_at",
                "delivered_at",
                "acknowledged_at",
                "resolved_at",
                "next_retry_at",
                "last_error_code",
                "last_error_message",
                "created_at",
                "updated_at",
            ]),
        ),
        (
            "MailboxMessage".to_owned(),
            fields(&[
                "message_id",
                "conversation_id",
                "task_id",
                "node_id",
                "parent_message_id",
                "correlation_id",
                "from_agent",
                "to_agent",
                "to_role",
                "channel",
                "message_type",
                "ack_policy",
                "priority",
                "payload",
                "payload_schema_version",
                "created_at",
                "resolved_at",
            ]),
        ),
        (
            "Message".to_owned(),
            fields(&[
                "message_id",
                "conversation_id",
                "role",
                "content",
                "task_id",
                "stream_status",
                "created_at",
            ]),
        ),
        (
            "Task".to_owned(),
            fields(&[
                "task_id",
                "conversation_id",
                "root_message_id",
                "status",
                "routing_mode",
                "requested_capability_id",
                "root_node_id",
                "summary",
                "cancel_requested_at",
                "created_at",
                "updated_at",
            ]),
        ),
        (
            "TaskEdge".to_owned(),
            fields(&["from_node_id", "to_node_id", "edge_type", "condition"]),
        ),
        (
            "TaskNode".to_owned(),
            fields(&[
                "node_id",
                "task_id",
                "capability_id",
                "assigned_instance_id",
                "status",
                "criticality",
                "dependency_type",
                "retry_policy",
                "timeout_policy",
                "resource_class",
                "input_refs",
                "output_refs",
                "started_at",
                "finished_at",
            ]),
        ),
    ])
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        CoreErrorCode::ContractValidationFailed,
        CoreErrorCode::BoundaryViolation,
        CoreErrorCode::ContractMismatch,
        CoreErrorCode::StructuredOutputInvalid,
    ]
    .iter()
    .map(|code| ErrorCodeEntry {
        code: code.as_str().to_owned(),
        category: code.category().to_owned(),
        retriable: false,
    })
    .collect()
}

#[must_use]
pub fn core_contract_artifact() -> CoreContractArtifact {
    CoreContractArtifact {
        component: COMPONENT_ID.to_owned(),
        contract_version: CONTRACT_VERSION.to_owned(),
        schema_hash: SCHEMA_HASH.to_owned(),
        error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        supported_features: supported_features(),
        enums: enum_contracts(),
        models: model_contracts(),
        error_codes: error_code_table(),
    }
}

pub fn core_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&core_contract_artifact())?;
    json.push('\n');
    Ok(json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    #[test]
    fn enum_contracts_keep_stable_values() {
        let enums = enum_contracts();
        let task_status = enums.get("TaskStatus").expect("TaskStatus missing");
        assert_eq!(
            task_status.first().map(|entry| entry.value.as_str()),
            Some("accepted")
        );
        assert_eq!(
            task_status.last().map(|entry| entry.value.as_str()),
            Some("failed")
        );
        let node_status = enums.get("NodeStatus").expect("NodeStatus missing");
        assert!(
            node_status
                .iter()
                .any(|entry| entry.value == "ready_to_resume")
        );
    }

    #[test]
    fn model_contracts_include_cross_module_runtime_models() {
        let models = model_contracts();
        assert_eq!(models.get("Task").expect("Task missing")[0], "task_id");
        assert_eq!(
            models
                .get("CapabilityExecutionResult")
                .expect("CapabilityExecutionResult missing")[0],
            "capability_id"
        );
        assert!(models.contains_key("EventRecord"));
        assert!(!models.contains_key("AuthUser"));
        assert!(!models.contains_key("AuthSession"));
        assert!(!models.contains_key("CaptchaChallenge"));
        assert!(!models.contains_key("AuthApiToken"));
        assert_eq!(
            models
                .get("AuthUserToken")
                .expect("AuthUserToken missing")
                .as_slice(),
            [
                "username",
                "api_token_hash",
                "token_issued_at",
                "token_last_used_at",
                "created_at",
                "updated_at",
            ]
        );
    }

    #[test]
    fn core_error_codes_are_stable_prefixed_and_not_retriable() {
        let codes = error_code_table();
        assert!(codes.iter().all(|entry| entry.code.starts_with("core_")));
        assert!(codes.iter().all(|entry| !entry.retriable));
    }

    #[test]
    fn supported_features_include_pyo3_facade_contract() {
        let features = supported_features();
        assert!(features.contains(&"core_contract_artifact".to_owned()));
        assert!(features.contains(&"pyo3_core_facade".to_owned()));
    }

    #[test]
    fn checked_in_contract_artifact_matches_rust_canonical_export() {
        let artifact =
            fs::read_to_string(repo_root().join("src/core/rust_contracts/core_contract.json"))
                .expect("checked-in core contract artifact must exist");
        assert_eq!(
            artifact,
            core_contract_json().expect("serialize core contract")
        );
    }
}

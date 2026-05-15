//! Rust canonical lifecycle transition policy.
//!
//! The Python lifecycle module keeps storage orchestration and dataclass
//! adapters, while transition eligibility and target status tables come from
//! this crate's exported contract artifact.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const COMPONENT_ID: &str = "maf_lifecycle";
pub const CONTRACT_VERSION: &str = "lifecycle.v1";
pub const TRANSITION_TABLE_HASH: &str = "maf_lifecycle_transition_table_v1_20260515";
pub const ERROR_CODE_TABLE_HASH: &str = "maf_lifecycle_error_table_v1_20260515";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionRule {
    pub from: Vec<String>,
    pub to: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorCodeEntry {
    pub code: String,
    pub category: String,
    pub retriable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LifecycleContractArtifact {
    pub component: String,
    pub contract_version: String,
    pub transition_table_hash: String,
    pub error_code_table_hash: String,
    pub transitions: BTreeMap<String, TransitionRule>,
    pub cancel_node_targets: BTreeMap<String, String>,
    pub active_task_statuses: Vec<String>,
    pub cancel_interrupt_terminal_statuses: Vec<String>,
    pub interrupt_reopen_guard_terminal_statuses: Vec<String>,
    pub interrupt_open_status: String,
    pub delivery_timeout_terminal_statuses: Vec<String>,
    pub delivery_timeout_error_code: String,
    pub delivery_timeout_error_message: String,
    pub task_cancellation_noop_statuses: Vec<String>,
    pub late_result_rejected_task_statuses: Vec<String>,
    pub error_codes: Vec<ErrorCodeEntry>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum LifecycleErrorCode {
    TransitionDenied,
    ContractMismatch,
    StructuredOutputInvalid,
}

impl LifecycleErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TransitionDenied => "lifecycle_transition_denied",
            Self::ContractMismatch => "lifecycle_contract_mismatch",
            Self::StructuredOutputInvalid => "lifecycle_structured_output_invalid",
        }
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct LifecycleTypedError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

impl LifecycleTypedError {
    #[must_use]
    pub fn transition_denied(message: impl Into<String>) -> Self {
        Self {
            code: LifecycleErrorCode::TransitionDenied.as_str().to_owned(),
            message: message.into(),
            retriable: false,
            category: "lifecycle".to_owned(),
            safe_metadata: BTreeMap::new(),
        }
    }
}

fn values(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

fn rule(from: &[&str], to: &str) -> TransitionRule {
    TransitionRule {
        from: values(from),
        to: to.to_owned(),
    }
}

#[must_use]
pub fn transition_table() -> BTreeMap<String, TransitionRule> {
    BTreeMap::from([
        (
            "mailbox_delivery.mark_delivered".to_owned(),
            rule(&["pending"], "delivered"),
        ),
        (
            "mailbox_delivery.acknowledge".to_owned(),
            rule(&["delivered"], "acknowledged"),
        ),
        (
            "mailbox_delivery.resolve_strong".to_owned(),
            rule(&["acknowledged"], "resolved"),
        ),
        (
            "mailbox_delivery.resolve_light".to_owned(),
            rule(&["delivered", "acknowledged"], "resolved"),
        ),
        (
            "mailbox_delivery.retry_timeout".to_owned(),
            rule(&["pending", "delivered", "acknowledged"], "pending"),
        ),
        (
            "mailbox_delivery.expire_timeout".to_owned(),
            rule(&["pending", "delivered", "acknowledged"], "expired"),
        ),
        (
            "mailbox_delivery.cancel".to_owned(),
            rule(&["pending", "delivered", "acknowledged"], "cancelled"),
        ),
        (
            "node.open_interrupt".to_owned(),
            rule(
                &["pending", "ready", "running", "waiting_for_dependency"],
                "waiting_for_input",
            ),
        ),
        ("interrupt.answer".to_owned(), rule(&["open"], "answered")),
        (
            "interrupt.cancel".to_owned(),
            rule(&["open", "answered"], "cancelled"),
        ),
        (
            "node.answer_interrupt".to_owned(),
            rule(&["waiting_for_input"], "ready_to_resume"),
        ),
        (
            "node.begin_resume".to_owned(),
            rule(&["ready_to_resume"], "resuming"),
        ),
        (
            "task.begin_cancellation".to_owned(),
            rule(
                &["accepted", "planning", "running", "cancelling"],
                "cancelling",
            ),
        ),
        (
            "task.finalize_cancellation".to_owned(),
            rule(&["cancelling", "cancelled"], "cancelled"),
        ),
    ])
}

#[must_use]
pub fn cancel_node_targets() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("pending".to_owned(), "blocked_by_cancellation".to_owned()),
        ("ready".to_owned(), "blocked_by_cancellation".to_owned()),
        ("running".to_owned(), "cancelled".to_owned()),
        ("waiting_for_dependency".to_owned(), "cancelled".to_owned()),
        ("waiting_for_input".to_owned(), "cancelled".to_owned()),
        ("ready_to_resume".to_owned(), "cancelled".to_owned()),
        ("resuming".to_owned(), "cancelled".to_owned()),
        ("cancelling".to_owned(), "cancelled".to_owned()),
    ])
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        LifecycleErrorCode::TransitionDenied,
        LifecycleErrorCode::ContractMismatch,
        LifecycleErrorCode::StructuredOutputInvalid,
    ]
    .iter()
    .map(|code| ErrorCodeEntry {
        code: code.as_str().to_owned(),
        category: "lifecycle".to_owned(),
        retriable: false,
    })
    .collect()
}

#[must_use]
pub fn lifecycle_contract_artifact() -> LifecycleContractArtifact {
    LifecycleContractArtifact {
        component: COMPONENT_ID.to_owned(),
        contract_version: CONTRACT_VERSION.to_owned(),
        transition_table_hash: TRANSITION_TABLE_HASH.to_owned(),
        error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        transitions: transition_table(),
        cancel_node_targets: cancel_node_targets(),
        active_task_statuses: values(&["accepted", "planning", "running", "cancelling"]),
        cancel_interrupt_terminal_statuses: values(&["cancelled", "expired"]),
        interrupt_reopen_guard_terminal_statuses: values(&["answered", "cancelled", "expired"]),
        interrupt_open_status: "open".to_owned(),
        delivery_timeout_terminal_statuses: values(&["resolved", "cancelled", "expired"]),
        delivery_timeout_error_code: "ttl_expired".to_owned(),
        delivery_timeout_error_message: "delivery exceeded ttl window".to_owned(),
        task_cancellation_noop_statuses: values(&["cancelled", "completed", "failed"]),
        late_result_rejected_task_statuses: values(&["cancelling", "cancelled"]),
        error_codes: error_code_table(),
    }
}

pub fn lifecycle_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&lifecycle_contract_artifact())?;
    json.push('\n');
    Ok(json)
}

#[must_use]
pub fn can_transition(operation: &str, current: &str) -> bool {
    transition_table()
        .get(operation)
        .is_some_and(|rule| rule.from.iter().any(|status| status == current))
}

#[must_use]
pub fn transition_target(operation: &str) -> Option<String> {
    transition_table()
        .get(operation)
        .map(|rule| rule.to.clone())
}

#[must_use]
pub fn cancel_node_target(status: &str) -> Option<String> {
    cancel_node_targets().get(status).cloned()
}

#[must_use]
pub fn can_accept_late_result(task_status: Option<&str>) -> bool {
    match task_status {
        None => false,
        Some(status) => !["cancelling", "cancelled"].contains(&status),
    }
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
    fn transition_table_preserves_interrupt_resume_invariant() {
        assert!(can_transition("node.open_interrupt", "running"));
        assert!(!can_transition("node.open_interrupt", "completed"));
        assert_eq!(
            transition_target("node.begin_resume").as_deref(),
            Some("resuming")
        );
    }

    #[test]
    fn cancellation_policy_blocks_future_work_and_late_results() {
        assert_eq!(
            cancel_node_target("pending").as_deref(),
            Some("blocked_by_cancellation")
        );
        assert_eq!(cancel_node_target("running").as_deref(), Some("cancelled"));
        assert!(can_transition("mailbox_delivery.cancel", "delivered"));
        assert!(!can_transition("mailbox_delivery.cancel", "resolved"));
        assert_eq!(
            transition_target("mailbox_delivery.retry_timeout").as_deref(),
            Some("pending")
        );
        assert_eq!(
            transition_target("mailbox_delivery.expire_timeout").as_deref(),
            Some("expired")
        );
        assert!(can_transition("interrupt.cancel", "open"));
        assert!(!can_transition("interrupt.cancel", "expired"));
        let artifact = lifecycle_contract_artifact();
        assert_eq!(
            artifact.active_task_statuses,
            values(&["accepted", "planning", "running", "cancelling"])
        );
        assert_eq!(
            artifact
                .transitions
                .get("task.finalize_cancellation")
                .expect("finalize cancellation transition")
                .from,
            values(&["cancelling", "cancelled"])
        );
        assert_eq!(
            artifact.interrupt_reopen_guard_terminal_statuses,
            values(&["answered", "cancelled", "expired"])
        );
        assert_eq!(artifact.interrupt_open_status, "open");
        assert!(!can_accept_late_result(Some("cancelling")));
        assert!(!can_accept_late_result(Some("cancelled")));
        assert!(can_accept_late_result(Some("completed")));
        assert!(!can_accept_late_result(None));
    }

    #[test]
    fn lifecycle_error_codes_are_stable_prefixed_and_fail_closed() {
        let codes = error_code_table();
        assert!(
            codes
                .iter()
                .all(|entry| entry.code.starts_with("lifecycle_"))
        );
        assert!(codes.iter().all(|entry| !entry.retriable));
    }

    #[test]
    fn checked_in_contract_artifact_matches_rust_canonical_export() {
        let artifact = fs::read_to_string(
            repo_root().join("src/lifecycle/rust_contracts/lifecycle_contract.json"),
        )
        .expect("checked-in lifecycle contract artifact must exist");
        assert_eq!(
            artifact,
            lifecycle_contract_json().expect("serialize lifecycle contract")
        );
    }
}

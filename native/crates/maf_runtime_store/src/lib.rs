//! Runtime store / dispatcher / event sidecar contract and lease kernel.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use thiserror::Error;

pub const COMPONENT_ID: &str = "maf_runtime_sidecar";
pub const PROTOCOL_VERSION: &str = "maf.runtime.v1";
pub const SCHEMA_HASH: &str = "maf_runtime_v1_schema_20260826_event_append_exact_a4";
pub const ERROR_CODE_TABLE_HASH: &str = "maf_runtime_error_table_v1_idempotency_conflict_20260812";
pub const PROTO_HASH: &str = "maf_runtime_proto_v1_20260826_event_append_exact_a4";
pub const FEATURE_RUNTIME_STORE: &str = "runtime_store";
pub const FEATURE_EVENT_LOG: &str = "event_log";
pub const FEATURE_TASK_DISPATCHER: &str = "task_dispatcher";
pub const FEATURE_ARTIFACT_METADATA: &str = "artifact_metadata";
pub const FEATURE_TASK_READ: &str = "task_read";
pub const FEATURE_AGENT_STATE: &str = "agent_state";
pub const FEATURE_SUBMISSION_ADMISSION: &str = "submission_admission";
pub const MAX_IN_FLIGHT_MIN: u64 = 8;
pub const MAX_IN_FLIGHT_CAP: u64 = 64;
pub const MAX_IN_FLIGHT_CPU_MULTIPLIER: u64 = 4;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperationPolicy {
    pub name: String,
    pub kind: String,
    pub enforce_failure: String,
    pub python_legacy_write_fallback: bool,
    pub idempotency_required: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorCodeEntry {
    pub code: String,
    pub category: String,
    pub retriable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RetryPolicy {
    pub max_attempts: u32,
    pub initial_backoff_ms: u64,
    pub max_backoff_ms: u64,
    pub jitter_percent: u32,
    pub same_sidecar_only: bool,
    pub requires_idempotency_key: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConfigPolicy {
    pub allowed_sources: Vec<String>,
    pub forbidden_sources: Vec<String>,
    pub cross_host_requires_mtls: bool,
    pub secret_safe_metadata_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactProvenancePolicy {
    pub allowed_sources: Vec<String>,
    pub required_fields: Vec<String>,
    pub expected_proto_hash: String,
    pub require_schema_hash_match: bool,
    pub require_checksum_allowlist: bool,
    pub require_cargo_lock_digest_allowlist: bool,
    pub require_sbom: bool,
    pub require_provenance_attestation: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BenchmarkPolicy {
    pub required_baselines: Vec<String>,
    pub required_operations: Vec<String>,
    pub required_metrics: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromotionPolicy {
    pub min_shadow_days: u32,
    pub min_shadow_samples: u64,
    pub max_contract_mismatch_rate_ppm: u64,
    pub max_panic_count: u64,
    pub max_crash_count: u64,
    pub max_p95_latency_ratio_percent: u32,
    pub error_rate_must_not_exceed_legacy: bool,
    pub allowed_scopes: Vec<String>,
    pub required_evidence: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MigrationPolicy {
    pub required_components: Vec<String>,
    pub required_evidence: Vec<String>,
    pub require_target_schema_version: bool,
    pub task_authority_evidence_schema: String,
    pub task_authority_evidence_path_env: String,
    pub task_authority_hmac_key_path_env: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpsPolicy {
    pub required_observability: Vec<String>,
    pub required_runbooks: Vec<String>,
    pub required_drills: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecommissionPolicy {
    pub required_removed_legacy_paths: Vec<String>,
    pub required_facade_only_paths: Vec<String>,
    pub required_evidence: Vec<String>,
    pub allowed_rollback_paths: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeSidecarContractArtifact {
    pub component: String,
    pub protocol_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
    pub modes: Vec<String>,
    pub mode_env: BTreeMap<String, String>,
    pub operations: Vec<OperationPolicy>,
    pub error_codes: Vec<ErrorCodeEntry>,
    pub resource_limits: BTreeMap<String, u64>,
    pub retry_policy: RetryPolicy,
    pub config_policy: ConfigPolicy,
    pub artifact_policy: ArtifactProvenancePolicy,
    pub benchmark_policy: BenchmarkPolicy,
    pub promotion_policy: PromotionPolicy,
    pub migration_policy: MigrationPolicy,
    pub ops_policy: OpsPolicy,
    pub decommission_policy: DecommissionPolicy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RuntimeSidecarErrorCode {
    RuntimeStoreUnavailable,
    RuntimeStoreProtocolIncompatible,
    RuntimeStoreResponseInvalid,
    RuntimeStoreConfigUntrusted,
    RuntimeStoreArtifactUntrusted,
    RuntimeStoreBenchmarkInvalid,
    RuntimeStorePromotionBlocked,
    RuntimeStoreMigrationBlocked,
    RuntimeStoreOpsReadinessBlocked,
    RuntimeStoreDecommissionBlocked,
    RuntimeStoreLeaseConflict,
    RuntimeStoreLeaseExpired,
    RuntimeStoreIdempotencyConflict,
    RuntimeStoreWriteFailed,
    EventLogUnavailable,
    EventLogPayloadTooLarge,
    EventLogReplayPageExceeded,
    DispatcherUnavailable,
    DispatcherQueueFull,
    DispatcherDeadlineExceeded,
}

impl RuntimeSidecarErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::RuntimeStoreUnavailable => "runtime_store_unavailable",
            Self::RuntimeStoreProtocolIncompatible => "runtime_store_protocol_incompatible",
            Self::RuntimeStoreResponseInvalid => "runtime_store_response_invalid",
            Self::RuntimeStoreConfigUntrusted => "runtime_store_config_untrusted",
            Self::RuntimeStoreArtifactUntrusted => "runtime_store_artifact_untrusted",
            Self::RuntimeStoreBenchmarkInvalid => "runtime_store_benchmark_invalid",
            Self::RuntimeStorePromotionBlocked => "runtime_store_promotion_blocked",
            Self::RuntimeStoreMigrationBlocked => "runtime_store_migration_blocked",
            Self::RuntimeStoreOpsReadinessBlocked => "runtime_store_ops_readiness_blocked",
            Self::RuntimeStoreDecommissionBlocked => "runtime_store_decommission_blocked",
            Self::RuntimeStoreLeaseConflict => "runtime_store_lease_conflict",
            Self::RuntimeStoreLeaseExpired => "runtime_store_lease_expired",
            Self::RuntimeStoreIdempotencyConflict => "runtime_store_idempotency_conflict",
            Self::RuntimeStoreWriteFailed => "runtime_store_write_failed",
            Self::EventLogUnavailable => "event_log_unavailable",
            Self::EventLogPayloadTooLarge => "event_log_payload_too_large",
            Self::EventLogReplayPageExceeded => "event_log_replay_page_exceeded",
            Self::DispatcherUnavailable => "dispatcher_unavailable",
            Self::DispatcherQueueFull => "dispatcher_queue_full",
            Self::DispatcherDeadlineExceeded => "dispatcher_deadline_exceeded",
        }
    }

    #[must_use]
    pub const fn category(self) -> &'static str {
        match self {
            Self::RuntimeStoreIdempotencyConflict => "internal",
            Self::RuntimeStoreProtocolIncompatible => "compatibility",
            Self::RuntimeStoreResponseInvalid => "protocol",
            Self::RuntimeStoreConfigUntrusted | Self::RuntimeStoreArtifactUntrusted => "security",
            Self::RuntimeStoreBenchmarkInvalid
            | Self::RuntimeStorePromotionBlocked
            | Self::RuntimeStoreOpsReadinessBlocked
            | Self::RuntimeStoreDecommissionBlocked => "quality_gate",
            Self::RuntimeStoreMigrationBlocked
            | Self::RuntimeStoreLeaseConflict
            | Self::RuntimeStoreLeaseExpired
            | Self::RuntimeStoreWriteFailed => "state",
            Self::EventLogPayloadTooLarge
            | Self::EventLogReplayPageExceeded
            | Self::DispatcherQueueFull
            | Self::DispatcherDeadlineExceeded => "resource_limit",
            Self::RuntimeStoreUnavailable
            | Self::EventLogUnavailable
            | Self::DispatcherUnavailable => "internal",
        }
    }

    #[must_use]
    pub const fn retriable(self) -> bool {
        matches!(
            self,
            Self::RuntimeStoreUnavailable
                | Self::EventLogUnavailable
                | Self::DispatcherUnavailable
                | Self::DispatcherQueueFull
                | Self::DispatcherDeadlineExceeded
        )
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct RuntimeSidecarError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

impl RuntimeSidecarError {
    #[must_use]
    pub fn new(code: RuntimeSidecarErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code.as_str().to_owned(),
            message: message.into(),
            retriable: code.retriable(),
            category: code.category().to_owned(),
            safe_metadata: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskLease {
    pub task_id: String,
    pub owner_id: String,
    pub revision: u64,
    pub expires_at_ms: i64,
    pub renew_token: String,
}

#[derive(Debug, Default)]
pub struct LeaseRegistry {
    leases: BTreeMap<String, TaskLease>,
}

impl LeaseRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn acquire(
        &mut self,
        task_id: impl Into<String>,
        owner_id: impl Into<String>,
        now_ms: i64,
        ttl_ms: i64,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        let task_id = task_id.into();
        let owner_id = owner_id.into();
        if let Some(existing) = self.leases.get(&task_id)
            && existing.expires_at_ms > now_ms
            && existing.owner_id != owner_id
        {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseConflict,
                "task lease is owned by another active owner",
            ));
        }
        let revision = self
            .leases
            .get(&task_id)
            .map_or(1, |lease| lease.revision + 1);
        let lease = TaskLease {
            renew_token: format!("lease:{task_id}:{owner_id}:{revision}"),
            task_id: task_id.clone(),
            owner_id,
            revision,
            expires_at_ms: now_ms + ttl_ms,
        };
        self.leases.insert(task_id, lease.clone());
        Ok(lease)
    }

    pub fn renew(
        &mut self,
        task_id: &str,
        renew_token: &str,
        now_ms: i64,
        ttl_ms: i64,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        let lease = self.leases.get_mut(task_id).ok_or_else(|| {
            RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseExpired,
                "task lease is missing",
            )
        })?;
        if lease.renew_token != renew_token || lease.expires_at_ms <= now_ms {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseExpired,
                "task lease cannot be renewed",
            ));
        }
        lease.revision += 1;
        lease.expires_at_ms = now_ms + ttl_ms;
        lease.renew_token = format!("lease:{task_id}:{}:{}", lease.owner_id, lease.revision);
        Ok(lease.clone())
    }

    pub fn release(
        &mut self,
        task_id: &str,
        renew_token: &str,
    ) -> Result<bool, RuntimeSidecarError> {
        let Some(lease) = self.leases.get(task_id) else {
            return Ok(false);
        };
        if lease.renew_token != renew_token {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreLeaseConflict,
                "task lease release token mismatch",
            ));
        }
        self.leases.remove(task_id);
        Ok(true)
    }
}

fn write_operation(name: &str) -> OperationPolicy {
    OperationPolicy {
        name: name.to_owned(),
        kind: "write".to_owned(),
        enforce_failure: "fail_closed".to_owned(),
        python_legacy_write_fallback: false,
        idempotency_required: true,
    }
}

#[must_use]
pub fn operation_policies() -> Vec<OperationPolicy> {
    [
        "task_submit",
        "node_state_transition",
        "artifact_save",
        "event_append",
        "lease_acquire",
        "lease_renew",
        "lease_release",
        "cancellation_token_write",
        "bundle_revision_pin",
        "bundle_revision_release",
        "agent_state_commit",
        "submission_admit",
        "submission_pending_claim",
        "submission_claim_renew",
        "submission_projection_acknowledge",
        "submission_handoff_prepare",
        "submission_handoff_acknowledge",
        "conversation_admission_close",
        "message_identity_reserve",
    ]
    .into_iter()
    .map(write_operation)
    .chain(
        [
            "task_get",
            "task_list_for_conversation",
            "task_get_active_for_conversation",
            "task_node_get",
            "task_node_list",
            "artifact_get",
            "artifact_list",
            "event_replay",
            "agent_run_get",
            "agent_item_list",
            "agent_final_projection_get",
            "submission_preparation_get",
        ]
        .into_iter()
        .map(|name| OperationPolicy {
            name: name.to_owned(),
            kind: "read".to_owned(),
            enforce_failure: "read_only_degraded_error".to_owned(),
            python_legacy_write_fallback: false,
            idempotency_required: false,
        }),
    )
    .collect()
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        RuntimeSidecarErrorCode::RuntimeStoreUnavailable,
        RuntimeSidecarErrorCode::RuntimeStoreProtocolIncompatible,
        RuntimeSidecarErrorCode::RuntimeStoreResponseInvalid,
        RuntimeSidecarErrorCode::RuntimeStoreConfigUntrusted,
        RuntimeSidecarErrorCode::RuntimeStoreArtifactUntrusted,
        RuntimeSidecarErrorCode::RuntimeStoreBenchmarkInvalid,
        RuntimeSidecarErrorCode::RuntimeStorePromotionBlocked,
        RuntimeSidecarErrorCode::RuntimeStoreMigrationBlocked,
        RuntimeSidecarErrorCode::RuntimeStoreOpsReadinessBlocked,
        RuntimeSidecarErrorCode::RuntimeStoreDecommissionBlocked,
        RuntimeSidecarErrorCode::RuntimeStoreLeaseConflict,
        RuntimeSidecarErrorCode::RuntimeStoreLeaseExpired,
        RuntimeSidecarErrorCode::RuntimeStoreIdempotencyConflict,
        RuntimeSidecarErrorCode::RuntimeStoreWriteFailed,
        RuntimeSidecarErrorCode::EventLogUnavailable,
        RuntimeSidecarErrorCode::EventLogPayloadTooLarge,
        RuntimeSidecarErrorCode::EventLogReplayPageExceeded,
        RuntimeSidecarErrorCode::DispatcherUnavailable,
        RuntimeSidecarErrorCode::DispatcherQueueFull,
        RuntimeSidecarErrorCode::DispatcherDeadlineExceeded,
    ]
    .iter()
    .map(|code| ErrorCodeEntry {
        code: code.as_str().to_owned(),
        category: code.category().to_owned(),
        retriable: code.retriable(),
    })
    .collect()
}

#[must_use]
pub const fn retry_policy() -> RetryPolicy {
    RetryPolicy {
        max_attempts: 3,
        initial_backoff_ms: 100,
        max_backoff_ms: 1_000,
        jitter_percent: 20,
        same_sidecar_only: true,
        requires_idempotency_key: true,
    }
}

#[must_use]
pub const fn max_in_flight_for_cpu(cpu_count: u64) -> u64 {
    let computed = cpu_count * MAX_IN_FLIGHT_CPU_MULTIPLIER;
    if computed < MAX_IN_FLIGHT_MIN {
        MAX_IN_FLIGHT_MIN
    } else if computed > MAX_IN_FLIGHT_CAP {
        MAX_IN_FLIGHT_CAP
    } else {
        computed
    }
}

#[must_use]
pub fn config_policy() -> ConfigPolicy {
    ConfigPolicy {
        allowed_sources: [
            "deployment_config",
            "environment_variable",
            "secret_manager",
            "readonly_config_file",
            "runtime_allowlist",
        ]
        .iter()
        .map(|source| (*source).to_owned())
        .collect(),
        forbidden_sources: [
            "user_input",
            "skill_manifest",
            "llm_output",
            "external_tool_output",
        ]
        .iter()
        .map(|source| (*source).to_owned())
        .collect(),
        cross_host_requires_mtls: true,
        secret_safe_metadata_only: true,
    }
}

#[must_use]
pub fn artifact_provenance_policy() -> ArtifactProvenancePolicy {
    ArtifactProvenancePolicy {
        allowed_sources: ["ci_pipeline", "deployment_pipeline", "runtime_allowlist"]
            .iter()
            .map(|source| (*source).to_owned())
            .collect(),
        required_fields: [
            "source",
            "artifact_kind",
            "checksum_sha256",
            "sbom_digest",
            "cargo_lock_digest",
            "proto_hash",
            "schema_hash",
            "provenance_attestation",
        ]
        .iter()
        .map(|field| (*field).to_owned())
        .collect(),
        expected_proto_hash: PROTO_HASH.to_owned(),
        require_schema_hash_match: true,
        require_checksum_allowlist: true,
        require_cargo_lock_digest_allowlist: true,
        require_sbom: true,
        require_provenance_attestation: true,
    }
}

#[must_use]
pub fn benchmark_policy() -> BenchmarkPolicy {
    BenchmarkPolicy {
        required_baselines: ["python_baseline", "rust_sidecar_baseline"]
            .iter()
            .map(|baseline| (*baseline).to_owned())
            .collect(),
        required_operations: [
            "task_submit",
            "node_state_transition",
            "artifact_save",
            "event_append",
            "lease_acquire",
            "event_replay",
            "sse_snapshot",
        ]
        .iter()
        .map(|operation| (*operation).to_owned())
        .collect(),
        required_metrics: [
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "queue_wait_ms",
            "cpu_percent",
            "memory_mb",
            "throughput_per_sec",
        ]
        .iter()
        .map(|metric| (*metric).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn promotion_policy() -> PromotionPolicy {
    PromotionPolicy {
        min_shadow_days: 7,
        min_shadow_samples: 1_000,
        max_contract_mismatch_rate_ppm: 0,
        max_panic_count: 0,
        max_crash_count: 0,
        max_p95_latency_ratio_percent: 110,
        error_rate_must_not_exceed_legacy: true,
        allowed_scopes: ["single_task", "single_conversation", "single_instance"]
            .iter()
            .map(|scope| (*scope).to_owned())
            .collect(),
        required_evidence: [
            "artifact_provenance",
            "benchmark_report",
            "audit_redaction_secret_leak",
            "rollback_drill",
            "ops_runbook",
            "regression_tests",
            "cargo_tests",
            "cargo_clippy",
            "cargo_fmt",
        ]
        .iter()
        .map(|evidence| (*evidence).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn migration_policy() -> MigrationPolicy {
    MigrationPolicy {
        required_components: [
            "sqlite_schema",
            "event_log",
            "lease",
            "cursor",
            "artifact_metadata",
            "bundle_pin",
        ]
        .iter()
        .map(|component| (*component).to_owned())
        .collect(),
        required_evidence: [
            "schema_version",
            "migration_lock",
            "preflight",
            "dry_run",
            "backup",
            "restore",
            "event_replay_validation",
            "rollback_runbook",
            "roll_forward_runbook",
        ]
        .iter()
        .map(|evidence| (*evidence).to_owned())
        .collect(),
        require_target_schema_version: true,
        task_authority_evidence_schema: "maf.runtime_sidecar.task_authority_migration_evidence.v1"
            .to_owned(),
        task_authority_evidence_path_env: "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH".to_owned(),
        task_authority_hmac_key_path_env: "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH"
            .to_owned(),
    }
}

#[must_use]
pub fn ops_policy() -> OpsPolicy {
    OpsPolicy {
        required_observability: ["dashboard", "alert", "slo"]
            .iter()
            .map(|item| (*item).to_owned())
            .collect(),
        required_runbooks: ["drain", "restart", "rollback", "restore", "replay"]
            .iter()
            .map(|item| (*item).to_owned())
            .collect(),
        required_drills: [
            "unavailable",
            "protocol_mismatch",
            "queue_full",
            "deadline_spike",
            "secret_identity_mismatch",
            "migration_failure",
            "crash_recovery",
            "restore_replay",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn decommission_policy() -> DecommissionPolicy {
    DecommissionPolicy {
        required_removed_legacy_paths: [
            "python_storage_task_write",
            "python_storage_node_write",
            "python_storage_artifact_write",
            "python_event_append_write",
            "python_bundle_pin_write",
            "python_cancellation_token_write",
            "python_lease_state",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
        required_facade_only_paths: [
            "runtime_store_client",
            "dispatcher_client",
            "event_log_client",
            "api_dto_adapter",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
        required_evidence: [
            "promotion_threshold_passed",
            "ops_readiness_passed",
            "migration_dr_passed",
            "legacy_write_architecture_guard",
            "decommission_regression_tests",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
        allowed_rollback_paths: ["deployment_or_restore", "sidecar_artifact_rollback"]
            .iter()
            .map(|item| (*item).to_owned())
            .collect(),
    }
}

#[must_use]
pub fn runtime_sidecar_contract_artifact() -> RuntimeSidecarContractArtifact {
    RuntimeSidecarContractArtifact {
        component: COMPONENT_ID.to_owned(),
        protocol_version: PROTOCOL_VERSION.to_owned(),
        schema_hash: SCHEMA_HASH.to_owned(),
        error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        supported_features: vec![
            FEATURE_RUNTIME_STORE.to_owned(),
            FEATURE_EVENT_LOG.to_owned(),
            FEATURE_TASK_DISPATCHER.to_owned(),
            FEATURE_ARTIFACT_METADATA.to_owned(),
            FEATURE_TASK_READ.to_owned(),
            FEATURE_AGENT_STATE.to_owned(),
            FEATURE_SUBMISSION_ADMISSION.to_owned(),
        ],
        modes: vec!["off".to_owned(), "shadow".to_owned(), "enforce".to_owned()],
        mode_env: BTreeMap::from([
            (
                "runtime_store".to_owned(),
                "MAF_RUST_RUNTIME_STORE_MODE".to_owned(),
            ),
            ("event_log".to_owned(), "MAF_RUST_EVENT_LOG_MODE".to_owned()),
            (
                "task_dispatcher".to_owned(),
                "MAF_RUST_TASK_DISPATCHER_MODE".to_owned(),
            ),
        ]),
        operations: operation_policies(),
        error_codes: error_code_table(),
        resource_limits: BTreeMap::from([
            ("max_in_flight_min".to_owned(), MAX_IN_FLIGHT_MIN),
            ("max_in_flight_cap".to_owned(), MAX_IN_FLIGHT_CAP),
            (
                "max_in_flight_cpu_multiplier".to_owned(),
                MAX_IN_FLIGHT_CPU_MULTIPLIER,
            ),
            ("queue_size".to_owned(), 1024),
            ("queue_wait_ms".to_owned(), 2_000),
            ("task_submit_deadline_ms".to_owned(), 3_000),
            ("state_transition_deadline_ms".to_owned(), 2_000),
            ("artifact_metadata_deadline_ms".to_owned(), 2_000),
            ("event_append_deadline_ms".to_owned(), 2_000),
            ("lease_deadline_ms".to_owned(), 1_000),
            ("event_replay_deadline_ms".to_owned(), 10_000),
            ("event_payload_bytes".to_owned(), 256 * 1024),
            ("replay_page_events".to_owned(), 1_000),
            ("replay_page_bytes".to_owned(), 1024 * 1024),
            ("shutdown_drain_ms".to_owned(), 30_000),
            (
                "submission_conversation_projection_bytes".to_owned(),
                64 * 1024,
            ),
            (
                "submission_message_projection_bytes".to_owned(),
                64 * 1024 * 1024,
            ),
            ("submission_continuation_bytes".to_owned(), 64 * 1024 * 1024),
            ("submission_prepared_execution_bytes".to_owned(), 128 * 1024),
            ("submission_import_page_rows".to_owned(), 1_000),
            ("submission_import_record_bytes".to_owned(), 64 * 1024),
            (
                "submission_import_stdin_bytes".to_owned(),
                1024 * 1024 * 1024,
            ),
            ("grpc_max_message_bytes".to_owned(), 140 * 1024 * 1024),
        ]),
        retry_policy: retry_policy(),
        config_policy: config_policy(),
        artifact_policy: artifact_provenance_policy(),
        benchmark_policy: benchmark_policy(),
        promotion_policy: promotion_policy(),
        migration_policy: migration_policy(),
        ops_policy: ops_policy(),
        decommission_policy: decommission_policy(),
    }
}

pub fn runtime_sidecar_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&runtime_sidecar_contract_artifact())?;
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
    fn write_operations_fail_closed_without_python_fallback() {
        let operations = operation_policies();
        let claim = operations
            .iter()
            .find(|operation| operation.name == "submission_pending_claim")
            .expect("pending submission claim operation");
        assert_eq!(claim.kind, "write");
        assert!(claim.idempotency_required);
        for operation in operations
            .into_iter()
            .filter(|operation| operation.kind == "write")
        {
            assert_eq!(operation.enforce_failure, "fail_closed");
            assert!(!operation.python_legacy_write_fallback);
            assert!(operation.idempotency_required);
        }
    }

    #[test]
    fn lease_registry_enforces_owner_and_token() {
        let mut registry = LeaseRegistry::new();
        let lease = registry
            .acquire("task-1", "owner-1", 100, 50)
            .expect("lease");
        assert!(registry.acquire("task-1", "owner-2", 110, 50).is_err());
        let renewed = registry
            .renew("task-1", &lease.renew_token, 120, 50)
            .expect("renew");
        assert_eq!(renewed.revision, 2);
        assert!(registry.release("task-1", "bad-token").is_err());
        assert!(
            registry
                .release("task-1", &renewed.renew_token)
                .expect("release")
        );
    }

    #[test]
    fn retry_policy_requires_idempotency_and_same_sidecar() {
        let policy = retry_policy();
        assert_eq!(policy.max_attempts, 3);
        assert_eq!(policy.initial_backoff_ms, 100);
        assert_eq!(policy.max_backoff_ms, 1_000);
        assert_eq!(policy.jitter_percent, 20);
        assert!(policy.same_sidecar_only);
        assert!(policy.requires_idempotency_key);
    }

    #[test]
    fn backpressure_and_deadline_limits_match_prd_hard_caps() {
        assert_eq!(max_in_flight_for_cpu(1), 8);
        assert_eq!(max_in_flight_for_cpu(4), 16);
        assert_eq!(max_in_flight_for_cpu(32), 64);
        let artifact = runtime_sidecar_contract_artifact();
        assert_eq!(artifact.resource_limits["task_submit_deadline_ms"], 3_000);
        assert_eq!(
            artifact.resource_limits["state_transition_deadline_ms"],
            2_000
        );
        assert_eq!(
            artifact.resource_limits["artifact_metadata_deadline_ms"],
            2_000
        );
        assert_eq!(artifact.resource_limits["event_append_deadline_ms"], 2_000);
        assert_eq!(artifact.resource_limits["lease_deadline_ms"], 1_000);
        assert_eq!(artifact.resource_limits["event_replay_deadline_ms"], 10_000);
    }

    #[test]
    fn config_policy_rejects_untrusted_sources_and_requires_cross_host_mtls() {
        let policy = config_policy();
        assert!(
            policy
                .allowed_sources
                .contains(&"deployment_config".to_owned())
        );
        assert!(
            policy
                .allowed_sources
                .contains(&"secret_manager".to_owned())
        );
        assert!(policy.forbidden_sources.contains(&"user_input".to_owned()));
        assert!(policy.forbidden_sources.contains(&"llm_output".to_owned()));
        assert!(policy.cross_host_requires_mtls);
        assert!(policy.secret_safe_metadata_only);
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreConfigUntrusted.category(),
            "security"
        );
        assert!(!RuntimeSidecarErrorCode::RuntimeStoreConfigUntrusted.retriable());
    }

    #[test]
    fn artifact_provenance_policy_requires_ci_built_allowlisted_artifacts() {
        let policy = artifact_provenance_policy();
        assert!(policy.allowed_sources.contains(&"ci_pipeline".to_owned()));
        assert!(
            policy
                .allowed_sources
                .contains(&"deployment_pipeline".to_owned())
        );
        assert!(
            policy
                .required_fields
                .contains(&"checksum_sha256".to_owned())
        );
        assert!(
            policy
                .required_fields
                .contains(&"cargo_lock_digest".to_owned())
        );
        assert!(
            policy
                .required_fields
                .contains(&"provenance_attestation".to_owned())
        );
        assert_eq!(policy.expected_proto_hash, PROTO_HASH);
        assert!(policy.require_schema_hash_match);
        assert!(policy.require_checksum_allowlist);
        assert!(policy.require_cargo_lock_digest_allowlist);
        assert!(policy.require_sbom);
        assert!(policy.require_provenance_attestation);
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreArtifactUntrusted.category(),
            "security"
        );
        assert!(!RuntimeSidecarErrorCode::RuntimeStoreArtifactUntrusted.retriable());
    }

    #[test]
    fn benchmark_policy_requires_python_and_rust_baseline_metrics() {
        let policy = benchmark_policy();
        assert_eq!(
            policy.required_baselines,
            vec![
                "python_baseline".to_owned(),
                "rust_sidecar_baseline".to_owned()
            ]
        );
        assert!(
            policy
                .required_operations
                .contains(&"task_submit".to_owned())
        );
        assert!(
            policy
                .required_operations
                .contains(&"artifact_save".to_owned())
        );
        assert!(
            policy
                .required_operations
                .contains(&"event_replay".to_owned())
        );
        assert!(
            policy
                .required_operations
                .contains(&"sse_snapshot".to_owned())
        );
        assert!(policy.required_metrics.contains(&"p95_ms".to_owned()));
        assert!(policy.required_metrics.contains(&"p99_ms".to_owned()));
        assert!(
            policy
                .required_metrics
                .contains(&"queue_wait_ms".to_owned())
        );
        assert!(policy.required_metrics.contains(&"cpu_percent".to_owned()));
        assert!(policy.required_metrics.contains(&"memory_mb".to_owned()));
        assert!(
            policy
                .required_metrics
                .contains(&"throughput_per_sec".to_owned())
        );
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreBenchmarkInvalid.category(),
            "quality_gate"
        );
    }

    #[test]
    fn promotion_policy_matches_global_minimum_thresholds() {
        let policy = promotion_policy();
        assert_eq!(policy.min_shadow_days, 7);
        assert_eq!(policy.min_shadow_samples, 1_000);
        assert_eq!(policy.max_contract_mismatch_rate_ppm, 0);
        assert_eq!(policy.max_panic_count, 0);
        assert_eq!(policy.max_crash_count, 0);
        assert_eq!(policy.max_p95_latency_ratio_percent, 110);
        assert!(policy.error_rate_must_not_exceed_legacy);
        assert!(
            policy
                .allowed_scopes
                .contains(&"single_instance".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"artifact_provenance".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"rollback_drill".to_owned())
        );
        assert!(policy.required_evidence.contains(&"cargo_fmt".to_owned()));
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStorePromotionBlocked.category(),
            "quality_gate"
        );
    }

    #[test]
    fn migration_policy_requires_dr_and_replay_evidence() {
        let policy = migration_policy();
        assert_eq!(
            policy.required_components,
            vec![
                "sqlite_schema".to_owned(),
                "event_log".to_owned(),
                "lease".to_owned(),
                "cursor".to_owned(),
                "artifact_metadata".to_owned(),
                "bundle_pin".to_owned(),
            ]
        );
        assert!(
            policy
                .required_evidence
                .contains(&"schema_version".to_owned())
        );
        assert_eq!(
            policy.task_authority_evidence_path_env,
            "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH"
        );
        assert!(
            policy
                .required_evidence
                .contains(&"migration_lock".to_owned())
        );
        assert!(policy.required_evidence.contains(&"preflight".to_owned()));
        assert!(policy.required_evidence.contains(&"dry_run".to_owned()));
        assert!(policy.required_evidence.contains(&"backup".to_owned()));
        assert!(policy.required_evidence.contains(&"restore".to_owned()));
        assert!(
            policy
                .required_evidence
                .contains(&"event_replay_validation".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"rollback_runbook".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"roll_forward_runbook".to_owned())
        );
        assert!(policy.require_target_schema_version);
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreMigrationBlocked.category(),
            "state"
        );
    }

    #[test]
    fn ops_policy_requires_runbooks_observability_and_fault_drills() {
        let policy = ops_policy();
        assert_eq!(
            policy.required_observability,
            vec!["dashboard".to_owned(), "alert".to_owned(), "slo".to_owned(),]
        );
        assert_eq!(
            policy.required_runbooks,
            vec![
                "drain".to_owned(),
                "restart".to_owned(),
                "rollback".to_owned(),
                "restore".to_owned(),
                "replay".to_owned(),
            ]
        );
        assert!(policy.required_drills.contains(&"unavailable".to_owned()));
        assert!(
            policy
                .required_drills
                .contains(&"protocol_mismatch".to_owned())
        );
        assert!(policy.required_drills.contains(&"queue_full".to_owned()));
        assert!(
            policy
                .required_drills
                .contains(&"deadline_spike".to_owned())
        );
        assert!(
            policy
                .required_drills
                .contains(&"secret_identity_mismatch".to_owned())
        );
        assert!(
            policy
                .required_drills
                .contains(&"migration_failure".to_owned())
        );
        assert!(
            policy
                .required_drills
                .contains(&"crash_recovery".to_owned())
        );
        assert!(
            policy
                .required_drills
                .contains(&"restore_replay".to_owned())
        );
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreOpsReadinessBlocked.category(),
            "quality_gate"
        );
    }

    #[test]
    fn decommission_policy_requires_legacy_write_path_removal() {
        let policy = decommission_policy();
        assert_eq!(
            policy.required_removed_legacy_paths,
            vec![
                "python_storage_task_write".to_owned(),
                "python_storage_node_write".to_owned(),
                "python_storage_artifact_write".to_owned(),
                "python_event_append_write".to_owned(),
                "python_bundle_pin_write".to_owned(),
                "python_cancellation_token_write".to_owned(),
                "python_lease_state".to_owned(),
            ]
        );
        assert!(
            policy
                .required_facade_only_paths
                .contains(&"runtime_store_client".to_owned())
        );
        assert!(
            policy
                .required_facade_only_paths
                .contains(&"dispatcher_client".to_owned())
        );
        assert!(
            policy
                .required_facade_only_paths
                .contains(&"event_log_client".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"promotion_threshold_passed".to_owned())
        );
        assert!(
            policy
                .required_evidence
                .contains(&"legacy_write_architecture_guard".to_owned())
        );
        assert!(
            policy
                .allowed_rollback_paths
                .contains(&"deployment_or_restore".to_owned())
        );
        assert_eq!(
            RuntimeSidecarErrorCode::RuntimeStoreDecommissionBlocked.category(),
            "quality_gate"
        );
        assert!(!RuntimeSidecarErrorCode::RuntimeStoreDecommissionBlocked.retriable());
    }

    #[test]
    fn checked_in_contract_artifact_matches_rust_canonical_export() {
        let artifact = fs::read_to_string(
            repo_root().join("src/storage/rust_contracts/runtime_sidecar_contract.json"),
        )
        .expect("checked-in runtime sidecar contract artifact must exist");
        assert_eq!(
            artifact,
            runtime_sidecar_contract_json().expect("serialize runtime sidecar contract"),
        );
    }
}

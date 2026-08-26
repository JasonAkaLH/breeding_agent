//! Runtime sidecar service kernel.
//!
//! This crate owns the transport-independent runtime sidecar write semantics.
//! A tonic/gRPC wrapper can delegate to this kernel without reintroducing
//! Python-owned dispatcher, lease, event, cancellation, or bundle state.

use maf_event_log::{EventLog, EventRecord as EventLogRecord};
use maf_runtime_store::{
    ERROR_CODE_TABLE_HASH as RUNTIME_ERROR_CODE_TABLE_HASH, FEATURE_AGENT_STATE,
    FEATURE_ARTIFACT_METADATA, FEATURE_EVENT_LOG, FEATURE_RUNTIME_STORE,
    FEATURE_SUBMISSION_ADMISSION, FEATURE_TASK_DISPATCHER, FEATURE_TASK_READ, LeaseRegistry,
    PROTOCOL_VERSION as RUNTIME_PROTOCOL_VERSION, RuntimeSidecarError, RuntimeSidecarErrorCode,
    SCHEMA_HASH, TaskLease, runtime_sidecar_contract_artifact,
};
use maf_task_dispatcher::{
    TaskDispatcher, TaskSubmitRequest as DispatcherTaskSubmitRequest, TaskSubmitResult,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fs;
use std::future::Future;
use std::io::ErrorKind;
use std::net::SocketAddr;
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use tokio_stream::wrappers::TcpListenerStream;
#[cfg(unix)]
use tokio_stream::wrappers::UnixListenerStream;
use tonic::transport::{Certificate, Identity, ServerTlsConfig};

mod codec;
mod sqlite_adapter;
pub use sqlite_adapter::{
    RuntimeSidecarSqliteAdapter, SubmissionAuthorityFinalizeResult,
    SubmissionAuthorityImportRequest, SubmissionConversationImportRecord,
};

pub mod pb {
    pub mod common {
        pub mod v1 {
            tonic::include_proto!("maf.common.v1");
        }
    }

    pub mod runtime {
        pub mod v1 {
            tonic::include_proto!("maf.runtime.v1");
        }
    }
}

use pb::common::v1 as common_pb;
use pb::runtime::v1 as runtime_pb;

use codec::{
    admit_submission_request_from_pb, admit_submission_response_to_pb, agent_item_record_from_pb,
    agent_item_record_to_pb, agent_run_record_from_pb, agent_run_record_to_pb,
    agent_state_response_to_pb, artifact_from_pb, artifact_response_to_pb,
    bundle_revision_response_to_pb, claim_pending_request_from_pb, claim_pending_response_to_pb,
    close_request_from_pb, close_response_to_pb, cursor_to_pb, get_preparation_request_from_pb,
    get_preparation_response_to_pb, handoff_ack_request_from_pb, health_state_to_pb,
    idempotency_from_pb, lease_response_to_pb, list_artifacts_response_to_pb,
    missing_features_from_error, pb_idempotency_key, prepare_handoff_request_from_pb,
    projection_ack_request_from_pb, readiness_state_to_pb, renew_claim_request_from_pb,
    reserve_request_from_pb, reserve_response_to_pb, submission_admission_response_to_pb,
    submission_claim_response_to_pb, task_node_record_from_pb, task_node_record_to_pb,
    task_record_from_pb, task_record_to_pb, typed_error_to_pb, version_to_pb,
};

pub const COMPONENT_ID: &str = "maf_runtime_sidecar";
pub const PROTOCOL_VERSION: &str = RUNTIME_PROTOCOL_VERSION;
pub const DEFAULT_LISTEN_ADDR: &str = "127.0.0.1:50051";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeSidecarVersion {
    pub component: String,
    pub protocol_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheck {
    pub expected_component: String,
    pub expected_protocol_version: String,
    pub expected_schema_hash: String,
    pub expected_error_code_table_hash: String,
    pub required_features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityResult {
    pub compatible: bool,
    pub missing_features: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum HealthState {
    Serving,
    NotServing,
    Degraded,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReadinessState {
    Ready,
    NotReady,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HealthStatus {
    pub state: HealthState,
    pub version: RuntimeSidecarVersion,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadinessStatus {
    pub state: ReadinessState,
    pub version: RuntimeSidecarVersion,
    pub compatibility_handshake_passed: bool,
    pub shutdown_drain_deadline_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedErrorEnvelope {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Idempotency {
    pub key: String,
    pub owner: String,
    pub deadline_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheckRequest {
    pub client_version: String,
    pub check: CompatibilityCheck,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheckResponse {
    pub compatible: bool,
    pub version: RuntimeSidecarVersion,
    pub missing_features: Vec<String>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmitTaskRequest {
    pub task_id: String,
    pub conversation_id: String,
    pub idempotency: Option<Idempotency>,
    pub task: Option<TaskRecord>,
    pub expected_from_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmitTaskResponse {
    pub task_id: String,
    pub duplicate: bool,
    pub error: Option<TypedErrorEnvelope>,
    pub task: Option<TaskRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskRouteAssignment {
    pub route_mode: String,
    pub real_path: String,
    pub shadow_path: String,
    pub config_version: String,
    pub reason_code: String,
    pub cohort_id: Option<String>,
    pub assignment_key_hash: Option<String>,
    pub assigned_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskRecord {
    pub task_id: String,
    pub conversation_id: String,
    pub root_message_id: String,
    pub status: String,
    pub routing_mode: String,
    pub requested_capability_id: Option<String>,
    pub summary: Option<String>,
    pub cancel_requested_at: Option<String>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    pub assignment: Option<TaskRouteAssignment>,
}

pub const SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES: usize = 64 * 1024;
pub const SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES: usize = 64 * 1024 * 1024;
pub const SUBMISSION_CONTINUATION_MAX_BYTES: usize = 64 * 1024 * 1024;
pub const SUBMISSION_PREPARED_EXECUTION_MAX_BYTES: usize = 128 * 1024;
pub const GRPC_MAX_MESSAGE_BYTES: usize = 140 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionAdmissionDisposition {
    Created,
    IdempotentReplay,
    ConversationBusy,
    MessageIdConflict,
    ConversationNotAvailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionProjectionState {
    Pending,
    Projected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionPreparationState {
    Pending,
    Prepared,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubmissionHandoffState {
    Pending,
    HandedOff,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageIdentityKind {
    Submission,
    Interrupt,
    ServerInternal,
    FileVisible,
    LegacyConflictOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageIdentityDisposition {
    Created,
    ExactReplay,
    Conflict,
    ConversationNotAvailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConversationAdmissionCloseDisposition {
    Closed,
    ExactReplay,
    ConversationNotAvailable,
    Conflict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmissionClaim {
    pub owner: String,
    pub token: String,
    pub expires_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MessageIdentityRecord {
    pub message_id: String,
    pub conversation_id: String,
    pub username: String,
    pub identity_kind: MessageIdentityKind,
    pub role: Option<String>,
    pub message_type: Option<String>,
    pub message_created_at_ms: Option<i64>,
    pub task_id: Option<String>,
    pub request_fingerprint: Option<String>,
    pub reserved_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmissionAdmissionRecord {
    pub message_id: String,
    pub task_id: String,
    pub conversation_id: String,
    pub username: String,
    pub request_fingerprint: String,
    pub conversation_projection_json: Vec<u8>,
    pub message_projection_json: Vec<u8>,
    pub projection_sha256: String,
    pub continuation_json: Vec<u8>,
    pub continuation_sha256: String,
    pub projection_state: SubmissionProjectionState,
    pub preparation_state: SubmissionPreparationState,
    pub prepared_execution_json: Option<Vec<u8>>,
    pub prepared_execution_sha256: Option<String>,
    pub handoff_state: SubmissionHandoffState,
    pub handoff_kind: Option<String>,
    pub handoff_identity: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub closed: bool,
    pub task: TaskRecord,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdmitSubmissionRequest {
    pub message_id: String,
    pub task_id: String,
    pub conversation_id: String,
    pub username: String,
    pub request_fingerprint: String,
    pub conversation_projection_json: Vec<u8>,
    pub message_projection_json: Vec<u8>,
    pub projection_sha256: String,
    pub continuation_json: Vec<u8>,
    pub continuation_sha256: String,
    pub message_created_at_ms: i64,
    pub workflow_owner: String,
    pub now_ms: i64,
    pub claim_ttl_ms: i64,
    pub task: TaskRecord,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdmitSubmissionResponse {
    pub disposition: SubmissionAdmissionDisposition,
    pub admission: Option<SubmissionAdmissionRecord>,
    pub claim: Option<SubmissionClaim>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClaimPendingSubmissionRequest {
    pub workflow_owner: String,
    pub now_ms: i64,
    pub claim_ttl_ms: i64,
    pub after_created_at_ms: Option<i64>,
    pub after_message_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClaimPendingSubmissionResponse {
    pub found: bool,
    pub admission: Option<SubmissionAdmissionRecord>,
    pub claim: Option<SubmissionClaim>,
    pub authority_state: String,
    pub finalization_receipt_sha256: Option<String>,
    pub pending_count: u64,
    pub earliest_claim_expires_at_ms: Option<i64>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenewSubmissionClaimRequest {
    pub message_id: String,
    pub workflow_owner: String,
    pub claim_token: String,
    pub now_ms: i64,
    pub claim_ttl_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmissionAdmissionResponse {
    pub admission: Option<SubmissionAdmissionRecord>,
    pub duplicate: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcknowledgeSubmissionProjectionRequest {
    pub message_id: String,
    pub workflow_owner: String,
    pub claim_token: String,
    pub projection_sha256: String,
    pub expected_state: SubmissionProjectionState,
    pub now_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PrepareSubmissionHandoffRequest {
    pub message_id: String,
    pub workflow_owner: String,
    pub claim_token: String,
    pub prepared_execution_json: Vec<u8>,
    pub prepared_execution_sha256: String,
    pub expected_state: SubmissionPreparationState,
    pub now_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetSubmissionPreparationRequest {
    pub username: String,
    pub conversation_id: String,
    pub task_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetSubmissionPreparationResponse {
    pub found: bool,
    pub admission: Option<SubmissionAdmissionRecord>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcknowledgeSubmissionHandoffRequest {
    pub message_id: String,
    pub workflow_owner: String,
    pub claim_token: String,
    pub prepared_execution_sha256: String,
    pub handoff_kind: String,
    pub handoff_identity: String,
    pub expected_state: SubmissionHandoffState,
    pub now_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloseConversationAdmissionRequest {
    pub username: String,
    pub conversation_id: String,
    pub operation_id: String,
    pub now_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CloseConversationAdmissionResponse {
    pub disposition: ConversationAdmissionCloseDisposition,
    pub revision: u64,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReserveMessageIdentityRequest {
    pub identity: MessageIdentityRecord,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReserveMessageIdentityResponse {
    pub disposition: MessageIdentityDisposition,
    pub identity: Option<MessageIdentityRecord>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetTaskResponse {
    pub task: Option<TaskRecord>,
    pub found: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ListTasksForConversationResponse {
    pub tasks: Vec<TaskRecord>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetActiveTaskForConversationResponse {
    pub task: Option<TaskRecord>,
    pub found: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentRunRecord {
    pub run_id: String,
    pub task_id: String,
    pub conversation_id: String,
    pub status: String,
    pub model_edition: String,
    pub reasoning_effort: String,
    pub thinking_enabled: bool,
    pub binding_option_digests_json: Vec<u8>,
    pub next_item_sequence: u64,
    pub compacted_through_sequence: u64,
    pub active_sample_item_id: Option<String>,
    pub waiting_call_item_ids: Vec<String>,
    pub next_batch_call_ordinal: u64,
    pub claim_owner: Option<String>,
    pub claim_token: Option<String>,
    pub lease_expires_at_ms: Option<i64>,
    pub revision: u64,
    pub terminal_reason_code: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub terminal_at_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentItemRecord {
    pub item_id: String,
    pub run_id: String,
    pub task_id: String,
    pub sequence: u64,
    pub kind: String,
    pub state: String,
    pub payload_json: Vec<u8>,
    pub payload_size_bytes: u64,
    pub payload_sha256: String,
    pub parent_item_id: Option<String>,
    pub source_call_item_id: Option<String>,
    pub provider_sample_id: Option<String>,
    pub call_ordinal: Option<u64>,
    pub created_at_ms: i64,
    pub committed_at_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitAgentStateRequest {
    pub operation: String,
    pub run: Option<AgentRunRecord>,
    pub items: Vec<AgentItemRecord>,
    pub expected_revision: u64,
    pub expected_claim_token: Option<String>,
    pub idempotency: Option<Idempotency>,
    pub task_nodes: Vec<TaskNodeRecord>,
    pub artifacts: Vec<ArtifactRecord>,
    pub final_projection_json: Option<Vec<u8>>,
    pub task: Option<TaskRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentStateResponse {
    pub run: Option<AgentRunRecord>,
    pub items: Vec<AgentItemRecord>,
    pub duplicate: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct AgentStateReceipt {
    pub(crate) operation: String,
    pub(crate) run: AgentRunRecord,
    pub(crate) items: Vec<AgentItemRecord>,
    pub(crate) task_nodes: Vec<TaskNodeRecord>,
    pub(crate) artifacts: Vec<ArtifactRecord>,
    pub(crate) final_projection_json: Option<Vec<u8>>,
    pub(crate) task: Option<TaskRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionNodeRequest {
    pub task_id: String,
    pub node_id: String,
    pub from_status: String,
    pub to_status: String,
    pub idempotency: Option<Idempotency>,
    pub node: Option<TaskNodeRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionNodeResponse {
    pub node_id: String,
    pub status: String,
    pub error: Option<TypedErrorEnvelope>,
    pub node: Option<TaskNodeRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskNodeRecord {
    pub node_id: String,
    pub task_id: String,
    pub capability_id: String,
    pub assigned_instance_id: Option<String>,
    pub status: String,
    pub input_refs: Vec<String>,
    pub output_refs: Vec<String>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetTaskNodeResponse {
    pub node: Option<TaskNodeRecord>,
    pub found: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ListTaskNodesForTaskResponse {
    pub nodes: Vec<TaskNodeRecord>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactRecord {
    pub artifact_id: String,
    pub task_id: String,
    pub producer_node_id: String,
    pub artifact_type: String,
    pub storage_ref: String,
    pub summary: String,
    pub is_complete: bool,
    pub created_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SaveArtifactRequest {
    pub artifact: ArtifactRecord,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GetArtifactRequest {
    pub artifact_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactResponse {
    pub artifact: Option<ArtifactRecord>,
    pub found: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ListArtifactsForTaskRequest {
    pub task_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ListArtifactsForTaskResponse {
    pub artifacts: Vec<ArtifactRecord>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventCursor {
    pub conversation_id: String,
    pub task_id: String,
    pub sequence: u64,
    pub created_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppendEventRequest {
    pub conversation_id: String,
    pub task_id: String,
    pub event_type: String,
    pub payload_json: Vec<u8>,
    pub idempotency: Option<Idempotency>,
    pub created_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppendEventResponse {
    pub cursor: Option<EventCursor>,
    pub error: Option<TypedErrorEnvelope>,
    pub duplicate: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayEventsRequest {
    pub conversation_id: String,
    pub task_id: String,
    pub after_sequence: u64,
    pub page_limit: u32,
    pub byte_limit: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayEventsResponse {
    pub cursors: Vec<EventCursor>,
    pub truncated: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NodeTransitionResult {
    pub task_id: String,
    pub node_id: String,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancellationToken {
    pub task_id: String,
    pub requested_at_ms: i64,
    pub reason: String,
    pub terminal_policy: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BundleRevisionResult {
    pub task_id: String,
    pub bundle_kind: String,
    pub revision: String,
    pub released: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcquireLeaseRequest {
    pub task_id: String,
    pub owner_id: String,
    pub now_ms: i64,
    pub ttl_ms: i64,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenewLeaseRequest {
    pub task_id: String,
    pub renew_token: String,
    pub now_ms: i64,
    pub ttl_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReleaseLeaseRequest {
    pub task_id: String,
    pub renew_token: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeaseResponse {
    pub lease: Option<TaskLease>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReleaseLeaseResponse {
    pub released: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriteCancellationTokenRequest {
    pub task_id: String,
    pub requested_at_ms: i64,
    pub reason: String,
    pub terminal_policy: String,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriteCancellationTokenResponse {
    pub written: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PinBundleRevisionRequest {
    pub task_id: String,
    pub bundle_kind: String,
    pub revision: String,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReleaseBundleRevisionRequest {
    pub task_id: String,
    pub bundle_kind: String,
    pub revision: String,
    pub released_at_ms: i64,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BundleRevisionResponse {
    pub result: Option<BundleRevisionResult>,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeSidecarServeConfig {
    pub listen_addr: SocketAddr,
    pub unix_socket_path: Option<PathBuf>,
    pub sqlite_path: Option<PathBuf>,
    pub tls_config: Option<RuntimeSidecarTlsConfig>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeSidecarTlsConfig {
    pub identity_cert_path: PathBuf,
    pub identity_key_path: PathBuf,
    pub client_ca_path: PathBuf,
}

impl RuntimeSidecarTlsConfig {
    pub fn from_paths(
        identity_cert_path: impl AsRef<Path>,
        identity_key_path: impl AsRef<Path>,
        client_ca_path: impl AsRef<Path>,
    ) -> Result<Self, RuntimeSidecarError> {
        Ok(Self {
            identity_cert_path: validate_mtls_path(
                identity_cert_path.as_ref(),
                "identity certificate",
            )?,
            identity_key_path: validate_mtls_path(identity_key_path.as_ref(), "identity key")?,
            client_ca_path: validate_mtls_path(client_ca_path.as_ref(), "client CA")?,
        })
    }

    fn to_server_tls_config(&self) -> Result<ServerTlsConfig, Box<dyn Error + Send + Sync>> {
        let cert = fs::read(&self.identity_cert_path)?;
        let key = fs::read(&self.identity_key_path)?;
        let client_ca = fs::read(&self.client_ca_path)?;
        Ok(ServerTlsConfig::new()
            .identity(Identity::from_pem(cert, key))
            .client_ca_root(Certificate::from_pem(client_ca)))
    }
}

impl RuntimeSidecarServeConfig {
    pub fn from_env_or_default() -> Result<Self, RuntimeSidecarError> {
        let listen_addr = std::env::var("MAF_RUNTIME_SIDECAR_LISTEN_ADDR")
            .unwrap_or_else(|_| DEFAULT_LISTEN_ADDR.to_owned());
        let tls_cert_path = std::env::var("MAF_RUNTIME_SIDECAR_TLS_CERT_PATH").ok();
        let tls_key_path = std::env::var("MAF_RUNTIME_SIDECAR_TLS_KEY_PATH").ok();
        let tls_client_ca_path = std::env::var("MAF_RUNTIME_SIDECAR_TLS_CLIENT_CA_PATH").ok();
        let mut config = match (
            tls_cert_path.as_deref(),
            tls_key_path.as_deref(),
            tls_client_ca_path.as_deref(),
        ) {
            (Some(cert), Some(key), Some(client_ca)) => {
                Self::from_listen_addr_with_mtls_paths(&listen_addr, cert, key, client_ca)?
            }
            (None, None, None) => Self::from_listen_addr(&listen_addr)?,
            _ => {
                return Err(config_untrusted(
                    "runtime sidecar mTLS env config requires certificate, key, and client CA",
                ));
            }
        };
        if let Ok(sqlite_path) = std::env::var("MAF_RUNTIME_SIDECAR_SQLITE_PATH") {
            config = config.with_sqlite_path(sqlite_path)?;
        }
        Ok(config)
    }

    pub fn from_listen_addr(listen_addr: &str) -> Result<Self, RuntimeSidecarError> {
        if let Some(path) = listen_addr.strip_prefix("unix://") {
            return Self::from_unix_socket_path(path);
        }
        let listen_addr = listen_addr
            .parse::<SocketAddr>()
            .map_err(|_| config_untrusted("runtime sidecar listen address is invalid"))?;
        Self::from_socket_addr(listen_addr)
    }

    pub fn from_listen_addr_with_sqlite_path(
        listen_addr: &str,
        sqlite_path: impl AsRef<Path>,
    ) -> Result<Self, RuntimeSidecarError> {
        Self::from_listen_addr(listen_addr)?.with_sqlite_path(sqlite_path)
    }

    pub fn from_listen_addr_with_mtls_paths(
        listen_addr: &str,
        identity_cert_path: impl AsRef<Path>,
        identity_key_path: impl AsRef<Path>,
        client_ca_path: impl AsRef<Path>,
    ) -> Result<Self, RuntimeSidecarError> {
        if listen_addr.strip_prefix("unix://").is_some() {
            return Err(config_untrusted(
                "runtime sidecar Unix sockets must not be combined with mTLS config",
            ));
        }
        let tls_config = RuntimeSidecarTlsConfig::from_paths(
            identity_cert_path,
            identity_key_path,
            client_ca_path,
        )?;
        let listen_addr = listen_addr
            .parse::<SocketAddr>()
            .map_err(|_| config_untrusted("runtime sidecar listen address is invalid"))?;
        Self::from_socket_addr_with_mtls_config(listen_addr, tls_config)
    }

    pub fn from_socket_addr(listen_addr: SocketAddr) -> Result<Self, RuntimeSidecarError> {
        if !listen_addr.ip().is_loopback() {
            let mut error = config_untrusted(
                "runtime sidecar listener must bind loopback unless mTLS endpoint support is configured",
            );
            error
                .safe_metadata
                .insert("listen_addr".to_owned(), listen_addr.to_string());
            return Err(error);
        }
        Ok(Self {
            listen_addr,
            unix_socket_path: None,
            sqlite_path: None,
            tls_config: None,
        })
    }

    pub fn from_socket_addr_with_mtls_config(
        listen_addr: SocketAddr,
        tls_config: RuntimeSidecarTlsConfig,
    ) -> Result<Self, RuntimeSidecarError> {
        Ok(Self {
            listen_addr,
            unix_socket_path: None,
            sqlite_path: None,
            tls_config: Some(tls_config),
        })
    }

    #[cfg(unix)]
    pub fn from_unix_socket_path(path: impl AsRef<Path>) -> Result<Self, RuntimeSidecarError> {
        let path = path.as_ref();
        if path.as_os_str().is_empty() || !path.is_absolute() {
            return Err(config_untrusted(
                "runtime sidecar unix socket path must be absolute and non-empty",
            ));
        }
        Ok(Self {
            listen_addr: DEFAULT_LISTEN_ADDR
                .parse::<SocketAddr>()
                .expect("default listen addr is valid"),
            unix_socket_path: Some(path.to_path_buf()),
            sqlite_path: None,
            tls_config: None,
        })
    }

    #[cfg(not(unix))]
    pub fn from_unix_socket_path(_path: impl AsRef<Path>) -> Result<Self, RuntimeSidecarError> {
        Err(config_untrusted(
            "runtime sidecar unix sockets are unavailable on this platform",
        ))
    }

    pub fn with_sqlite_path(
        mut self,
        sqlite_path: impl AsRef<Path>,
    ) -> Result<Self, RuntimeSidecarError> {
        let sqlite_path = sqlite_path.as_ref();
        if sqlite_path.as_os_str().is_empty() {
            return Err(config_untrusted(
                "runtime sidecar sqlite path must be non-empty",
            ));
        }
        self.sqlite_path = Some(sqlite_path.to_path_buf());
        Ok(self)
    }

    pub fn with_tls_config(
        mut self,
        tls_config: RuntimeSidecarTlsConfig,
    ) -> Result<Self, RuntimeSidecarError> {
        if self.unix_socket_path.is_some() {
            return Err(config_untrusted(
                "runtime sidecar Unix sockets must not be combined with mTLS config",
            ));
        }
        self.tls_config = Some(tls_config);
        Ok(self)
    }

    pub fn build_service(&self) -> Result<RuntimeSidecarGrpcService, RuntimeSidecarError> {
        match self.sqlite_path.as_ref() {
            Some(sqlite_path) => Ok(RuntimeSidecarGrpcService::with_sqlite_adapter(
                RuntimeSidecarSqliteAdapter::open(sqlite_path)?,
            )),
            None => Ok(RuntimeSidecarGrpcService::new()),
        }
    }
}

fn validate_mtls_path(path: &Path, label: &str) -> Result<PathBuf, RuntimeSidecarError> {
    if path.as_os_str().is_empty() || !path.is_absolute() {
        return Err(config_untrusted(&format!(
            "runtime sidecar mTLS {label} path must be absolute and non-empty"
        )));
    }
    Ok(path.to_path_buf())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct BundlePin {
    revision: String,
    released_at_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct NodeTransitionReceipt {
    task_id: String,
    node_id: String,
    to_status: String,
    expected_from_status: String,
    node: Option<TaskNodeRecord>,
    result: NodeTransitionResult,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SubmissionConversationGuard {
    username: String,
    available: bool,
    revision: u64,
    active_task_id: Option<String>,
    close_operation_id: Option<String>,
    updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SubmissionAdmissionEntry {
    record: SubmissionAdmissionRecord,
    claim: Option<SubmissionClaim>,
    claim_revision: u64,
}

#[derive(Debug)]
pub struct RuntimeSidecarKernel {
    dispatcher: TaskDispatcher,
    event_log: EventLog,
    leases: LeaseRegistry,
    node_statuses: BTreeMap<(String, String), String>,
    task_nodes: BTreeMap<String, TaskNodeRecord>,
    artifacts: BTreeMap<String, ArtifactRecord>,
    cancellation_tokens: BTreeMap<String, CancellationToken>,
    bundle_pins: BTreeMap<(String, String), BundlePin>,
    event_append_idempotency: BTreeMap<String, EventLogRecord>,
    lease_acquire_idempotency: BTreeMap<String, TaskLease>,
    task_submit_idempotency: BTreeMap<String, TaskSubmitResult>,
    task_record_idempotency: BTreeMap<String, TaskRecord>,
    tasks: BTreeMap<String, TaskRecord>,
    agent_runs: BTreeMap<String, AgentRunRecord>,
    agent_task_runs: BTreeMap<String, String>,
    agent_items: BTreeMap<String, AgentItemRecord>,
    agent_final_projections: BTreeMap<String, Vec<u8>>,
    agent_state_idempotency: BTreeMap<String, AgentStateReceipt>,
    dispatched_task_ids: BTreeSet<String>,
    node_transition_idempotency: BTreeMap<String, NodeTransitionReceipt>,
    artifact_idempotency: BTreeMap<String, ArtifactRecord>,
    cancellation_idempotency: BTreeMap<String, bool>,
    bundle_revision_idempotency: BTreeMap<String, BundleRevisionResult>,
    submission_conversations: BTreeMap<String, SubmissionConversationGuard>,
    submission_message_identities: BTreeMap<String, MessageIdentityRecord>,
    submission_admissions: BTreeMap<String, SubmissionAdmissionEntry>,
    submission_idempotency_keys: BTreeMap<String, String>,
    submission_authority_finalization_receipt_sha256: Option<String>,
    compatibility_handshake_passed: bool,
    shutdown_drain_started_at_ms: Option<i64>,
}

impl Default for RuntimeSidecarKernel {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeSidecarKernel {
    #[must_use]
    pub fn new() -> Self {
        Self {
            dispatcher: TaskDispatcher::default(),
            event_log: EventLog::new(),
            leases: LeaseRegistry::new(),
            node_statuses: BTreeMap::new(),
            task_nodes: BTreeMap::new(),
            artifacts: BTreeMap::new(),
            cancellation_tokens: BTreeMap::new(),
            bundle_pins: BTreeMap::new(),
            event_append_idempotency: BTreeMap::new(),
            lease_acquire_idempotency: BTreeMap::new(),
            task_submit_idempotency: BTreeMap::new(),
            task_record_idempotency: BTreeMap::new(),
            tasks: BTreeMap::new(),
            agent_runs: BTreeMap::new(),
            agent_task_runs: BTreeMap::new(),
            agent_items: BTreeMap::new(),
            agent_final_projections: BTreeMap::new(),
            agent_state_idempotency: BTreeMap::new(),
            dispatched_task_ids: BTreeSet::new(),
            node_transition_idempotency: BTreeMap::new(),
            artifact_idempotency: BTreeMap::new(),
            cancellation_idempotency: BTreeMap::new(),
            bundle_revision_idempotency: BTreeMap::new(),
            submission_conversations: BTreeMap::new(),
            submission_message_identities: BTreeMap::new(),
            submission_admissions: BTreeMap::new(),
            submission_idempotency_keys: BTreeMap::new(),
            submission_authority_finalization_receipt_sha256: None,
            compatibility_handshake_passed: false,
            shutdown_drain_started_at_ms: None,
        }
    }

    #[must_use]
    pub fn health(&self) -> HealthStatus {
        HealthStatus {
            state: if self.shutdown_drain_started_at_ms.is_some() {
                HealthState::Degraded
            } else {
                HealthState::Serving
            },
            version: self.version(),
        }
    }

    #[must_use]
    pub fn readiness(&self) -> ReadinessStatus {
        ReadinessStatus {
            state: if self.compatibility_handshake_passed
                && self.shutdown_drain_started_at_ms.is_none()
            {
                ReadinessState::Ready
            } else {
                ReadinessState::NotReady
            },
            version: self.version(),
            compatibility_handshake_passed: self.compatibility_handshake_passed,
            shutdown_drain_deadline_ms: self.shutdown_drain_deadline_ms(),
        }
    }

    #[must_use]
    pub fn shutdown_drain_deadline_ms(&self) -> Option<i64> {
        self.shutdown_drain_started_at_ms
            .map(|started_at_ms| started_at_ms + shutdown_drain_ms())
    }

    pub fn begin_shutdown_drain(&mut self, now_ms: i64) {
        self.shutdown_drain_started_at_ms.get_or_insert(now_ms);
    }

    #[must_use]
    pub fn version(&self) -> RuntimeSidecarVersion {
        RuntimeSidecarVersion {
            component: COMPONENT_ID.to_owned(),
            protocol_version: PROTOCOL_VERSION.to_owned(),
            schema_hash: SCHEMA_HASH.to_owned(),
            error_code_table_hash: RUNTIME_ERROR_CODE_TABLE_HASH.to_owned(),
            supported_features: supported_features(),
        }
    }

    pub fn check_compatibility(
        &self,
        check: CompatibilityCheck,
    ) -> Result<CompatibilityResult, RuntimeSidecarError> {
        let version = self.version();
        if check.expected_component != version.component
            || check.expected_protocol_version != version.protocol_version
            || check.expected_schema_hash != version.schema_hash
            || check.expected_error_code_table_hash != version.error_code_table_hash
        {
            let mut error = RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreProtocolIncompatible,
                "runtime sidecar protocol handshake is incompatible",
            );
            error
                .safe_metadata
                .insert("component".to_owned(), version.component);
            error
                .safe_metadata
                .insert("protocol_version".to_owned(), version.protocol_version);
            return Err(error);
        }

        let supported = version
            .supported_features
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let missing_features = check
            .required_features
            .into_iter()
            .filter(|feature| !supported.contains(feature))
            .collect::<Vec<_>>();
        if !missing_features.is_empty() {
            let mut error = RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreProtocolIncompatible,
                "runtime sidecar required features are missing",
            );
            error
                .safe_metadata
                .insert("missing_features".to_owned(), missing_features.join(","));
            return Err(error);
        }

        Ok(CompatibilityResult {
            compatible: true,
            missing_features,
        })
    }

    pub fn accept_compatibility_handshake(
        &mut self,
        check: CompatibilityCheck,
    ) -> Result<CompatibilityResult, RuntimeSidecarError> {
        let result = self.check_compatibility(check)?;
        self.compatibility_handshake_passed = true;
        Ok(result)
    }

    #[must_use]
    pub fn new_with_finalized_submission_authority(
        finalization_receipt_sha256: impl Into<String>,
    ) -> Self {
        let mut kernel = Self::new();
        kernel.submission_authority_finalization_receipt_sha256 =
            Some(finalization_receipt_sha256.into());
        kernel
    }

    pub fn admit_submission(
        &mut self,
        request: AdmitSubmissionRequest,
    ) -> Result<AdmitSubmissionResponse, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        if self
            .submission_authority_finalization_receipt_sha256
            .is_none()
        {
            return Err(migration_blocked(
                "submission authority must be finalized before online admission",
            ));
        }
        validate_admit_submission_request(&request)?;
        let message_projection: serde_json::Value =
            serde_json::from_slice(&request.message_projection_json)
                .map_err(|_| write_failed("message projection JSON is invalid"))?;
        let message_role = message_projection["role"]
            .as_str()
            .ok_or_else(|| write_failed("message projection role is invalid"))?
            .to_owned();
        let message_type = message_projection["message_type"]
            .as_str()
            .ok_or_else(|| write_failed("message projection type is invalid"))?
            .to_owned();

        if let Some(identity) = self.submission_message_identities.get(&request.message_id) {
            let is_replay = identity.identity_kind == MessageIdentityKind::Submission
                && identity.conversation_id == request.conversation_id
                && identity.username == request.username
                && identity.role.as_deref() == Some(message_role.as_str())
                && identity.message_type.as_deref() == Some(message_type.as_str())
                && identity.request_fingerprint.as_deref()
                    == Some(request.request_fingerprint.as_str());
            if !is_replay {
                return Ok(admission_disposition(
                    SubmissionAdmissionDisposition::MessageIdConflict,
                ));
            }
            let entry = self
                .submission_admissions
                .get(&request.message_id)
                .ok_or_else(|| write_failed("submission identity is missing its admission"))?;
            if entry.record.idempotency_key != request.idempotency_key {
                return Ok(admission_disposition(
                    SubmissionAdmissionDisposition::MessageIdConflict,
                ));
            }
            let claim = entry.claim.as_ref().and_then(|claim| {
                (claim.owner == request.workflow_owner && claim.expires_at_ms > request.now_ms)
                    .then(|| claim.clone())
            });
            return Ok(AdmitSubmissionResponse {
                disposition: SubmissionAdmissionDisposition::IdempotentReplay,
                admission: Some(entry.record.clone()),
                claim,
                error: None,
            });
        }

        if self
            .submission_idempotency_keys
            .get(&request.idempotency_key)
            .is_some_and(|message_id| message_id != &request.message_id)
        {
            return Err(idempotency_conflict(
                "submission idempotency key is bound to a different Message",
            ));
        }

        if let Some(guard) = self.submission_conversations.get(&request.conversation_id)
            && (guard.username != request.username || !guard.available)
        {
            return Ok(admission_disposition(
                SubmissionAdmissionDisposition::ConversationNotAvailable,
            ));
        }

        if self
            .submission_conversations
            .get(&request.conversation_id)
            .and_then(|guard| guard.active_task_id.as_ref())
            .is_some()
        {
            return Ok(admission_disposition(
                SubmissionAdmissionDisposition::ConversationBusy,
            ));
        }
        if self.tasks.contains_key(&request.task_id)
            || self
                .submission_admissions
                .values()
                .any(|entry| entry.record.task_id == request.task_id)
        {
            return Err(idempotency_conflict(
                "submission Task identity is already authoritative",
            ));
        }

        let claim = new_submission_claim(
            &request.message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            1,
        )?;
        let identity = MessageIdentityRecord {
            message_id: request.message_id.clone(),
            conversation_id: request.conversation_id.clone(),
            username: request.username.clone(),
            identity_kind: MessageIdentityKind::Submission,
            role: Some(message_role),
            message_type: Some(message_type),
            message_created_at_ms: Some(request.message_created_at_ms),
            task_id: Some(request.task_id.clone()),
            request_fingerprint: Some(request.request_fingerprint.clone()),
            reserved_at_ms: request.now_ms,
        };
        let record = SubmissionAdmissionRecord {
            message_id: request.message_id.clone(),
            task_id: request.task_id.clone(),
            conversation_id: request.conversation_id.clone(),
            username: request.username.clone(),
            request_fingerprint: request.request_fingerprint,
            conversation_projection_json: request.conversation_projection_json,
            message_projection_json: request.message_projection_json,
            projection_sha256: request.projection_sha256,
            continuation_json: request.continuation_json,
            continuation_sha256: request.continuation_sha256,
            projection_state: SubmissionProjectionState::Pending,
            preparation_state: SubmissionPreparationState::Pending,
            prepared_execution_json: None,
            prepared_execution_sha256: None,
            handoff_state: SubmissionHandoffState::Pending,
            handoff_kind: None,
            handoff_identity: None,
            created_at_ms: request.now_ms,
            updated_at_ms: request.now_ms,
            closed: false,
            task: request.task.clone(),
            idempotency_key: request.idempotency_key.clone(),
        };
        self.submission_conversations
            .entry(request.conversation_id.clone())
            .and_modify(|guard| {
                guard.active_task_id = Some(request.task_id.clone());
                guard.revision += 1;
                guard.updated_at_ms = request.now_ms;
            })
            .or_insert(SubmissionConversationGuard {
                username: request.username,
                available: true,
                revision: 1,
                active_task_id: Some(request.task_id.clone()),
                close_operation_id: None,
                updated_at_ms: request.now_ms,
            });
        self.submission_message_identities
            .insert(request.message_id.clone(), identity);
        self.tasks.insert(request.task_id, request.task);
        self.submission_admissions.insert(
            request.message_id,
            SubmissionAdmissionEntry {
                record: record.clone(),
                claim: Some(claim.clone()),
                claim_revision: 1,
            },
        );
        self.submission_idempotency_keys
            .insert(request.idempotency_key, record.message_id.clone());
        Ok(AdmitSubmissionResponse {
            disposition: SubmissionAdmissionDisposition::Created,
            admission: Some(record),
            claim: Some(claim),
            error: None,
        })
    }

    pub fn claim_pending_submission(
        &mut self,
        request: ClaimPendingSubmissionRequest,
    ) -> Result<ClaimPendingSubmissionResponse, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let receipt = self
            .submission_authority_finalization_receipt_sha256
            .clone();
        if receipt.is_none() {
            return Err(migration_blocked(
                "submission authority must be finalized before recovery claims",
            ));
        }
        validate_claim_timing(
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
        )?;
        if request.after_created_at_ms.is_some() != request.after_message_id.is_some() {
            return Err(write_failed(
                "submission recovery cursor fields must be all-or-none",
            ));
        }
        let mut candidates = self
            .submission_admissions
            .iter()
            .filter(|(_, entry)| {
                !entry.record.closed
                    && entry.record.handoff_state != SubmissionHandoffState::HandedOff
            })
            .map(|(message_id, entry)| (entry.record.created_at_ms, message_id.clone()))
            .filter(|(created_at_ms, message_id)| {
                match (
                    request.after_created_at_ms,
                    request.after_message_id.as_deref(),
                ) {
                    (None, None) => true,
                    (Some(after), Some(after_id)) => {
                        (*created_at_ms, message_id.as_str()) > (after, after_id)
                    }
                    _ => false,
                }
            })
            .collect::<Vec<_>>();
        candidates.sort();
        let pending_count = candidates.len() as u64;
        let Some((_, message_id)) = candidates.into_iter().next() else {
            return Ok(ClaimPendingSubmissionResponse {
                found: false,
                admission: None,
                claim: None,
                authority_state: "finalized".to_owned(),
                finalization_receipt_sha256: receipt,
                pending_count: 0,
                earliest_claim_expires_at_ms: None,
                error: None,
            });
        };
        if let Some(expires_at_ms) = self
            .submission_admissions
            .get(&message_id)
            .and_then(|entry| entry.claim.as_ref())
            .map(|claim| claim.expires_at_ms)
            .filter(|expires_at_ms| *expires_at_ms > request.now_ms)
        {
            return Ok(ClaimPendingSubmissionResponse {
                found: false,
                admission: None,
                claim: None,
                authority_state: "finalized".to_owned(),
                finalization_receipt_sha256: receipt,
                pending_count,
                earliest_claim_expires_at_ms: Some(expires_at_ms),
                error: None,
            });
        }
        let entry = self
            .submission_admissions
            .get_mut(&message_id)
            .ok_or_else(|| write_failed("pending submission disappeared"))?;
        entry.claim_revision += 1;
        let claim = new_submission_claim(
            &message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            entry.claim_revision,
        )?;
        entry.claim = Some(claim.clone());
        Ok(ClaimPendingSubmissionResponse {
            found: true,
            admission: Some(entry.record.clone()),
            claim: Some(claim),
            authority_state: "finalized".to_owned(),
            finalization_receipt_sha256: receipt,
            pending_count,
            earliest_claim_expires_at_ms: None,
            error: None,
        })
    }

    pub fn renew_submission_claim(
        &mut self,
        request: RenewSubmissionClaimRequest,
    ) -> Result<SubmissionClaim, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        validate_claim_timing(
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
        )?;
        let entry = self
            .submission_admissions
            .get_mut(&request.message_id)
            .ok_or_else(|| idempotency_conflict("submission claim identity is unknown"))?;
        if entry.record.closed || entry.record.handoff_state == SubmissionHandoffState::HandedOff {
            return Err(idempotency_conflict(
                "closed or handed-off submission claims cannot be renewed",
            ));
        }
        validate_submission_claim(
            entry,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
        )?;
        entry.claim_revision += 1;
        let claim = new_submission_claim(
            &request.message_id,
            &request.workflow_owner,
            request.now_ms,
            request.claim_ttl_ms,
            entry.claim_revision,
        )?;
        entry.claim = Some(claim.clone());
        Ok(claim)
    }

    pub fn acknowledge_submission_projection(
        &mut self,
        request: AcknowledgeSubmissionProjectionRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let entry = self.submission_entry_for_claim_mut(
            &request.message_id,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
        )?;
        if entry.record.projection_sha256 != request.projection_sha256 {
            return Err(idempotency_conflict(
                "submission projection digest mismatch",
            ));
        }
        if entry.record.projection_state == SubmissionProjectionState::Projected {
            return Ok((entry.record.clone(), true));
        }
        if request.expected_state != SubmissionProjectionState::Pending {
            return Err(idempotency_conflict(
                "submission projection expected state mismatch",
            ));
        }
        entry.record.projection_state = SubmissionProjectionState::Projected;
        entry.record.updated_at_ms = request.now_ms;
        Ok((entry.record.clone(), false))
    }

    pub fn prepare_submission_handoff(
        &mut self,
        request: PrepareSubmissionHandoffRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        validate_prepared_execution(
            &request.prepared_execution_json,
            &request.prepared_execution_sha256,
        )?;
        let prepared: serde_json::Value = serde_json::from_slice(&request.prepared_execution_json)
            .map_err(|_| write_failed("prepared execution JSON is invalid"))?;
        let entry = self.submission_entry_for_claim_mut(
            &request.message_id,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
        )?;
        if prepared["message_id"].as_str() != Some(entry.record.message_id.as_str())
            || prepared["task_id"].as_str() != Some(entry.record.task_id.as_str())
            || prepared["conversation_id"].as_str() != Some(entry.record.conversation_id.as_str())
            || prepared["owner_scope"].as_str() != Some(entry.record.username.as_str())
            || prepared["requested_capability_id"].as_str()
                != entry.record.task.requested_capability_id.as_deref()
        {
            return Err(write_failed("prepared execution identity mismatch"));
        }
        if entry.record.projection_state != SubmissionProjectionState::Projected {
            return Err(idempotency_conflict(
                "submission must be projected before preparation",
            ));
        }
        if entry.record.preparation_state == SubmissionPreparationState::Prepared {
            if entry.record.prepared_execution_json.as_deref()
                != Some(request.prepared_execution_json.as_slice())
                || entry.record.prepared_execution_sha256.as_deref()
                    != Some(request.prepared_execution_sha256.as_str())
            {
                return Err(idempotency_conflict(
                    "submission prepared snapshot is immutable",
                ));
            }
            return Ok((entry.record.clone(), true));
        }
        if request.expected_state != SubmissionPreparationState::Pending {
            return Err(idempotency_conflict(
                "submission preparation expected state mismatch",
            ));
        }
        entry.record.preparation_state = SubmissionPreparationState::Prepared;
        entry.record.prepared_execution_json = Some(request.prepared_execution_json);
        entry.record.prepared_execution_sha256 = Some(request.prepared_execution_sha256);
        entry.record.updated_at_ms = request.now_ms;
        Ok((entry.record.clone(), false))
    }

    #[must_use]
    pub fn get_submission_preparation(
        &self,
        request: &GetSubmissionPreparationRequest,
    ) -> Option<SubmissionAdmissionRecord> {
        self.submission_admissions.values().find_map(|entry| {
            (entry.record.username == request.username
                && entry.record.conversation_id == request.conversation_id
                && entry.record.task_id == request.task_id
                && entry.record.preparation_state == SubmissionPreparationState::Prepared)
                .then(|| entry.record.clone())
        })
    }

    pub fn acknowledge_submission_handoff(
        &mut self,
        request: AcknowledgeSubmissionHandoffRequest,
    ) -> Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        if !matches!(
            request.handoff_kind.as_str(),
            "agent_run" | "interrupt" | "no_server_intent"
        ) || request.handoff_identity.trim().is_empty()
        {
            return Err(write_failed("submission handoff identity is invalid"));
        }
        let entry = self
            .submission_admissions
            .get_mut(&request.message_id)
            .ok_or_else(|| idempotency_conflict("submission admission is unknown"))?;
        validate_submission_claim(
            entry,
            &request.workflow_owner,
            &request.claim_token,
            request.now_ms,
        )?;
        if entry.record.closed {
            return Err(idempotency_conflict("submission admission is closed"));
        }
        if entry.record.preparation_state != SubmissionPreparationState::Prepared
            || entry.record.prepared_execution_sha256.as_deref()
                != Some(request.prepared_execution_sha256.as_str())
        {
            return Err(idempotency_conflict(
                "submission prepared handoff digest mismatch",
            ));
        }
        if entry.record.handoff_state == SubmissionHandoffState::HandedOff {
            if entry.record.handoff_kind.as_deref() != Some(request.handoff_kind.as_str())
                || entry.record.handoff_identity.as_deref()
                    != Some(request.handoff_identity.as_str())
            {
                return Err(idempotency_conflict(
                    "submission durable handoff is immutable",
                ));
            }
            return Ok((entry.record.clone(), true));
        }
        if request.expected_state != SubmissionHandoffState::Pending {
            return Err(idempotency_conflict(
                "submission handoff expected state mismatch",
            ));
        }
        entry.record.handoff_state = SubmissionHandoffState::HandedOff;
        entry.record.handoff_kind = Some(request.handoff_kind);
        entry.record.handoff_identity = Some(request.handoff_identity);
        entry.record.updated_at_ms = request.now_ms;
        Ok((entry.record.clone(), false))
    }

    pub fn close_conversation_admission(
        &mut self,
        request: CloseConversationAdmissionRequest,
    ) -> Result<CloseConversationAdmissionResponse, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        if request.username.trim().is_empty()
            || request.conversation_id.trim().is_empty()
            || request.operation_id.trim().is_empty()
        {
            return Err(write_failed("conversation close identity is invalid"));
        }
        let Some(guard) = self
            .submission_conversations
            .get_mut(&request.conversation_id)
        else {
            return Ok(CloseConversationAdmissionResponse {
                disposition: ConversationAdmissionCloseDisposition::ConversationNotAvailable,
                revision: 0,
                error: None,
            });
        };
        if guard.username != request.username {
            return Ok(CloseConversationAdmissionResponse {
                disposition: ConversationAdmissionCloseDisposition::ConversationNotAvailable,
                revision: guard.revision,
                error: None,
            });
        }
        if !guard.available {
            let disposition =
                if guard.close_operation_id.as_deref() == Some(request.operation_id.as_str()) {
                    ConversationAdmissionCloseDisposition::ExactReplay
                } else {
                    ConversationAdmissionCloseDisposition::Conflict
                };
            return Ok(CloseConversationAdmissionResponse {
                disposition,
                revision: guard.revision,
                error: None,
            });
        }
        guard.available = false;
        guard.close_operation_id = Some(request.operation_id);
        guard.revision += 1;
        guard.updated_at_ms = request.now_ms;
        let active_task_id = guard.active_task_id.take();
        let revision = guard.revision;
        let should_cancel_active_task = active_task_id.as_ref().is_some_and(|task_id| {
            self.submission_admissions.values().any(|entry| {
                entry.record.task_id == *task_id
                    && entry.record.handoff_state != SubmissionHandoffState::HandedOff
            })
        });
        for entry in self.submission_admissions.values_mut().filter(|entry| {
            entry.record.conversation_id == request.conversation_id
                && entry.record.handoff_state != SubmissionHandoffState::HandedOff
        }) {
            entry.record.closed = true;
            entry.record.updated_at_ms = request.now_ms;
            entry.claim = None;
        }
        if should_cancel_active_task
            && let Some(task_id) = active_task_id
            && let Some(task) = self.tasks.get_mut(&task_id)
            && task.status == "accepted"
        {
            let mut cancelling = task.clone();
            cancelling.status = "cancelling".to_owned();
            cancelling.cancel_requested_at = Some(request.now_ms.to_string());
            cancelling.updated_at = Some(request.now_ms.to_string());
            validate_task_update(task, &cancelling)?;
            let mut cancelled = cancelling.clone();
            cancelled.status = "cancelled".to_owned();
            validate_task_update(&cancelling, &cancelled)?;
            *task = cancelled;
            if let Some(entry) = self
                .submission_admissions
                .values_mut()
                .find(|entry| entry.record.task_id == task_id)
            {
                entry.record.task = task.clone();
            }
        }
        Ok(CloseConversationAdmissionResponse {
            disposition: ConversationAdmissionCloseDisposition::Closed,
            revision,
            error: None,
        })
    }

    pub fn reserve_message_identity(
        &mut self,
        request: ReserveMessageIdentityRequest,
    ) -> Result<ReserveMessageIdentityResponse, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        if self
            .submission_authority_finalization_receipt_sha256
            .is_none()
        {
            return Err(migration_blocked(
                "submission authority must be finalized before Message identity reservation",
            ));
        }
        validate_message_identity(&request.identity, false)?;
        if let Some(existing) = self
            .submission_message_identities
            .get(&request.identity.message_id)
        {
            let exact = message_identity_is_exact_replay(existing, &request.identity);
            return Ok(ReserveMessageIdentityResponse {
                disposition: if exact {
                    MessageIdentityDisposition::ExactReplay
                } else {
                    MessageIdentityDisposition::Conflict
                },
                identity: exact.then(|| existing.clone()),
                error: None,
            });
        }
        if let Some(guard) = self
            .submission_conversations
            .get(&request.identity.conversation_id)
            && (guard.username != request.identity.username || !guard.available)
        {
            return Ok(ReserveMessageIdentityResponse {
                disposition: MessageIdentityDisposition::ConversationNotAvailable,
                identity: None,
                error: None,
            });
        }
        self.submission_conversations
            .entry(request.identity.conversation_id.clone())
            .or_insert(SubmissionConversationGuard {
                username: request.identity.username.clone(),
                available: true,
                revision: 1,
                active_task_id: None,
                close_operation_id: None,
                updated_at_ms: request.identity.reserved_at_ms,
            });
        self.submission_message_identities.insert(
            request.identity.message_id.clone(),
            request.identity.clone(),
        );
        Ok(ReserveMessageIdentityResponse {
            disposition: MessageIdentityDisposition::Created,
            identity: Some(request.identity),
            error: None,
        })
    }

    fn submission_entry_for_claim_mut(
        &mut self,
        message_id: &str,
        owner: &str,
        token: &str,
        now_ms: i64,
    ) -> Result<&mut SubmissionAdmissionEntry, RuntimeSidecarError> {
        let entry = self
            .submission_admissions
            .get_mut(message_id)
            .ok_or_else(|| idempotency_conflict("submission admission is unknown"))?;
        validate_submission_claim(entry, owner, token, now_ms)?;
        if entry.record.closed {
            return Err(idempotency_conflict("submission admission is closed"));
        }
        Ok(entry)
    }

    pub fn submit_task(
        &mut self,
        task_id: impl Into<String>,
        conversation_id: impl Into<String>,
        idempotency_key: impl Into<String>,
    ) -> Result<TaskSubmitResult, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(result) = self.task_submit_idempotency.get(&idempotency_key) {
            return Ok(result.clone());
        }
        let result = self.dispatcher.submit(DispatcherTaskSubmitRequest {
            task_id: task_id.into(),
            conversation_id: conversation_id.into(),
            idempotency_key: idempotency_key.clone(),
        })?;
        self.dispatched_task_ids.insert(result.task_id.clone());
        self.task_submit_idempotency
            .insert(idempotency_key, result.clone());
        Ok(result)
    }

    pub fn submit_task_record(
        &mut self,
        task: TaskRecord,
        idempotency_key: impl Into<String>,
        expected_from_status: Option<&str>,
    ) -> Result<(TaskRecord, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        validate_task_record(&task)?;
        if let Some(original) = self.task_record_idempotency.get(&idempotency_key) {
            if original != &task {
                return Err(idempotency_conflict(
                    "task submit idempotency key was reused with a different TaskRecord",
                ));
            }
            return Ok((original.clone(), true));
        }
        if let Some(existing) = self.tasks.get(&task.task_id) {
            validate_expected_status(expected_from_status, Some(&existing.status))?;
            if !self.initial_no_server_task_transition_is_authorized(existing, &task) {
                validate_task_update(existing, &task)?;
            }
        } else {
            validate_expected_status(expected_from_status, None)?;
            if task.status == "accepted"
                && self
                    .submission_authority_finalization_receipt_sha256
                    .is_some()
                && !self
                    .submission_admissions
                    .values()
                    .any(|entry| entry.record.task_id == task.task_id)
                && !self
                    .submission_conversations
                    .values()
                    .any(|guard| guard.active_task_id.as_deref() == Some(task.task_id.as_str()))
            {
                return Err(migration_blocked(
                    "new accepted Task requires submission admission or import evidence",
                ));
            }
        }
        if self.dispatched_task_ids.insert(task.task_id.clone()) {
            self.dispatcher.submit(DispatcherTaskSubmitRequest {
                task_id: task.task_id.clone(),
                conversation_id: task.conversation_id.clone(),
                idempotency_key: idempotency_key.clone(),
            })?;
        }
        self.tasks.insert(task.task_id.clone(), task.clone());
        self.sync_submission_task_and_release_terminal_guard(&task);
        self.task_record_idempotency
            .insert(idempotency_key, task.clone());
        Ok((task, false))
    }

    #[must_use]
    pub fn get_task(&self, task_id: &str) -> Option<TaskRecord> {
        self.tasks.get(task_id).cloned()
    }

    pub fn commit_agent_state(
        &mut self,
        request: CommitAgentStateRequest,
    ) -> Result<(AgentRunRecord, Vec<AgentItemRecord>, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key(request.idempotency))?;
        let run = request
            .run
            .ok_or_else(|| write_failed("AgentRunRecord is required"))?;
        validate_agent_run_record(&run)?;
        for item in &request.items {
            validate_agent_item_record(item, &run)?;
        }
        validate_agent_final_commit_shape(
            &request.operation,
            &run,
            &request.items,
            &request.task_nodes,
            &request.artifacts,
            request.task.as_ref(),
            request.final_projection_json.as_deref(),
        )?;
        if let Some(receipt) = self.agent_state_idempotency.get(&idempotency_key) {
            if receipt.operation != request.operation
                || receipt.run != run
                || receipt.items != request.items
                || receipt.task_nodes != request.task_nodes
                || receipt.artifacts != request.artifacts
                || receipt.final_projection_json != request.final_projection_json
                || receipt.task != request.task
            {
                return Err(idempotency_conflict(
                    "agent state idempotency key was reused with different state",
                ));
            }
            return Ok((receipt.run.clone(), receipt.items.clone(), true));
        }
        if request.operation == "create_run" {
            if request.expected_revision != 0 || run.revision != 0 || !request.items.is_empty() {
                return Err(write_failed("create_run Agent state shape is invalid"));
            }
            if self.agent_runs.contains_key(&run.run_id)
                || self.agent_task_runs.contains_key(&run.task_id)
            {
                return Err(write_failed("Task already has an AgentRun"));
            }
        } else {
            let existing = self
                .agent_runs
                .get(&run.run_id)
                .ok_or_else(|| write_failed("AgentRun is missing"))?;
            if existing.task_id != run.task_id
                || existing.conversation_id != run.conversation_id
                || existing.revision != request.expected_revision
                || existing.claim_token != request.expected_claim_token
                || run.revision != request.expected_revision + 1
            {
                return Err(write_failed(
                    "Agent state CAS or immutable identity mismatch",
                ));
            }
        }
        let existing_items = self
            .agent_items
            .values()
            .filter(|item| item.run_id == run.run_id)
            .cloned()
            .collect::<Vec<_>>();
        validate_agent_item_relationships(&existing_items, &request.items)?;
        let mut sequences = BTreeMap::new();
        for existing in &existing_items {
            sequences.insert(existing.sequence, existing.item_id.clone());
        }
        let mut request_item_ids = BTreeSet::new();
        for item in &request.items {
            if !request_item_ids.insert(item.item_id.clone()) {
                return Err(write_failed("AgentItem identity or sequence conflict"));
            }
            if let Some(existing) = self.agent_items.get(&item.item_id) {
                validate_agent_item_update(existing, item)?;
            } else if let Some(existing_item_id) = sequences.get(&item.sequence) {
                if existing_item_id != &item.item_id {
                    return Err(write_failed("AgentItem identity or sequence conflict"));
                }
            } else {
                sequences.insert(item.sequence, item.item_id.clone());
            }
        }
        for node in &request.task_nodes {
            validate_task_node_record(node)?;
            if node.task_id != run.task_id {
                return Err(write_failed("Agent TaskNode belongs to a different Task"));
            }
            if let Some(existing) = self.task_nodes.get(&node.node_id) {
                validate_task_node_update(existing, node)?;
            }
        }
        for artifact in &request.artifacts {
            if artifact.artifact_id.is_empty()
                || artifact.producer_node_id.is_empty()
                || artifact.artifact_type.is_empty()
                || artifact.storage_ref.is_empty()
                || artifact.task_id != run.task_id
            {
                return Err(write_failed("Agent Artifact belongs to a different Task"));
            }
            if self.artifacts.contains_key(&artifact.artifact_id)
                || (!self.task_nodes.contains_key(&artifact.producer_node_id)
                    && !request
                        .task_nodes
                        .iter()
                        .any(|node| node.node_id == artifact.producer_node_id))
            {
                return Err(write_failed("Agent Artifact identity or producer conflict"));
            }
        }
        if let Some(projection) = &request.final_projection_json {
            if request.operation != "commit_final"
                || self.agent_final_projections.contains_key(&run.run_id)
            {
                return Err(write_failed("Agent final projection operation conflict"));
            }
            validate_agent_final_projection(
                projection,
                &run,
                &request.task_nodes,
                &request.artifacts,
                &request.items,
            )?;
        } else if request.operation == "commit_final" {
            return Err(write_failed("Agent final projection is required"));
        }
        if let Some(task) = &request.task {
            validate_task_record(task)?;
            if task.task_id != run.task_id || task.conversation_id != run.conversation_id {
                return Err(write_failed("Agent Task projection identity mismatch"));
            }
            if let Some(existing) = self.tasks.get(&task.task_id)
                && !self.initial_no_server_task_transition_is_authorized(existing, task)
            {
                validate_task_update(existing, task)?;
            } else if !self.tasks.contains_key(&task.task_id)
                && task.status == "accepted"
                && self
                    .submission_authority_finalization_receipt_sha256
                    .is_some()
                && !self.task_has_submission_or_import_evidence(&task.task_id)
            {
                return Err(migration_blocked(
                    "new accepted Task requires submission admission or import evidence",
                ));
            }
        }
        self.agent_task_runs
            .insert(run.task_id.clone(), run.run_id.clone());
        self.agent_runs.insert(run.run_id.clone(), run.clone());
        for item in &request.items {
            self.agent_items.insert(item.item_id.clone(), item.clone());
        }
        for node in &request.task_nodes {
            self.task_nodes.insert(node.node_id.clone(), node.clone());
            self.node_statuses.insert(
                (node.task_id.clone(), node.node_id.clone()),
                node.status.clone(),
            );
        }
        for artifact in &request.artifacts {
            self.artifacts
                .insert(artifact.artifact_id.clone(), artifact.clone());
        }
        if let Some(projection) = &request.final_projection_json {
            self.agent_final_projections
                .insert(run.run_id.clone(), projection.clone());
        }
        if let Some(task) = &request.task {
            self.tasks.insert(task.task_id.clone(), task.clone());
            self.sync_submission_task_and_release_terminal_guard(task);
        }
        self.agent_state_idempotency.insert(
            idempotency_key,
            AgentStateReceipt {
                operation: request.operation,
                run: run.clone(),
                items: request.items.clone(),
                task_nodes: request.task_nodes,
                artifacts: request.artifacts,
                final_projection_json: request.final_projection_json,
                task: request.task,
            },
        );
        Ok((run, request.items, false))
    }

    fn sync_submission_task_and_release_terminal_guard(&mut self, task: &TaskRecord) {
        if let Some(entry) = self
            .submission_admissions
            .values_mut()
            .find(|entry| entry.record.task_id == task.task_id)
        {
            entry.record.task = task.clone();
        }
        if matches!(task.status.as_str(), "completed" | "failed" | "cancelled")
            && let Some(guard) = self.submission_conversations.get_mut(&task.conversation_id)
            && guard.active_task_id.as_deref() == Some(task.task_id.as_str())
        {
            guard.active_task_id = None;
            guard.revision += 1;
        }
    }

    fn initial_no_server_task_transition_is_authorized(
        &self,
        existing: &TaskRecord,
        replacement: &TaskRecord,
    ) -> bool {
        initial_no_server_task_transition_shape(existing, replacement)
            && self.submission_admissions.values().any(|entry| {
                entry.record.task_id == existing.task_id
                    && !entry.record.closed
                    && entry.record.preparation_state == SubmissionPreparationState::Prepared
                    && entry.record.handoff_state == SubmissionHandoffState::Pending
                    && entry
                        .record
                        .prepared_execution_json
                        .as_deref()
                        .is_some_and(|bytes| {
                            serde_json::from_slice::<serde_json::Value>(bytes)
                                .ok()
                                .and_then(|value| {
                                    value
                                        .get("prepared_kind")
                                        .and_then(serde_json::Value::as_str)
                                        .map(str::to_owned)
                                })
                                .as_deref()
                                == Some("no_server_intent")
                        })
                    && serde_json::from_slice::<serde_json::Value>(&entry.record.continuation_json)
                        .ok()
                        .and_then(|value| {
                            value
                                .get("initial_no_server_eligible")
                                .and_then(serde_json::Value::as_bool)
                        })
                        == Some(true)
                    && entry
                        .record
                        .prepared_execution_json
                        .as_deref()
                        .is_some_and(|bytes| {
                            serde_json::from_slice::<serde_json::Value>(bytes)
                                .ok()
                                .and_then(|value| {
                                    value
                                        .get("planned_handoff_kind")
                                        .and_then(serde_json::Value::as_str)
                                        .map(str::to_owned)
                                })
                                .as_deref()
                                == Some("no_server_intent")
                        })
            })
    }

    fn task_has_submission_or_import_evidence(&self, task_id: &str) -> bool {
        self.submission_admissions
            .values()
            .any(|entry| entry.record.task_id == task_id)
            || self
                .submission_conversations
                .values()
                .any(|guard| guard.active_task_id.as_deref() == Some(task_id))
    }

    #[must_use]
    pub fn get_agent_final_projection(&self, run_id: &str) -> Option<Vec<u8>> {
        self.agent_final_projections.get(run_id).cloned()
    }

    #[must_use]
    pub fn get_agent_run_for_task(&self, task_id: &str) -> Option<AgentRunRecord> {
        self.agent_task_runs
            .get(task_id)
            .and_then(|run_id| self.agent_runs.get(run_id))
            .cloned()
    }

    #[must_use]
    pub fn get_agent_run(&self, run_id: &str) -> Option<AgentRunRecord> {
        self.agent_runs.get(run_id).cloned()
    }

    #[must_use]
    pub fn list_agent_runs(&self, statuses: &BTreeSet<String>) -> Vec<AgentRunRecord> {
        let mut runs: Vec<_> = self
            .agent_runs
            .values()
            .filter(|run| statuses.is_empty() || statuses.contains(&run.status))
            .cloned()
            .collect();
        runs.sort_by(|left, right| left.run_id.cmp(&right.run_id));
        runs
    }

    #[must_use]
    pub fn list_agent_items(&self, run_id: &str) -> Vec<AgentItemRecord> {
        let mut items: Vec<_> = self
            .agent_items
            .values()
            .filter(|item| item.run_id == run_id)
            .cloned()
            .collect();
        items.sort_by_key(|item| item.sequence);
        items
    }

    #[must_use]
    pub fn list_tasks_for_conversation(
        &self,
        conversation_id: &str,
        statuses: &[String],
    ) -> Vec<TaskRecord> {
        let status_filter = statuses.iter().collect::<BTreeSet<_>>();
        let mut tasks = self
            .tasks
            .values()
            .filter(|task| {
                task.conversation_id == conversation_id
                    && (status_filter.is_empty() || status_filter.contains(&task.status))
            })
            .cloned()
            .collect::<Vec<_>>();
        tasks.sort_by(|left, right| {
            right
                .created_at
                .cmp(&left.created_at)
                .then_with(|| right.task_id.cmp(&left.task_id))
        });
        tasks
    }

    #[must_use]
    pub fn get_active_task_for_conversation(&self, conversation_id: &str) -> Option<TaskRecord> {
        self.list_tasks_for_conversation(
            conversation_id,
            &[
                "accepted".to_owned(),
                "planning".to_owned(),
                "running".to_owned(),
                "cancelling".to_owned(),
            ],
        )
        .into_iter()
        .next()
    }

    pub fn transition_node(
        &mut self,
        task_id: impl Into<String>,
        node_id: impl Into<String>,
        to_status: impl Into<String>,
        expected_from_status: impl Into<String>,
        idempotency_key: impl Into<String>,
        node: Option<TaskNodeRecord>,
    ) -> Result<NodeTransitionResult, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let task_id = task_id.into();
        let node_id = node_id.into();
        let to_status = to_status.into();
        let expected_from_status = expected_from_status.into();
        let requested_node = node.clone();
        if let Some(receipt) = self.node_transition_idempotency.get(&idempotency_key) {
            if receipt.task_id != task_id
                || receipt.node_id != node_id
                || receipt.to_status != to_status
                || receipt.expected_from_status != expected_from_status
                || receipt.node != node
            {
                return Err(idempotency_conflict(
                    "node transition idempotency key was reused with a different request",
                ));
            }
            return Ok(receipt.result.clone());
        }
        validate_expected_status(
            Some(&expected_from_status),
            self.task_nodes
                .get(&node_id)
                .map(|existing| &existing.status),
        )?;
        if let Some(existing) = self.task_nodes.get(&node_id) {
            if existing.task_id != task_id {
                return Err(write_failed("TaskNodeRecord cannot move between tasks"));
            }
            if node.is_none()
                && node_status_is_terminal(&existing.status)
                && existing.status != to_status
            {
                return Err(write_failed(
                    "terminal TaskNodeRecord status cannot be changed",
                ));
            }
        }
        if let Some(node) = node {
            validate_task_node_record(&node)?;
            if node.task_id != task_id || node.node_id != node_id || node.status != to_status {
                return Err(write_failed(
                    "TransitionNode identity does not match TaskNodeRecord",
                ));
            }
            if let Some(existing) = self.task_nodes.get(&node.node_id) {
                validate_task_node_update(existing, &node)?;
            }
            self.task_nodes.insert(node.node_id.clone(), node.clone());
        }
        let result = NodeTransitionResult {
            task_id: task_id.clone(),
            node_id: node_id.clone(),
            status: to_status.clone(),
        };
        self.node_statuses.insert(
            (result.task_id.clone(), result.node_id.clone()),
            result.status.clone(),
        );
        self.node_transition_idempotency.insert(
            idempotency_key,
            NodeTransitionReceipt {
                task_id,
                node_id,
                to_status,
                expected_from_status,
                node: requested_node,
                result: result.clone(),
            },
        );
        Ok(result)
    }

    #[must_use]
    pub fn get_task_node(&self, node_id: &str) -> Option<TaskNodeRecord> {
        self.task_nodes.get(node_id).cloned()
    }

    #[must_use]
    pub fn list_task_nodes_for_task(&self, task_id: &str) -> Vec<TaskNodeRecord> {
        self.task_nodes
            .values()
            .filter(|node| node.task_id == task_id)
            .cloned()
            .collect()
    }

    pub fn save_artifact(
        &mut self,
        artifact: ArtifactRecord,
        idempotency_key: impl Into<String>,
    ) -> Result<ArtifactRecord, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(result) = self.artifact_idempotency.get(&idempotency_key) {
            return Ok(result.clone());
        }
        self.artifacts
            .insert(artifact.artifact_id.clone(), artifact.clone());
        self.artifact_idempotency
            .insert(idempotency_key, artifact.clone());
        Ok(artifact)
    }

    #[must_use]
    pub fn get_artifact(&self, artifact_id: &str) -> Option<ArtifactRecord> {
        self.artifacts.get(artifact_id).cloned()
    }

    #[must_use]
    pub fn list_artifacts_for_task(&self, task_id: &str) -> Vec<ArtifactRecord> {
        self.artifacts
            .values()
            .filter(|artifact| artifact.task_id == task_id)
            .cloned()
            .collect()
    }

    pub fn append_event(
        &mut self,
        conversation_id: impl Into<String>,
        task_id: impl Into<String>,
        event_type: impl Into<String>,
        payload_json: Vec<u8>,
        created_at_ms: i64,
        idempotency_key: impl Into<String>,
    ) -> Result<EventCursor, RuntimeSidecarError> {
        self.append_event_exact(
            conversation_id,
            task_id,
            event_type,
            payload_json,
            created_at_ms,
            idempotency_key,
        )
        .map(|(cursor, _duplicate)| cursor)
    }

    pub fn append_event_exact(
        &mut self,
        conversation_id: impl Into<String>,
        task_id: impl Into<String>,
        event_type: impl Into<String>,
        payload_json: Vec<u8>,
        created_at_ms: i64,
        idempotency_key: impl Into<String>,
    ) -> Result<(EventCursor, bool), RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        let conversation_id = conversation_id.into();
        let task_id = task_id.into();
        let event_type = event_type.into();
        if let Some(event) = self.event_append_idempotency.get(&idempotency_key) {
            if event.conversation_id != conversation_id
                || event.task_id != task_id
                || event.event_type != event_type
                || event.payload_json != payload_json
                || event.created_at_ms != created_at_ms
            {
                return Err(idempotency_conflict(
                    "event append idempotency payload differs",
                ));
            }
            return Ok((
                EventCursor {
                    conversation_id: event.conversation_id.clone(),
                    task_id: event.task_id.clone(),
                    sequence: event.sequence,
                    created_at_ms: event.created_at_ms,
                },
                true,
            ));
        }
        let event = self.event_log.append(
            conversation_id,
            task_id,
            event_type,
            payload_json,
            created_at_ms,
        )?;
        let cursor = EventCursor {
            conversation_id: event.conversation_id.clone(),
            task_id: event.task_id.clone(),
            sequence: event.sequence,
            created_at_ms: event.created_at_ms,
        };
        self.event_append_idempotency.insert(idempotency_key, event);
        Ok((cursor, false))
    }

    pub fn replay_events(
        &self,
        conversation_id: &str,
        task_id: &str,
        after_sequence: u64,
        max_events: usize,
        max_bytes: usize,
    ) -> Result<Vec<EventCursor>, RuntimeSidecarError> {
        self.event_log
            .replay(
                conversation_id,
                task_id,
                after_sequence,
                max_events,
                max_bytes,
            )
            .map(|events| {
                events
                    .into_iter()
                    .map(|event| EventCursor {
                        conversation_id: event.conversation_id,
                        task_id: event.task_id,
                        sequence: event.sequence,
                        created_at_ms: event.created_at_ms,
                    })
                    .collect()
            })
    }

    pub fn acquire_lease(
        &mut self,
        task_id: impl Into<String>,
        owner_id: impl Into<String>,
        now_ms: i64,
        ttl_ms: i64,
        idempotency_key: impl Into<String>,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(lease) = self.lease_acquire_idempotency.get(&idempotency_key) {
            return Ok(lease.clone());
        }
        let lease = self.leases.acquire(task_id, owner_id, now_ms, ttl_ms)?;
        self.lease_acquire_idempotency
            .insert(idempotency_key, lease.clone());
        Ok(lease)
    }

    pub fn renew_lease(
        &mut self,
        task_id: &str,
        renew_token: &str,
        now_ms: i64,
        ttl_ms: i64,
    ) -> Result<TaskLease, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        self.leases.renew(task_id, renew_token, now_ms, ttl_ms)
    }

    pub fn release_lease(
        &mut self,
        task_id: &str,
        renew_token: &str,
    ) -> Result<bool, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        self.leases.release(task_id, renew_token)
    }

    pub fn write_cancellation_token(
        &mut self,
        task_id: impl Into<String>,
        requested_at_ms: i64,
        reason: impl Into<String>,
        terminal_policy: impl Into<String>,
        idempotency_key: impl Into<String>,
    ) -> Result<bool, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(written) = self.cancellation_idempotency.get(&idempotency_key) {
            return Ok(*written);
        }
        let token = CancellationToken {
            task_id: task_id.into(),
            requested_at_ms,
            reason: reason.into(),
            terminal_policy: terminal_policy.into(),
        };
        self.cancellation_tokens
            .insert(token.task_id.clone(), token);
        self.cancellation_idempotency.insert(idempotency_key, true);
        Ok(true)
    }

    #[must_use]
    pub fn cancellation_token(&self, task_id: &str) -> Option<CancellationToken> {
        self.cancellation_tokens.get(task_id).cloned()
    }

    pub fn pin_bundle_revision(
        &mut self,
        task_id: impl Into<String>,
        bundle_kind: impl Into<String>,
        revision: impl Into<String>,
        idempotency_key: impl Into<String>,
    ) -> Result<BundleRevisionResult, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(result) = self.bundle_revision_idempotency.get(&idempotency_key) {
            return Ok(result.clone());
        }
        let task_id = task_id.into();
        let bundle_kind = bundle_kind.into();
        let revision = revision.into();
        self.bundle_pins.insert(
            (task_id.clone(), bundle_kind.clone()),
            BundlePin {
                revision: revision.clone(),
                released_at_ms: None,
            },
        );
        let result = BundleRevisionResult {
            task_id,
            bundle_kind,
            revision,
            released: false,
        };
        self.bundle_revision_idempotency
            .insert(idempotency_key, result.clone());
        Ok(result)
    }

    pub fn release_bundle_revision(
        &mut self,
        task_id: impl Into<String>,
        bundle_kind: impl Into<String>,
        revision: impl Into<String>,
        released_at_ms: i64,
        idempotency_key: impl Into<String>,
    ) -> Result<BundleRevisionResult, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(result) = self.bundle_revision_idempotency.get(&idempotency_key) {
            return Ok(result.clone());
        }
        let task_id = task_id.into();
        let bundle_kind = bundle_kind.into();
        let revision = revision.into();
        let Some(pin) = self
            .bundle_pins
            .get_mut(&(task_id.clone(), bundle_kind.clone()))
        else {
            return Err(write_failed("bundle revision pin is missing"));
        };
        if pin.revision != revision {
            return Err(write_failed("bundle revision does not match active pin"));
        }
        pin.released_at_ms = Some(released_at_ms);
        let result = BundleRevisionResult {
            task_id,
            bundle_kind,
            revision,
            released: true,
        };
        self.bundle_revision_idempotency
            .insert(idempotency_key, result.clone());
        Ok(result)
    }

    fn ensure_accepting_writes(&self) -> Result<(), RuntimeSidecarError> {
        if self.shutdown_drain_started_at_ms.is_some() {
            return Err(RuntimeSidecarError::new(
                RuntimeSidecarErrorCode::RuntimeStoreUnavailable,
                "runtime sidecar is draining and not accepting new writes",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Default)]
pub struct RuntimeSidecarService {
    kernel: RuntimeSidecarKernel,
}

impl RuntimeSidecarService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            kernel: RuntimeSidecarKernel::new(),
        }
    }

    #[must_use]
    pub fn new_with_finalized_submission_authority(
        finalization_receipt_sha256: impl Into<String>,
    ) -> Self {
        Self {
            kernel: RuntimeSidecarKernel::new_with_finalized_submission_authority(
                finalization_receipt_sha256,
            ),
        }
    }

    #[must_use]
    pub fn version(&self) -> RuntimeSidecarVersion {
        self.kernel.version()
    }

    #[must_use]
    pub fn health(&self) -> HealthStatus {
        self.kernel.health()
    }

    #[must_use]
    pub fn readiness(&self) -> ReadinessStatus {
        self.kernel.readiness()
    }

    pub fn begin_shutdown_drain(&mut self, now_ms: i64) {
        self.kernel.begin_shutdown_drain(now_ms);
    }

    fn ensure_accepting_writes(&self) -> Result<(), RuntimeSidecarError> {
        self.kernel.ensure_accepting_writes()
    }

    pub fn check_compatibility(
        &mut self,
        request: CompatibilityCheckRequest,
    ) -> CompatibilityCheckResponse {
        let _client_version = request.client_version;
        match self.kernel.accept_compatibility_handshake(request.check) {
            Ok(result) => CompatibilityCheckResponse {
                compatible: result.compatible,
                version: self.kernel.version(),
                missing_features: result.missing_features,
                error: None,
            },
            Err(error) => CompatibilityCheckResponse {
                compatible: false,
                version: self.kernel.version(),
                missing_features: missing_features_from_error(&error),
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn submit_task(&mut self, request: SubmitTaskRequest) -> SubmitTaskResponse {
        let idempotency_key = idempotency_key(request.idempotency);
        let result = match request.task {
            Some(task) => {
                validate_submit_task_identity(&request.task_id, &request.conversation_id, &task)
                    .and_then(|()| {
                        self.kernel.submit_task_record(
                            task,
                            idempotency_key,
                            request.expected_from_status.as_deref(),
                        )
                    })
                    .map(|(task, duplicate)| (task.task_id.clone(), duplicate, Some(task)))
            }
            None => self
                .kernel
                .submit_task(request.task_id, request.conversation_id, idempotency_key)
                .map(|result| (result.task_id, result.duplicate, None)),
        };
        match result {
            Ok((task_id, duplicate, task)) => SubmitTaskResponse {
                task_id,
                duplicate,
                error: None,
                task,
            },
            Err(error) => SubmitTaskResponse {
                task_id: String::new(),
                duplicate: false,
                error: Some(TypedErrorEnvelope::from(error)),
                task: None,
            },
        }
    }

    #[must_use]
    pub fn get_task(&self, task_id: &str) -> GetTaskResponse {
        let task = self.kernel.get_task(task_id);
        GetTaskResponse {
            found: task.is_some(),
            task,
            error: None,
        }
    }

    #[must_use]
    pub fn list_tasks_for_conversation(
        &self,
        conversation_id: &str,
        statuses: &[String],
    ) -> ListTasksForConversationResponse {
        ListTasksForConversationResponse {
            tasks: self
                .kernel
                .list_tasks_for_conversation(conversation_id, statuses),
            error: None,
        }
    }

    #[must_use]
    pub fn get_active_task_for_conversation(
        &self,
        conversation_id: &str,
    ) -> GetActiveTaskForConversationResponse {
        let task = self
            .kernel
            .get_active_task_for_conversation(conversation_id);
        GetActiveTaskForConversationResponse {
            found: task.is_some(),
            task,
            error: None,
        }
    }

    pub fn commit_agent_state(&mut self, request: CommitAgentStateRequest) -> AgentStateResponse {
        match self.kernel.commit_agent_state(request) {
            Ok((run, items, duplicate)) => AgentStateResponse {
                run: Some(run),
                items,
                duplicate,
                error: None,
            },
            Err(error) => AgentStateResponse {
                run: None,
                items: Vec::new(),
                duplicate: false,
                error: Some(error.into()),
            },
        }
    }

    #[must_use]
    pub fn get_agent_run(&self, run_id: &str) -> AgentStateResponse {
        let run = self.kernel.get_agent_run(run_id);
        AgentStateResponse {
            duplicate: false,
            items: Vec::new(),
            error: None,
            run,
        }
    }

    #[must_use]
    pub fn list_agent_items(&self, run_id: &str) -> AgentStateResponse {
        AgentStateResponse {
            run: None,
            items: self.kernel.list_agent_items(run_id),
            duplicate: false,
            error: None,
        }
    }

    pub fn transition_node(&mut self, request: TransitionNodeRequest) -> TransitionNodeResponse {
        match self.kernel.transition_node(
            request.task_id,
            request.node_id,
            request.to_status,
            request.from_status,
            idempotency_key(request.idempotency),
            request.node.clone(),
        ) {
            Ok(result) => TransitionNodeResponse {
                node_id: result.node_id,
                status: result.status,
                error: None,
                node: request.node,
            },
            Err(error) => TransitionNodeResponse {
                node_id: String::new(),
                status: String::new(),
                error: Some(TypedErrorEnvelope::from(error)),
                node: None,
            },
        }
    }

    #[must_use]
    pub fn get_task_node(&self, node_id: &str) -> GetTaskNodeResponse {
        let node = self.kernel.get_task_node(node_id);
        GetTaskNodeResponse {
            found: node.is_some(),
            node,
            error: None,
        }
    }

    #[must_use]
    pub fn list_task_nodes_for_task(&self, task_id: &str) -> ListTaskNodesForTaskResponse {
        ListTaskNodesForTaskResponse {
            nodes: self.kernel.list_task_nodes_for_task(task_id),
            error: None,
        }
    }

    pub fn save_artifact(&mut self, request: SaveArtifactRequest) -> ArtifactResponse {
        match self
            .kernel
            .save_artifact(request.artifact, idempotency_key(request.idempotency))
        {
            Ok(artifact) => ArtifactResponse {
                artifact: Some(artifact),
                found: true,
                error: None,
            },
            Err(error) => ArtifactResponse {
                artifact: None,
                found: false,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn get_artifact(&self, request: GetArtifactRequest) -> ArtifactResponse {
        let artifact = self.kernel.get_artifact(&request.artifact_id);
        ArtifactResponse {
            found: artifact.is_some(),
            artifact,
            error: None,
        }
    }

    pub fn list_artifacts_for_task(
        &self,
        request: ListArtifactsForTaskRequest,
    ) -> ListArtifactsForTaskResponse {
        ListArtifactsForTaskResponse {
            artifacts: self.kernel.list_artifacts_for_task(&request.task_id),
            error: None,
        }
    }

    pub fn append_event(&mut self, request: AppendEventRequest) -> AppendEventResponse {
        match self.kernel.append_event_exact(
            request.conversation_id,
            request.task_id,
            request.event_type,
            request.payload_json,
            request.created_at_ms,
            idempotency_key(request.idempotency),
        ) {
            Ok((cursor, duplicate)) => AppendEventResponse {
                cursor: Some(cursor),
                error: None,
                duplicate,
            },
            Err(error) => AppendEventResponse {
                cursor: None,
                error: Some(TypedErrorEnvelope::from(error)),
                duplicate: false,
            },
        }
    }

    pub fn replay_events(&self, request: ReplayEventsRequest) -> ReplayEventsResponse {
        let requested_limit = request.page_limit as usize;
        match self.kernel.replay_events(
            &request.conversation_id,
            &request.task_id,
            request.after_sequence,
            requested_limit,
            request.byte_limit as usize,
        ) {
            Ok(cursors) => ReplayEventsResponse {
                truncated: requested_limit > 0 && cursors.len() == requested_limit,
                cursors,
                error: None,
            },
            Err(error) => ReplayEventsResponse {
                cursors: Vec::new(),
                truncated: false,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn acquire_lease(&mut self, request: AcquireLeaseRequest) -> LeaseResponse {
        match self.kernel.acquire_lease(
            request.task_id,
            request.owner_id,
            request.now_ms,
            request.ttl_ms,
            idempotency_key(request.idempotency),
        ) {
            Ok(lease) => LeaseResponse {
                lease: Some(lease),
                error: None,
            },
            Err(error) => LeaseResponse {
                lease: None,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn renew_lease(&mut self, request: RenewLeaseRequest) -> LeaseResponse {
        match self.kernel.renew_lease(
            &request.task_id,
            &request.renew_token,
            request.now_ms,
            request.ttl_ms,
        ) {
            Ok(lease) => LeaseResponse {
                lease: Some(lease),
                error: None,
            },
            Err(error) => LeaseResponse {
                lease: None,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn release_lease(&mut self, request: ReleaseLeaseRequest) -> ReleaseLeaseResponse {
        match self
            .kernel
            .release_lease(&request.task_id, &request.renew_token)
        {
            Ok(released) => ReleaseLeaseResponse {
                released,
                error: None,
            },
            Err(error) => ReleaseLeaseResponse {
                released: false,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn write_cancellation_token(
        &mut self,
        request: WriteCancellationTokenRequest,
    ) -> WriteCancellationTokenResponse {
        match self.kernel.write_cancellation_token(
            request.task_id,
            request.requested_at_ms,
            request.reason,
            request.terminal_policy,
            idempotency_key(request.idempotency),
        ) {
            Ok(written) => WriteCancellationTokenResponse {
                written,
                error: None,
            },
            Err(error) => WriteCancellationTokenResponse {
                written: false,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn pin_bundle_revision(
        &mut self,
        request: PinBundleRevisionRequest,
    ) -> BundleRevisionResponse {
        match self.kernel.pin_bundle_revision(
            request.task_id,
            request.bundle_kind,
            request.revision,
            idempotency_key(request.idempotency),
        ) {
            Ok(result) => BundleRevisionResponse {
                result: Some(result),
                error: None,
            },
            Err(error) => BundleRevisionResponse {
                result: None,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn release_bundle_revision(
        &mut self,
        request: ReleaseBundleRevisionRequest,
    ) -> BundleRevisionResponse {
        match self.kernel.release_bundle_revision(
            request.task_id,
            request.bundle_kind,
            request.revision,
            request.released_at_ms,
            idempotency_key(request.idempotency),
        ) {
            Ok(result) => BundleRevisionResponse {
                result: Some(result),
                error: None,
            },
            Err(error) => BundleRevisionResponse {
                result: None,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn admit_submission(&mut self, request: AdmitSubmissionRequest) -> AdmitSubmissionResponse {
        self.kernel
            .admit_submission(request)
            .unwrap_or_else(|error| {
                let mut response =
                    admission_disposition(SubmissionAdmissionDisposition::ConversationNotAvailable);
                response.error = Some(error.into());
                response
            })
    }

    pub fn claim_pending_submission(
        &mut self,
        request: ClaimPendingSubmissionRequest,
    ) -> ClaimPendingSubmissionResponse {
        self.kernel
            .claim_pending_submission(request)
            .unwrap_or_else(|error| ClaimPendingSubmissionResponse {
                found: false,
                admission: None,
                claim: None,
                authority_state: "uninitialized".to_owned(),
                finalization_receipt_sha256: None,
                pending_count: 0,
                earliest_claim_expires_at_ms: None,
                error: Some(error.into()),
            })
    }

    pub fn renew_submission_claim(
        &mut self,
        request: RenewSubmissionClaimRequest,
    ) -> Result<SubmissionClaim, TypedErrorEnvelope> {
        self.kernel
            .renew_submission_claim(request)
            .map_err(Into::into)
    }

    pub fn acknowledge_submission_projection(
        &mut self,
        request: AcknowledgeSubmissionProjectionRequest,
    ) -> SubmissionAdmissionResponse {
        submission_admission_result(self.kernel.acknowledge_submission_projection(request))
    }

    pub fn prepare_submission_handoff(
        &mut self,
        request: PrepareSubmissionHandoffRequest,
    ) -> SubmissionAdmissionResponse {
        submission_admission_result(self.kernel.prepare_submission_handoff(request))
    }

    #[must_use]
    pub fn get_submission_preparation(
        &self,
        request: &GetSubmissionPreparationRequest,
    ) -> GetSubmissionPreparationResponse {
        let admission = self.kernel.get_submission_preparation(request);
        GetSubmissionPreparationResponse {
            found: admission.is_some(),
            admission,
            error: None,
        }
    }

    pub fn acknowledge_submission_handoff(
        &mut self,
        request: AcknowledgeSubmissionHandoffRequest,
    ) -> SubmissionAdmissionResponse {
        submission_admission_result(self.kernel.acknowledge_submission_handoff(request))
    }

    pub fn close_conversation_admission(
        &mut self,
        request: CloseConversationAdmissionRequest,
    ) -> CloseConversationAdmissionResponse {
        self.kernel
            .close_conversation_admission(request)
            .unwrap_or_else(|error| CloseConversationAdmissionResponse {
                disposition: ConversationAdmissionCloseDisposition::Conflict,
                revision: 0,
                error: Some(error.into()),
            })
    }

    pub fn reserve_message_identity(
        &mut self,
        request: ReserveMessageIdentityRequest,
    ) -> ReserveMessageIdentityResponse {
        self.kernel
            .reserve_message_identity(request)
            .unwrap_or_else(|error| ReserveMessageIdentityResponse {
                disposition: MessageIdentityDisposition::Conflict,
                identity: None,
                error: Some(error.into()),
            })
    }
}

fn submission_admission_result(
    result: Result<(SubmissionAdmissionRecord, bool), RuntimeSidecarError>,
) -> SubmissionAdmissionResponse {
    match result {
        Ok((admission, duplicate)) => SubmissionAdmissionResponse {
            admission: Some(admission),
            duplicate,
            error: None,
        },
        Err(error) => SubmissionAdmissionResponse {
            admission: None,
            duplicate: false,
            error: Some(error.into()),
        },
    }
}

impl From<RuntimeSidecarError> for TypedErrorEnvelope {
    fn from(error: RuntimeSidecarError) -> Self {
        Self {
            code: error.code,
            message: error.message,
            retriable: error.retriable,
            category: error.category,
            safe_metadata: error.safe_metadata,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct RuntimeSidecarGrpcService {
    inner: Arc<Mutex<RuntimeSidecarService>>,
    sqlite_adapter: Option<Arc<RuntimeSidecarSqliteAdapter>>,
}

impl RuntimeSidecarGrpcService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(RuntimeSidecarService::new())),
            sqlite_adapter: None,
        }
    }

    #[must_use]
    pub fn new_with_finalized_submission_authority(
        finalization_receipt_sha256: impl Into<String>,
    ) -> Self {
        Self {
            inner: Arc::new(Mutex::new(
                RuntimeSidecarService::new_with_finalized_submission_authority(
                    finalization_receipt_sha256,
                ),
            )),
            sqlite_adapter: None,
        }
    }

    #[must_use]
    pub fn with_sqlite_adapter(sqlite_adapter: RuntimeSidecarSqliteAdapter) -> Self {
        Self {
            inner: Arc::new(Mutex::new(RuntimeSidecarService::new())),
            sqlite_adapter: Some(Arc::new(sqlite_adapter)),
        }
    }

    pub fn begin_shutdown_drain(&self, now_ms: i64) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.begin_shutdown_drain(now_ms);
        }
    }

    fn lock(&self) -> Result<MutexGuard<'_, RuntimeSidecarService>, tonic::Status> {
        self.inner
            .lock()
            .map_err(|_| tonic::Status::internal("runtime sidecar service lock is poisoned"))
    }

    fn run_sqlite_write<T>(
        &self,
        write: impl FnOnce() -> Result<T, RuntimeSidecarError>,
    ) -> Result<Result<T, RuntimeSidecarError>, tonic::Status> {
        let inner = self.lock()?;
        if let Err(error) = inner.ensure_accepting_writes() {
            return Ok(Err(error));
        }
        let result = write();
        drop(inner);
        Ok(result)
    }
}

#[tonic::async_trait]
impl runtime_pb::runtime_sidecar_server::RuntimeSidecar for RuntimeSidecarGrpcService {
    async fn health(
        &self,
        _request: tonic::Request<runtime_pb::HealthRequest>,
    ) -> Result<tonic::Response<runtime_pb::HealthResponse>, tonic::Status> {
        let status = self.lock()?.health();
        Ok(tonic::Response::new(runtime_pb::HealthResponse {
            state: health_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            error: None,
        }))
    }

    async fn readiness(
        &self,
        _request: tonic::Request<runtime_pb::ReadinessRequest>,
    ) -> Result<tonic::Response<runtime_pb::ReadinessResponse>, tonic::Status> {
        let status = self.lock()?.readiness();
        Ok(tonic::Response::new(runtime_pb::ReadinessResponse {
            state: readiness_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            compatibility_handshake_passed: status.compatibility_handshake_passed,
            error: None,
        }))
    }

    async fn version(
        &self,
        _request: tonic::Request<runtime_pb::VersionRequest>,
    ) -> Result<tonic::Response<runtime_pb::VersionResponse>, tonic::Status> {
        let version = self.lock()?.version();
        Ok(tonic::Response::new(runtime_pb::VersionResponse {
            version: Some(version_to_pb(version)),
        }))
    }

    async fn check_compatibility(
        &self,
        request: tonic::Request<runtime_pb::CompatibilityCheckRequest>,
    ) -> Result<tonic::Response<runtime_pb::CompatibilityCheckResponse>, tonic::Status> {
        let request = request.into_inner();
        let response = self.lock()?.check_compatibility(CompatibilityCheckRequest {
            client_version: request.client_version,
            check: CompatibilityCheck {
                expected_component: request.expected_component,
                expected_protocol_version: request.expected_protocol_version,
                expected_schema_hash: request.expected_schema_hash,
                expected_error_code_table_hash: request.expected_error_code_table_hash,
                required_features: request.required_features,
            },
        });
        Ok(tonic::Response::new(
            runtime_pb::CompatibilityCheckResponse {
                compatible: response.compatible,
                version: Some(version_to_pb(response.version)),
                missing_features: response.missing_features,
                error: response.error.map(typed_error_to_pb),
            },
        ))
    }

    async fn submit_task(
        &self,
        request: tonic::Request<runtime_pb::SubmitTaskRequest>,
    ) -> Result<tonic::Response<runtime_pb::SubmitTaskResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let task = request.task.clone().map(task_record_from_pb);
            let response = match self.run_sqlite_write(|| match task {
                Some(task) => {
                    validate_submit_task_identity(&request.task_id, &request.conversation_id, &task)
                        .and_then(|()| {
                            adapter.submit_task_record(
                                task,
                                &pb_idempotency_key(request.idempotency.clone()),
                                request.expected_from_status.as_deref(),
                            )
                        })
                }
                None => adapter
                    .submit_task(
                        &request.task_id,
                        &request.conversation_id,
                        &pb_idempotency_key(request.idempotency.clone()),
                    )
                    .map(|result| (None, result)),
            })? {
                Ok((task, result)) => SubmitTaskResponse {
                    task_id: result.task_id,
                    duplicate: result.duplicate,
                    error: None,
                    task,
                },
                Err(error) => SubmitTaskResponse {
                    task_id: String::new(),
                    duplicate: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                    task: None,
                },
            };
            return Ok(tonic::Response::new(runtime_pb::SubmitTaskResponse {
                task_id: response.task_id,
                duplicate: response.duplicate,
                error: response.error.map(typed_error_to_pb),
                task: response.task.map(task_record_to_pb),
            }));
        }
        let response = self.lock()?.submit_task(SubmitTaskRequest {
            task_id: request.task_id,
            conversation_id: request.conversation_id,
            idempotency: request.idempotency.map(idempotency_from_pb),
            task: request.task.map(task_record_from_pb),
            expected_from_status: request.expected_from_status,
        });
        Ok(tonic::Response::new(runtime_pb::SubmitTaskResponse {
            task_id: response.task_id,
            duplicate: response.duplicate,
            error: response.error.map(typed_error_to_pb),
            task: response.task.map(task_record_to_pb),
        }))
    }

    async fn get_task(
        &self,
        request: tonic::Request<runtime_pb::GetTaskRequest>,
    ) -> Result<tonic::Response<runtime_pb::GetTaskResponse>, tonic::Status> {
        let task_id = request.into_inner().task_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_task(&task_id) {
                Ok(task) => GetTaskResponse {
                    found: task.is_some(),
                    task,
                    error: None,
                },
                Err(error) => GetTaskResponse {
                    found: false,
                    task: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            }
        } else {
            self.lock()?.get_task(&task_id)
        };
        Ok(tonic::Response::new(runtime_pb::GetTaskResponse {
            task: response.task.map(task_record_to_pb),
            found: response.found,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn list_tasks_for_conversation(
        &self,
        request: tonic::Request<runtime_pb::ListTasksForConversationRequest>,
    ) -> Result<tonic::Response<runtime_pb::ListTasksForConversationResponse>, tonic::Status> {
        let request = request.into_inner();
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.list_tasks_for_conversation(&request.conversation_id, &request.statuses) {
                Ok(tasks) => ListTasksForConversationResponse { tasks, error: None },
                Err(error) => ListTasksForConversationResponse {
                    tasks: Vec::new(),
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            }
        } else {
            self.lock()?
                .list_tasks_for_conversation(&request.conversation_id, &request.statuses)
        };
        Ok(tonic::Response::new(
            runtime_pb::ListTasksForConversationResponse {
                tasks: response.tasks.into_iter().map(task_record_to_pb).collect(),
                error: response.error.map(typed_error_to_pb),
            },
        ))
    }

    async fn get_active_task_for_conversation(
        &self,
        request: tonic::Request<runtime_pb::GetActiveTaskForConversationRequest>,
    ) -> Result<tonic::Response<runtime_pb::GetActiveTaskForConversationResponse>, tonic::Status>
    {
        let conversation_id = request.into_inner().conversation_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_active_task_for_conversation(&conversation_id) {
                Ok(task) => GetActiveTaskForConversationResponse {
                    found: task.is_some(),
                    task,
                    error: None,
                },
                Err(error) => GetActiveTaskForConversationResponse {
                    found: false,
                    task: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            }
        } else {
            self.lock()?
                .get_active_task_for_conversation(&conversation_id)
        };
        Ok(tonic::Response::new(
            runtime_pb::GetActiveTaskForConversationResponse {
                task: response.task.map(task_record_to_pb),
                found: response.found,
                error: response.error.map(typed_error_to_pb),
            },
        ))
    }

    async fn commit_agent_state(
        &self,
        request: tonic::Request<runtime_pb::CommitAgentStateRequest>,
    ) -> Result<tonic::Response<runtime_pb::AgentStateResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let run = request.run.map(agent_run_record_from_pb);
            let items: Vec<_> = request
                .items
                .into_iter()
                .map(agent_item_record_from_pb)
                .collect();
            let task_nodes: Vec<_> = request
                .task_nodes
                .into_iter()
                .map(task_node_record_from_pb)
                .collect();
            let artifacts: Vec<_> = request
                .artifacts
                .into_iter()
                .map(artifact_from_pb)
                .collect();
            let final_projection_json = request.final_projection_json;
            let task = request.task.map(task_record_from_pb);
            let response = match run {
                Some(run) => match self.run_sqlite_write(|| {
                    adapter.commit_agent_state(CommitAgentStateRequest {
                        operation: request.operation,
                        run: Some(run),
                        items,
                        expected_revision: request.expected_revision,
                        expected_claim_token: request.expected_claim_token,
                        idempotency: request.idempotency.map(idempotency_from_pb),
                        task_nodes,
                        artifacts,
                        final_projection_json,
                        task,
                    })
                })? {
                    Ok((run, items, duplicate)) => AgentStateResponse {
                        run: Some(run),
                        items,
                        duplicate,
                        error: None,
                    },
                    Err(error) => AgentStateResponse {
                        run: None,
                        items: Vec::new(),
                        duplicate: false,
                        error: Some(error.into()),
                    },
                },
                None => AgentStateResponse {
                    run: None,
                    items: Vec::new(),
                    duplicate: false,
                    error: Some(write_failed("AgentRunRecord is required").into()),
                },
            };
            return Ok(tonic::Response::new(agent_state_response_to_pb(response)));
        }
        let response = self.lock()?.commit_agent_state(CommitAgentStateRequest {
            operation: request.operation,
            run: request.run.map(agent_run_record_from_pb),
            items: request
                .items
                .into_iter()
                .map(agent_item_record_from_pb)
                .collect(),
            expected_revision: request.expected_revision,
            expected_claim_token: request.expected_claim_token,
            idempotency: request.idempotency.map(idempotency_from_pb),
            task_nodes: request
                .task_nodes
                .into_iter()
                .map(task_node_record_from_pb)
                .collect(),
            artifacts: request
                .artifacts
                .into_iter()
                .map(artifact_from_pb)
                .collect(),
            final_projection_json: request.final_projection_json,
            task: request.task.map(task_record_from_pb),
        });
        Ok(tonic::Response::new(agent_state_response_to_pb(response)))
    }

    async fn get_agent_run(
        &self,
        request: tonic::Request<runtime_pb::GetAgentRunRequest>,
    ) -> Result<tonic::Response<runtime_pb::AgentRunResponse>, tonic::Status> {
        let run_id = request.into_inner().run_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_agent_run(&run_id) {
                Ok(run) => AgentStateResponse {
                    run,
                    items: Vec::new(),
                    duplicate: false,
                    error: None,
                },
                Err(error) => AgentStateResponse {
                    run: None,
                    items: Vec::new(),
                    duplicate: false,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.get_agent_run(&run_id)
        };
        Ok(tonic::Response::new(runtime_pb::AgentRunResponse {
            found: response.run.is_some(),
            run: response.run.map(agent_run_record_to_pb),
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn get_agent_run_for_task(
        &self,
        request: tonic::Request<runtime_pb::GetAgentRunForTaskRequest>,
    ) -> Result<tonic::Response<runtime_pb::AgentRunResponse>, tonic::Status> {
        let task_id = request.into_inner().task_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_agent_run_for_task(&task_id) {
                Ok(run) => AgentStateResponse {
                    run,
                    items: Vec::new(),
                    duplicate: false,
                    error: None,
                },
                Err(error) => AgentStateResponse {
                    run: None,
                    items: Vec::new(),
                    duplicate: false,
                    error: Some(error.into()),
                },
            }
        } else {
            AgentStateResponse {
                run: self.lock()?.kernel.get_agent_run_for_task(&task_id),
                items: Vec::new(),
                duplicate: false,
                error: None,
            }
        };
        Ok(tonic::Response::new(runtime_pb::AgentRunResponse {
            found: response.run.is_some(),
            run: response.run.map(agent_run_record_to_pb),
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn list_agent_runs(
        &self,
        request: tonic::Request<runtime_pb::ListAgentRunsRequest>,
    ) -> Result<tonic::Response<runtime_pb::ListAgentRunsResponse>, tonic::Status> {
        let statuses: BTreeSet<String> = request.into_inner().statuses.into_iter().collect();
        let (runs, error) = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.list_agent_runs(&statuses) {
                Ok(runs) => (runs, None),
                Err(error) => (Vec::new(), Some(error.into())),
            }
        } else {
            (self.lock()?.kernel.list_agent_runs(&statuses), None)
        };
        Ok(tonic::Response::new(runtime_pb::ListAgentRunsResponse {
            runs: runs.into_iter().map(agent_run_record_to_pb).collect(),
            error: error.map(typed_error_to_pb),
        }))
    }

    async fn list_agent_items(
        &self,
        request: tonic::Request<runtime_pb::ListAgentItemsRequest>,
    ) -> Result<tonic::Response<runtime_pb::ListAgentItemsResponse>, tonic::Status> {
        let run_id = request.into_inner().run_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.list_agent_items(&run_id) {
                Ok(items) => AgentStateResponse {
                    run: None,
                    items,
                    duplicate: false,
                    error: None,
                },
                Err(error) => AgentStateResponse {
                    run: None,
                    items: Vec::new(),
                    duplicate: false,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.list_agent_items(&run_id)
        };
        Ok(tonic::Response::new(runtime_pb::ListAgentItemsResponse {
            items: response
                .items
                .into_iter()
                .map(agent_item_record_to_pb)
                .collect(),
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn get_agent_final_projection(
        &self,
        request: tonic::Request<runtime_pb::GetAgentFinalProjectionRequest>,
    ) -> Result<tonic::Response<runtime_pb::AgentFinalProjectionResponse>, tonic::Status> {
        let run_id = request.into_inner().run_id;
        let result = if let Some(adapter) = &self.sqlite_adapter {
            adapter.get_agent_final_projection(&run_id)
        } else {
            Ok(self.lock()?.kernel.get_agent_final_projection(&run_id))
        };
        let response = match result {
            Ok(projection_json) => runtime_pb::AgentFinalProjectionResponse {
                found: projection_json.is_some(),
                projection_json,
                error: None,
            },
            Err(error) => runtime_pb::AgentFinalProjectionResponse {
                projection_json: None,
                found: false,
                error: Some(typed_error_to_pb(error.into())),
            },
        };
        Ok(tonic::Response::new(response))
    }

    async fn transition_node(
        &self,
        request: tonic::Request<runtime_pb::TransitionNodeRequest>,
    ) -> Result<tonic::Response<runtime_pb::TransitionNodeResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.transition_node(
                    &request.task_id,
                    &request.node_id,
                    &request.to_status,
                    &request.from_status,
                    &pb_idempotency_key(request.idempotency.clone()),
                    request.node.clone().map(task_node_record_from_pb),
                )
            })? {
                Ok(result) => TransitionNodeResponse {
                    node_id: result.node_id,
                    status: result.status,
                    error: None,
                    node: request.node.clone().map(task_node_record_from_pb),
                },
                Err(error) => TransitionNodeResponse {
                    node_id: String::new(),
                    status: String::new(),
                    error: Some(TypedErrorEnvelope::from(error)),
                    node: None,
                },
            };
            return Ok(tonic::Response::new(runtime_pb::TransitionNodeResponse {
                node_id: response.node_id,
                status: response.status,
                error: response.error.map(typed_error_to_pb),
                node: response.node.map(task_node_record_to_pb),
            }));
        }
        let response = self.lock()?.transition_node(TransitionNodeRequest {
            task_id: request.task_id,
            node_id: request.node_id,
            to_status: request.to_status,
            from_status: request.from_status,
            idempotency: request.idempotency.map(idempotency_from_pb),
            node: request.node.map(task_node_record_from_pb),
        });
        Ok(tonic::Response::new(runtime_pb::TransitionNodeResponse {
            node_id: response.node_id,
            status: response.status,
            error: response.error.map(typed_error_to_pb),
            node: response.node.map(task_node_record_to_pb),
        }))
    }

    async fn get_task_node(
        &self,
        request: tonic::Request<runtime_pb::GetTaskNodeRequest>,
    ) -> Result<tonic::Response<runtime_pb::GetTaskNodeResponse>, tonic::Status> {
        let node_id = request.into_inner().node_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_task_node(&node_id) {
                Ok(node) => GetTaskNodeResponse {
                    found: node.is_some(),
                    node,
                    error: None,
                },
                Err(error) => GetTaskNodeResponse {
                    found: false,
                    node: None,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.get_task_node(&node_id)
        };
        Ok(tonic::Response::new(runtime_pb::GetTaskNodeResponse {
            node: response.node.map(task_node_record_to_pb),
            found: response.found,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn list_task_nodes_for_task(
        &self,
        request: tonic::Request<runtime_pb::ListTaskNodesForTaskRequest>,
    ) -> Result<tonic::Response<runtime_pb::ListTaskNodesForTaskResponse>, tonic::Status> {
        let task_id = request.into_inner().task_id;
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.list_task_nodes_for_task(&task_id) {
                Ok(nodes) => ListTaskNodesForTaskResponse { nodes, error: None },
                Err(error) => ListTaskNodesForTaskResponse {
                    nodes: Vec::new(),
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.list_task_nodes_for_task(&task_id)
        };
        Ok(tonic::Response::new(
            runtime_pb::ListTaskNodesForTaskResponse {
                nodes: response
                    .nodes
                    .into_iter()
                    .map(task_node_record_to_pb)
                    .collect(),
                error: response.error.map(typed_error_to_pb),
            },
        ))
    }

    async fn save_artifact(
        &self,
        request: tonic::Request<runtime_pb::SaveArtifactRequest>,
    ) -> Result<tonic::Response<runtime_pb::ArtifactResponse>, tonic::Status> {
        let request = request.into_inner();
        let artifact = request.artifact.map(artifact_from_pb).unwrap_or_default();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.save_artifact(
                    artifact.clone(),
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok(artifact) => ArtifactResponse {
                    artifact: Some(artifact),
                    found: true,
                    error: None,
                },
                Err(error) => ArtifactResponse {
                    artifact: None,
                    found: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(artifact_response_to_pb(response)));
        }
        let response = self.lock()?.save_artifact(SaveArtifactRequest {
            artifact,
            idempotency: request.idempotency.map(idempotency_from_pb),
        });
        Ok(tonic::Response::new(artifact_response_to_pb(response)))
    }

    async fn get_artifact(
        &self,
        request: tonic::Request<runtime_pb::GetArtifactRequest>,
    ) -> Result<tonic::Response<runtime_pb::ArtifactResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match adapter.get_artifact(&request.artifact_id) {
                Ok(artifact) => ArtifactResponse {
                    found: artifact.is_some(),
                    artifact,
                    error: None,
                },
                Err(error) => ArtifactResponse {
                    artifact: None,
                    found: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(artifact_response_to_pb(response)));
        }
        let response = self.lock()?.get_artifact(GetArtifactRequest {
            artifact_id: request.artifact_id,
        });
        Ok(tonic::Response::new(artifact_response_to_pb(response)))
    }

    async fn list_artifacts_for_task(
        &self,
        request: tonic::Request<runtime_pb::ListArtifactsForTaskRequest>,
    ) -> Result<tonic::Response<runtime_pb::ListArtifactsForTaskResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match adapter.list_artifacts_for_task(&request.task_id) {
                Ok(artifacts) => ListArtifactsForTaskResponse {
                    artifacts,
                    error: None,
                },
                Err(error) => ListArtifactsForTaskResponse {
                    artifacts: Vec::new(),
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(list_artifacts_response_to_pb(
                response,
            )));
        }
        let response = self
            .lock()?
            .list_artifacts_for_task(ListArtifactsForTaskRequest {
                task_id: request.task_id,
            });
        Ok(tonic::Response::new(list_artifacts_response_to_pb(
            response,
        )))
    }

    async fn append_event(
        &self,
        request: tonic::Request<runtime_pb::AppendEventRequest>,
    ) -> Result<tonic::Response<runtime_pb::AppendEventResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.append_event_exact(
                    &request.conversation_id,
                    &request.task_id,
                    &request.event_type,
                    request.payload_json.clone(),
                    0,
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok((cursor, duplicate)) => AppendEventResponse {
                    cursor: Some(cursor),
                    error: None,
                    duplicate,
                },
                Err(error) => AppendEventResponse {
                    cursor: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                    duplicate: false,
                },
            };
            return Ok(tonic::Response::new(runtime_pb::AppendEventResponse {
                cursor: response.cursor.map(cursor_to_pb),
                error: response.error.map(typed_error_to_pb),
                duplicate: response.duplicate,
            }));
        }
        let response = self.lock()?.append_event(AppendEventRequest {
            conversation_id: request.conversation_id,
            task_id: request.task_id,
            event_type: request.event_type,
            payload_json: request.payload_json,
            idempotency: request.idempotency.map(idempotency_from_pb),
            created_at_ms: 0,
        });
        Ok(tonic::Response::new(runtime_pb::AppendEventResponse {
            cursor: response.cursor.map(cursor_to_pb),
            error: response.error.map(typed_error_to_pb),
            duplicate: response.duplicate,
        }))
    }

    async fn replay_events(
        &self,
        request: tonic::Request<runtime_pb::ReplayEventsRequest>,
    ) -> Result<tonic::Response<runtime_pb::ReplayEventsResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let requested_limit = request.page_limit as usize;
            let response = match adapter.replay_events(
                &request.conversation_id,
                &request.task_id,
                request.after_sequence,
                requested_limit,
                request.byte_limit as usize,
            ) {
                Ok(cursors) => ReplayEventsResponse {
                    truncated: requested_limit > 0 && cursors.len() == requested_limit,
                    cursors,
                    error: None,
                },
                Err(error) => ReplayEventsResponse {
                    cursors: Vec::new(),
                    truncated: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(runtime_pb::ReplayEventsResponse {
                cursors: response.cursors.into_iter().map(cursor_to_pb).collect(),
                truncated: response.truncated,
                error: response.error.map(typed_error_to_pb),
            }));
        }
        let response = self.lock()?.replay_events(ReplayEventsRequest {
            conversation_id: request.conversation_id,
            task_id: request.task_id,
            after_sequence: request.after_sequence,
            page_limit: request.page_limit,
            byte_limit: request.byte_limit,
        });
        Ok(tonic::Response::new(runtime_pb::ReplayEventsResponse {
            cursors: response.cursors.into_iter().map(cursor_to_pb).collect(),
            truncated: response.truncated,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn acquire_lease(
        &self,
        request: tonic::Request<runtime_pb::AcquireLeaseRequest>,
    ) -> Result<tonic::Response<runtime_pb::LeaseResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.acquire_lease(
                    &request.task_id,
                    &request.owner_id,
                    request.now_ms,
                    request.ttl_ms,
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok(lease) => LeaseResponse {
                    lease: Some(lease),
                    error: None,
                },
                Err(error) => LeaseResponse {
                    lease: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(lease_response_to_pb(response)));
        }
        let response = self.lock()?.acquire_lease(AcquireLeaseRequest {
            task_id: request.task_id,
            owner_id: request.owner_id,
            now_ms: request.now_ms,
            ttl_ms: request.ttl_ms,
            idempotency: request.idempotency.map(idempotency_from_pb),
        });
        Ok(tonic::Response::new(lease_response_to_pb(response)))
    }

    async fn renew_lease(
        &self,
        request: tonic::Request<runtime_pb::RenewLeaseRequest>,
    ) -> Result<tonic::Response<runtime_pb::LeaseResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.renew_lease(
                    &request.task_id,
                    &request.renew_token,
                    request.now_ms,
                    request.ttl_ms,
                )
            })? {
                Ok(lease) => LeaseResponse {
                    lease: Some(lease),
                    error: None,
                },
                Err(error) => LeaseResponse {
                    lease: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(lease_response_to_pb(response)));
        }
        let response = self.lock()?.renew_lease(RenewLeaseRequest {
            task_id: request.task_id,
            renew_token: request.renew_token,
            now_ms: request.now_ms,
            ttl_ms: request.ttl_ms,
        });
        Ok(tonic::Response::new(lease_response_to_pb(response)))
    }

    async fn release_lease(
        &self,
        request: tonic::Request<runtime_pb::ReleaseLeaseRequest>,
    ) -> Result<tonic::Response<runtime_pb::ReleaseLeaseResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.release_lease(&request.task_id, &request.renew_token)
            })? {
                Ok(released) => ReleaseLeaseResponse {
                    released,
                    error: None,
                },
                Err(error) => ReleaseLeaseResponse {
                    released: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(runtime_pb::ReleaseLeaseResponse {
                released: response.released,
                error: response.error.map(typed_error_to_pb),
            }));
        }
        let response = self.lock()?.release_lease(ReleaseLeaseRequest {
            task_id: request.task_id,
            renew_token: request.renew_token,
        });
        Ok(tonic::Response::new(runtime_pb::ReleaseLeaseResponse {
            released: response.released,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn write_cancellation_token(
        &self,
        request: tonic::Request<runtime_pb::WriteCancellationTokenRequest>,
    ) -> Result<tonic::Response<runtime_pb::WriteCancellationTokenResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.write_cancellation_token(
                    &request.task_id,
                    request.requested_at_ms,
                    &request.reason,
                    &request.terminal_policy,
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok(written) => WriteCancellationTokenResponse {
                    written,
                    error: None,
                },
                Err(error) => WriteCancellationTokenResponse {
                    written: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(
                runtime_pb::WriteCancellationTokenResponse {
                    written: response.written,
                    error: response.error.map(typed_error_to_pb),
                },
            ));
        }
        let response = self
            .lock()?
            .write_cancellation_token(WriteCancellationTokenRequest {
                task_id: request.task_id,
                requested_at_ms: request.requested_at_ms,
                reason: request.reason,
                terminal_policy: request.terminal_policy,
                idempotency: request.idempotency.map(idempotency_from_pb),
            });
        Ok(tonic::Response::new(
            runtime_pb::WriteCancellationTokenResponse {
                written: response.written,
                error: response.error.map(typed_error_to_pb),
            },
        ))
    }

    async fn pin_bundle_revision(
        &self,
        request: tonic::Request<runtime_pb::PinBundleRevisionRequest>,
    ) -> Result<tonic::Response<runtime_pb::BundleRevisionResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.pin_bundle_revision(
                    &request.task_id,
                    &request.bundle_kind,
                    &request.revision,
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok(result) => BundleRevisionResponse {
                    result: Some(result),
                    error: None,
                },
                Err(error) => BundleRevisionResponse {
                    result: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(bundle_revision_response_to_pb(
                response,
            )));
        }
        let response = self.lock()?.pin_bundle_revision(PinBundleRevisionRequest {
            task_id: request.task_id,
            bundle_kind: request.bundle_kind,
            revision: request.revision,
            idempotency: request.idempotency.map(idempotency_from_pb),
        });
        Ok(tonic::Response::new(bundle_revision_response_to_pb(
            response,
        )))
    }

    async fn release_bundle_revision(
        &self,
        request: tonic::Request<runtime_pb::ReleaseBundleRevisionRequest>,
    ) -> Result<tonic::Response<runtime_pb::BundleRevisionResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match self.run_sqlite_write(|| {
                adapter.release_bundle_revision(
                    &request.task_id,
                    &request.bundle_kind,
                    &request.revision,
                    request.released_at_ms,
                    &pb_idempotency_key(request.idempotency.clone()),
                )
            })? {
                Ok(result) => BundleRevisionResponse {
                    result: Some(result),
                    error: None,
                },
                Err(error) => BundleRevisionResponse {
                    result: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(bundle_revision_response_to_pb(
                response,
            )));
        }
        let response = self
            .lock()?
            .release_bundle_revision(ReleaseBundleRevisionRequest {
                task_id: request.task_id,
                bundle_kind: request.bundle_kind,
                revision: request.revision,
                released_at_ms: request.released_at_ms,
                idempotency: request.idempotency.map(idempotency_from_pb),
            });
        Ok(tonic::Response::new(bundle_revision_response_to_pb(
            response,
        )))
    }

    async fn admit_submission(
        &self,
        request: tonic::Request<runtime_pb::AdmitSubmissionRequest>,
    ) -> Result<tonic::Response<runtime_pb::AdmitSubmissionResponse>, tonic::Status> {
        let response = match admit_submission_request_from_pb(request.into_inner()) {
            Ok(request) => {
                if let Some(adapter) = &self.sqlite_adapter {
                    match self.run_sqlite_write(|| adapter.admit_submission(request))? {
                        Ok(response) => response,
                        Err(error) => {
                            let mut response = admission_disposition(
                                SubmissionAdmissionDisposition::ConversationNotAvailable,
                            );
                            response.error = Some(error.into());
                            response
                        }
                    }
                } else {
                    self.lock()?.admit_submission(request)
                }
            }
            Err(error) => AdmitSubmissionResponse {
                disposition: SubmissionAdmissionDisposition::ConversationNotAvailable,
                admission: None,
                claim: None,
                error: Some(error.into()),
            },
        };
        Ok(tonic::Response::new(admit_submission_response_to_pb(
            response,
        )))
    }

    async fn claim_pending_submission(
        &self,
        request: tonic::Request<runtime_pb::ClaimPendingSubmissionRequest>,
    ) -> Result<tonic::Response<runtime_pb::ClaimPendingSubmissionResponse>, tonic::Status> {
        let request = claim_pending_request_from_pb(request.into_inner());
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match self.run_sqlite_write(|| adapter.claim_pending_submission(request))? {
                Ok(response) => response,
                Err(error) => ClaimPendingSubmissionResponse {
                    found: false,
                    admission: None,
                    claim: None,
                    authority_state: "uninitialized".to_owned(),
                    finalization_receipt_sha256: None,
                    pending_count: 0,
                    earliest_claim_expires_at_ms: None,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.claim_pending_submission(request)
        };
        Ok(tonic::Response::new(claim_pending_response_to_pb(response)))
    }

    async fn renew_submission_claim(
        &self,
        request: tonic::Request<runtime_pb::RenewSubmissionClaimRequest>,
    ) -> Result<tonic::Response<runtime_pb::SubmissionClaimResponse>, tonic::Status> {
        let request = renew_claim_request_from_pb(request.into_inner());
        let result = if let Some(adapter) = &self.sqlite_adapter {
            self.run_sqlite_write(|| adapter.renew_submission_claim(request))?
                .map_err(Into::into)
        } else {
            self.lock()?.renew_submission_claim(request)
        };
        Ok(tonic::Response::new(submission_claim_response_to_pb(
            result,
        )))
    }

    async fn acknowledge_submission_projection(
        &self,
        request: tonic::Request<runtime_pb::AcknowledgeSubmissionProjectionRequest>,
    ) -> Result<tonic::Response<runtime_pb::SubmissionAdmissionResponse>, tonic::Status> {
        let response =
            match projection_ack_request_from_pb(request.into_inner()) {
                Ok(request) => {
                    if let Some(adapter) = &self.sqlite_adapter {
                        submission_admission_result(self.run_sqlite_write(|| {
                            adapter.acknowledge_submission_projection(request)
                        })?)
                    } else {
                        self.lock()?.acknowledge_submission_projection(request)
                    }
                }
                Err(error) => SubmissionAdmissionResponse {
                    admission: None,
                    duplicate: false,
                    error: Some(error.into()),
                },
            };
        Ok(tonic::Response::new(submission_admission_response_to_pb(
            response,
        )))
    }

    async fn prepare_submission_handoff(
        &self,
        request: tonic::Request<runtime_pb::PrepareSubmissionHandoffRequest>,
    ) -> Result<tonic::Response<runtime_pb::SubmissionAdmissionResponse>, tonic::Status> {
        let response = match prepare_handoff_request_from_pb(request.into_inner()) {
            Ok(request) => {
                if let Some(adapter) = &self.sqlite_adapter {
                    submission_admission_result(
                        self.run_sqlite_write(|| adapter.prepare_submission_handoff(request))?,
                    )
                } else {
                    self.lock()?.prepare_submission_handoff(request)
                }
            }
            Err(error) => SubmissionAdmissionResponse {
                admission: None,
                duplicate: false,
                error: Some(error.into()),
            },
        };
        Ok(tonic::Response::new(submission_admission_response_to_pb(
            response,
        )))
    }

    async fn get_submission_preparation(
        &self,
        request: tonic::Request<runtime_pb::GetSubmissionPreparationRequest>,
    ) -> Result<tonic::Response<runtime_pb::GetSubmissionPreparationResponse>, tonic::Status> {
        let request = get_preparation_request_from_pb(request.into_inner());
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match adapter.get_submission_preparation(&request) {
                Ok(admission) => GetSubmissionPreparationResponse {
                    found: admission.is_some(),
                    admission,
                    error: None,
                },
                Err(error) => GetSubmissionPreparationResponse {
                    found: false,
                    admission: None,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.get_submission_preparation(&request)
        };
        Ok(tonic::Response::new(get_preparation_response_to_pb(
            response,
        )))
    }

    async fn acknowledge_submission_handoff(
        &self,
        request: tonic::Request<runtime_pb::AcknowledgeSubmissionHandoffRequest>,
    ) -> Result<tonic::Response<runtime_pb::SubmissionAdmissionResponse>, tonic::Status> {
        let response = match handoff_ack_request_from_pb(request.into_inner()) {
            Ok(request) => {
                if let Some(adapter) = &self.sqlite_adapter {
                    submission_admission_result(
                        self.run_sqlite_write(|| adapter.acknowledge_submission_handoff(request))?,
                    )
                } else {
                    self.lock()?.acknowledge_submission_handoff(request)
                }
            }
            Err(error) => SubmissionAdmissionResponse {
                admission: None,
                duplicate: false,
                error: Some(error.into()),
            },
        };
        Ok(tonic::Response::new(submission_admission_response_to_pb(
            response,
        )))
    }

    async fn close_conversation_admission(
        &self,
        request: tonic::Request<runtime_pb::CloseConversationAdmissionRequest>,
    ) -> Result<tonic::Response<runtime_pb::CloseConversationAdmissionResponse>, tonic::Status>
    {
        let request = close_request_from_pb(request.into_inner());
        let response = if let Some(adapter) = &self.sqlite_adapter {
            match self.run_sqlite_write(|| adapter.close_conversation_admission(request))? {
                Ok(response) => response,
                Err(error) => CloseConversationAdmissionResponse {
                    disposition: ConversationAdmissionCloseDisposition::Conflict,
                    revision: 0,
                    error: Some(error.into()),
                },
            }
        } else {
            self.lock()?.close_conversation_admission(request)
        };
        Ok(tonic::Response::new(close_response_to_pb(response)))
    }

    async fn reserve_message_identity(
        &self,
        request: tonic::Request<runtime_pb::ReserveMessageIdentityRequest>,
    ) -> Result<tonic::Response<runtime_pb::ReserveMessageIdentityResponse>, tonic::Status> {
        let response = match reserve_request_from_pb(request.into_inner()) {
            Ok(request) => {
                if let Some(adapter) = &self.sqlite_adapter {
                    match self.run_sqlite_write(|| adapter.reserve_message_identity(request))? {
                        Ok(response) => response,
                        Err(error) => ReserveMessageIdentityResponse {
                            disposition: MessageIdentityDisposition::Conflict,
                            identity: None,
                            error: Some(error.into()),
                        },
                    }
                } else {
                    self.lock()?.reserve_message_identity(request)
                }
            }
            Err(error) => ReserveMessageIdentityResponse {
                disposition: MessageIdentityDisposition::Conflict,
                identity: None,
                error: Some(error.into()),
            },
        };
        Ok(tonic::Response::new(reserve_response_to_pb(response)))
    }
}

pub fn runtime_sidecar_service_from_config(
    config: &RuntimeSidecarServeConfig,
) -> Result<RuntimeSidecarGrpcService, RuntimeSidecarError> {
    config.build_service()
}

pub async fn serve_runtime_sidecar(
    config: RuntimeSidecarServeConfig,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    let service = runtime_sidecar_service_from_config(&config)?;
    if let Some(path) = config.unix_socket_path.as_ref() {
        serve_runtime_sidecar_unix_socket(path, service).await?;
    } else {
        serve_runtime_sidecar_tcp(config.listen_addr, service, config.tls_config.as_ref()).await?;
    }
    Ok(())
}

pub async fn serve_runtime_sidecar_service(
    listen_addr: SocketAddr,
    service: RuntimeSidecarGrpcService,
) -> Result<(), tonic::transport::Error> {
    tonic::transport::Server::builder()
        .add_service(runtime_sidecar_grpc_server(service))
        .serve(listen_addr)
        .await
}

pub async fn serve_runtime_sidecar_tcp(
    listen_addr: SocketAddr,
    service: RuntimeSidecarGrpcService,
    tls_config: Option<&RuntimeSidecarTlsConfig>,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    let mut builder = tonic::transport::Server::builder();
    if let Some(tls_config) = tls_config {
        builder = builder.tls_config(tls_config.to_server_tls_config()?)?;
    }
    builder
        .add_service(runtime_sidecar_grpc_server(service))
        .serve(listen_addr)
        .await?;
    Ok(())
}

#[cfg(unix)]
pub async fn serve_runtime_sidecar_unix_socket(
    socket_path: &Path,
    service: RuntimeSidecarGrpcService,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    match fs::symlink_metadata(socket_path) {
        Ok(metadata) if metadata.file_type().is_socket() => fs::remove_file(socket_path)?,
        Ok(_) => return Err("runtime sidecar socket path exists and is not a socket".into()),
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let listener = tokio::net::UnixListener::bind(socket_path)?;
    let incoming = UnixListenerStream::new(listener);
    tonic::transport::Server::builder()
        .add_service(runtime_sidecar_grpc_server(service))
        .serve_with_incoming(incoming)
        .await?;
    Ok(())
}

#[cfg(unix)]
pub async fn semantic_probe_runtime_sidecar_unix_socket(
    socket_path: impl AsRef<Path>,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    let socket_path = socket_path.as_ref().to_path_buf();
    let channel = tonic::transport::Endpoint::try_from("http://[::]:50051")?
        .connect_with_connector(tower::service_fn(move |_: tonic::transport::Uri| {
            let socket_path = socket_path.clone();
            async move {
                tokio::net::UnixStream::connect(socket_path)
                    .await
                    .map(hyper_util::rt::TokioIo::new)
            }
        }))
        .await?;
    let mut client = runtime_pb::runtime_sidecar_client::RuntimeSidecarClient::new(channel);
    let version = client
        .version(runtime_pb::VersionRequest {})
        .await?
        .into_inner()
        .version
        .ok_or("runtime sidecar Version response omitted version")?;
    if version.component != COMPONENT_ID
        || version.protocol_version != PROTOCOL_VERSION
        || version.schema_hash != SCHEMA_HASH
        || version.error_code_table_hash != RUNTIME_ERROR_CODE_TABLE_HASH
        || version.supported_features != supported_features()
    {
        return Err("runtime sidecar Version response is incompatible with this probe".into());
    }
    let compatibility = client
        .check_compatibility(runtime_pb::CompatibilityCheckRequest {
            client_version: env!("CARGO_PKG_VERSION").to_owned(),
            expected_component: COMPONENT_ID.to_owned(),
            expected_protocol_version: PROTOCOL_VERSION.to_owned(),
            expected_schema_hash: SCHEMA_HASH.to_owned(),
            expected_error_code_table_hash: RUNTIME_ERROR_CODE_TABLE_HASH.to_owned(),
            required_features: supported_features(),
        })
        .await?
        .into_inner();
    if !compatibility.compatible || compatibility.error.is_some() {
        return Err("runtime sidecar CheckCompatibility failed".into());
    }
    let readiness = client
        .readiness(runtime_pb::ReadinessRequest {})
        .await?
        .into_inner();
    if readiness.state != common_pb::ReadinessState::Ready as i32
        || !readiness.compatibility_handshake_passed
        || readiness.error.is_some()
    {
        return Err("runtime sidecar Readiness is not ready after compatibility handshake".into());
    }
    Ok(())
}

#[cfg(not(unix))]
pub async fn serve_runtime_sidecar_unix_socket(
    _socket_path: &Path,
    _service: RuntimeSidecarGrpcService,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    Err("runtime sidecar unix sockets are unavailable on this platform".into())
}

pub async fn serve_runtime_sidecar_with_incoming<F>(
    service: RuntimeSidecarGrpcService,
    incoming: TcpListenerStream,
    shutdown: F,
) -> Result<(), tonic::transport::Error>
where
    F: Future<Output = ()>,
{
    tonic::transport::Server::builder()
        .add_service(runtime_sidecar_grpc_server(service))
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await
}

fn runtime_sidecar_grpc_server(
    service: RuntimeSidecarGrpcService,
) -> runtime_pb::runtime_sidecar_server::RuntimeSidecarServer<RuntimeSidecarGrpcService> {
    runtime_pb::runtime_sidecar_server::RuntimeSidecarServer::new(service)
        .max_decoding_message_size(GRPC_MAX_MESSAGE_BYTES)
        .max_encoding_message_size(GRPC_MAX_MESSAGE_BYTES)
}

#[must_use]
pub fn supported_features() -> Vec<String> {
    vec![
        FEATURE_RUNTIME_STORE.to_owned(),
        FEATURE_EVENT_LOG.to_owned(),
        FEATURE_TASK_DISPATCHER.to_owned(),
        FEATURE_ARTIFACT_METADATA.to_owned(),
        FEATURE_TASK_READ.to_owned(),
        FEATURE_AGENT_STATE.to_owned(),
        FEATURE_SUBMISSION_ADMISSION.to_owned(),
    ]
}

pub(crate) fn idempotency_key(idempotency: Option<Idempotency>) -> String {
    idempotency.map_or_else(String::new, |value| value.key)
}

fn require_idempotency_key(
    idempotency_key: impl Into<String>,
) -> Result<String, RuntimeSidecarError> {
    let idempotency_key = idempotency_key.into().trim().to_owned();
    if idempotency_key.is_empty() {
        return Err(write_failed(
            "idempotency key is required for sidecar writes",
        ));
    }
    Ok(idempotency_key)
}

pub(crate) fn validate_task_node_record(node: &TaskNodeRecord) -> Result<(), RuntimeSidecarError> {
    if node.node_id.trim().is_empty()
        || node.task_id.trim().is_empty()
        || node.capability_id.trim().is_empty()
        || node.status.trim().is_empty()
    {
        return Err(write_failed("TaskNodeRecord required fields are missing"));
    }
    if ![
        "pending",
        "ready",
        "running",
        "waiting_for_dependency",
        "waiting_for_input",
        "ready_to_resume",
        "resuming",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "blocked_by_cancellation",
        "orphaned",
    ]
    .contains(&node.status.as_str())
    {
        return Err(write_failed(
            "TaskNodeRecord enum value is not in the closed contract",
        ));
    }
    Ok(())
}

pub(crate) fn validate_task_record(task: &TaskRecord) -> Result<(), RuntimeSidecarError> {
    if task.task_id.trim().is_empty()
        || task.conversation_id.trim().is_empty()
        || task.root_message_id.trim().is_empty()
    {
        return Err(write_failed(
            "TaskRecord requires task_id, conversation_id, and root_message_id",
        ));
    }
    if !matches!(
        task.status.as_str(),
        "accepted" | "planning" | "running" | "cancelling" | "cancelled" | "completed" | "failed"
    ) {
        return Err(write_failed(
            "TaskRecord status is not in the closed TaskStatus set",
        ));
    }
    if !matches!(task.routing_mode.as_str(), "auto" | "force_capability") {
        return Err(write_failed(
            "TaskRecord routing_mode is not in the closed RoutingMode set",
        ));
    }
    if let Some(assignment) = &task.assignment {
        validate_task_assignment(assignment)?;
    }
    Ok(())
}

fn validate_task_assignment(assignment: &TaskRouteAssignment) -> Result<(), RuntimeSidecarError> {
    let route_is_valid = matches!(assignment.route_mode.as_str(), "off" | "shadow" | "enforce");
    let real_is_valid = matches!(
        assignment.real_path.as_str(),
        "legacy" | "user_scoped" | "unavailable"
    );
    let shadow_is_valid = matches!(assignment.shadow_path.as_str(), "none" | "user_scoped");
    if !route_is_valid
        || !real_is_valid
        || !shadow_is_valid
        || assignment.config_version.trim().is_empty()
        || assignment.reason_code.trim().is_empty()
    {
        return Err(write_failed(
            "TaskRouteAssignment is outside its closed contract",
        ));
    }
    let paths_are_consistent = match assignment.route_mode.as_str() {
        "off" => {
            matches!(assignment.real_path.as_str(), "legacy" | "unavailable")
                && assignment.shadow_path == "none"
        }
        "shadow" => assignment.real_path == "legacy" && assignment.shadow_path == "user_scoped",
        "enforce" => assignment.shadow_path == "none",
        _ => false,
    };
    if !paths_are_consistent {
        return Err(write_failed(
            "TaskRouteAssignment route mode and paths are inconsistent",
        ));
    }
    Ok(())
}

fn validate_submit_task_identity(
    task_id: &str,
    conversation_id: &str,
    task: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    if task_id != task.task_id || conversation_id != task.conversation_id {
        return Err(write_failed(
            "SubmitTask top-level identity does not match TaskRecord",
        ));
    }
    Ok(())
}

pub(crate) fn validate_task_update(
    existing: &TaskRecord,
    replacement: &TaskRecord,
) -> Result<(), RuntimeSidecarError> {
    if existing.task_id != replacement.task_id
        || existing.conversation_id != replacement.conversation_id
        || existing.root_message_id != replacement.root_message_id
        || existing.routing_mode != replacement.routing_mode
        || existing.requested_capability_id != replacement.requested_capability_id
        || existing.created_at != replacement.created_at
    {
        return Err(write_failed(
            "immutable TaskRecord identity fields cannot be changed",
        ));
    }
    if existing.assignment.is_none() && replacement.assignment.is_some() {
        return Err(migration_blocked(
            "legacy TaskRecord assignment requires an explicit audited migration",
        ));
    }
    if existing.assignment.is_some() && existing.assignment != replacement.assignment {
        return Err(idempotency_conflict(
            "TaskRouteAssignment is write-once and cannot be changed or removed",
        ));
    }
    if !task_status_transition_allowed(&existing.status, &replacement.status) {
        return Err(write_failed("TaskRecord status transition is not allowed"));
    }
    Ok(())
}

pub(crate) fn initial_no_server_task_transition_shape(
    existing: &TaskRecord,
    replacement: &TaskRecord,
) -> bool {
    let (Some(existing_assignment), Some(replacement_assignment)) =
        (&existing.assignment, &replacement.assignment)
    else {
        return false;
    };
    existing.task_id == replacement.task_id
        && existing.conversation_id == replacement.conversation_id
        && existing.root_message_id == replacement.root_message_id
        && existing.routing_mode == replacement.routing_mode
        && existing.requested_capability_id == replacement.requested_capability_id
        && existing.summary == replacement.summary
        && existing.created_at == replacement.created_at
        && existing.cancel_requested_at == replacement.cancel_requested_at
        && existing.status == "accepted"
        && replacement.status == "failed"
        && existing_assignment.route_mode == "enforce"
        && existing_assignment.real_path == "user_scoped"
        && existing_assignment.shadow_path == "none"
        && replacement_assignment.route_mode == existing_assignment.route_mode
        && replacement_assignment.real_path == "unavailable"
        && replacement_assignment.shadow_path == existing_assignment.shadow_path
        && replacement_assignment.config_version == existing_assignment.config_version
        && replacement_assignment.reason_code == "no_user_scoped_server"
        && replacement_assignment.cohort_id == existing_assignment.cohort_id
        && replacement_assignment.assignment_key_hash == existing_assignment.assignment_key_hash
        && replacement_assignment.assigned_at == existing_assignment.assigned_at
}

pub(crate) fn validate_task_node_update(
    existing: &TaskNodeRecord,
    replacement: &TaskNodeRecord,
) -> Result<(), RuntimeSidecarError> {
    if existing.node_id != replacement.node_id
        || existing.task_id != replacement.task_id
        || existing.capability_id != replacement.capability_id
    {
        return Err(write_failed(
            "immutable TaskNodeRecord identity fields cannot be changed",
        ));
    }
    if node_status_is_terminal(&existing.status) && existing.status != replacement.status {
        return Err(write_failed(
            "terminal TaskNodeRecord status cannot be changed",
        ));
    }
    Ok(())
}

fn node_status_is_terminal(status: &str) -> bool {
    matches!(
        status,
        "completed" | "failed" | "cancelled" | "blocked_by_cancellation" | "orphaned"
    )
}

fn task_status_transition_allowed(from: &str, to: &str) -> bool {
    from == to
        || matches!(
            (from, to),
            (
                "accepted",
                "planning" | "running" | "cancelling" | "completed" | "failed"
            ) | (
                "planning",
                "running" | "cancelling" | "completed" | "failed"
            ) | ("running", "cancelling" | "completed" | "failed")
                | ("cancelling", "cancelled")
        )
}

fn admission_disposition(disposition: SubmissionAdmissionDisposition) -> AdmitSubmissionResponse {
    AdmitSubmissionResponse {
        disposition,
        admission: None,
        claim: None,
        error: None,
    }
}

fn validate_claim_timing(
    owner: &str,
    now_ms: i64,
    claim_ttl_ms: i64,
) -> Result<(), RuntimeSidecarError> {
    if owner.trim().is_empty() || now_ms < 0 || claim_ttl_ms <= 0 {
        return Err(write_failed("submission claim timing or owner is invalid"));
    }
    now_ms
        .checked_add(claim_ttl_ms)
        .ok_or_else(|| write_failed("submission claim expiry overflows"))?;
    Ok(())
}

pub(crate) fn new_submission_claim(
    message_id: &str,
    owner: &str,
    now_ms: i64,
    claim_ttl_ms: i64,
    revision: u64,
) -> Result<SubmissionClaim, RuntimeSidecarError> {
    validate_claim_timing(owner, now_ms, claim_ttl_ms)?;
    let expires_at_ms = now_ms + claim_ttl_ms;
    let token = format!(
        "{:x}",
        Sha256::digest(format!(
            "maf.submission.claim.v1\0{message_id}\0{owner}\0{revision}\0{now_ms}"
        ))
    );
    Ok(SubmissionClaim {
        owner: owner.to_owned(),
        token,
        expires_at_ms,
    })
}

fn validate_submission_claim(
    entry: &SubmissionAdmissionEntry,
    owner: &str,
    token: &str,
    now_ms: i64,
) -> Result<(), RuntimeSidecarError> {
    let claim = entry
        .claim
        .as_ref()
        .ok_or_else(|| idempotency_conflict("submission admission has no active claim"))?;
    if claim.owner != owner || claim.token != token || claim.expires_at_ms <= now_ms {
        return Err(idempotency_conflict(
            "submission claim owner, token, or expiry mismatch",
        ));
    }
    Ok(())
}

pub(crate) fn validate_admit_submission_request(
    request: &AdmitSubmissionRequest,
) -> Result<(), RuntimeSidecarError> {
    if request.message_id.trim().is_empty()
        || request.task_id.trim().is_empty()
        || request.conversation_id.trim().is_empty()
        || request.username.trim().is_empty()
        || request.workflow_owner.trim().is_empty()
        || request.idempotency_key.trim().is_empty()
        || !is_sha256(&request.request_fingerprint)
        || request.message_created_at_ms < 0
    {
        return Err(write_failed("submission admission identity is invalid"));
    }
    validate_claim_timing(
        &request.workflow_owner,
        request.now_ms,
        request.claim_ttl_ms,
    )?;
    validate_task_record(&request.task)?;
    if request.task.task_id != request.task_id
        || request.task.conversation_id != request.conversation_id
        || request.task.root_message_id != request.message_id
        || request.task.status != "accepted"
    {
        return Err(write_failed(
            "submission Task identity or initial status is invalid",
        ));
    }
    validate_canonical_json_object(
        &request.conversation_projection_json,
        SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES,
        &[
            "schema",
            "conversation_id",
            "username",
            "status",
            "current_task_id",
            "created_at",
            "updated_at",
            "create_if_missing",
        ],
        "conversation projection",
    )?;
    validate_canonical_json_object(
        &request.message_projection_json,
        SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES,
        &[
            "schema",
            "message_id",
            "conversation_id",
            "role",
            "content",
            "task_id",
            "stream_status",
            "message_created_at",
            "message_type",
            "metadata",
            "updated_at",
        ],
        "message projection",
    )?;
    validate_canonical_json_object(
        &request.continuation_json,
        SUBMISSION_CONTINUATION_MAX_BYTES,
        &[
            "schema",
            "request_fingerprint",
            "conversation_id",
            "message_id",
            "task_id",
            "owner_scope",
            "message_content_sha256",
            "routing_mode",
            "requested_capability_id",
            "model_options",
            "bundle_revisions",
            "execution_metadata",
            "upload_refs",
            "sheet_selections",
            "mcp_binding",
            "mcp_assignment",
            "available_mcp_servers",
            "pending_context",
            "initial_no_server_eligible",
        ],
        "submission continuation",
    )?;
    let expected_projection = domain_digest(
        b"maf.submission.projection.v1\0",
        &[
            &request.conversation_projection_json,
            b"\0",
            &request.message_projection_json,
        ],
    );
    if request.projection_sha256 != expected_projection
        || request.continuation_sha256
            != domain_digest(
                b"maf.submission.continuation.v1\0",
                &[&request.continuation_json],
            )
    {
        return Err(write_failed(
            "submission projection or continuation digest mismatch",
        ));
    }
    validate_projection_identity(request)?;
    Ok(())
}

fn validate_projection_identity(
    request: &AdmitSubmissionRequest,
) -> Result<(), RuntimeSidecarError> {
    let conversation: serde_json::Value =
        serde_json::from_slice(&request.conversation_projection_json)
            .map_err(|_| write_failed("conversation projection JSON is invalid"))?;
    let message: serde_json::Value = serde_json::from_slice(&request.message_projection_json)
        .map_err(|_| write_failed("message projection JSON is invalid"))?;
    let continuation: serde_json::Value = serde_json::from_slice(&request.continuation_json)
        .map_err(|_| write_failed("submission continuation JSON is invalid"))?;
    let conversation_object = exact_object(
        &conversation,
        &[
            "schema",
            "conversation_id",
            "username",
            "status",
            "current_task_id",
            "created_at",
            "updated_at",
            "create_if_missing",
        ],
        "conversation projection",
    )?;
    let message_object = exact_object(
        &message,
        &[
            "schema",
            "message_id",
            "conversation_id",
            "role",
            "content",
            "task_id",
            "stream_status",
            "message_created_at",
            "message_type",
            "metadata",
            "updated_at",
        ],
        "message projection",
    )?;
    let continuation_object = exact_object(
        &continuation,
        &[
            "schema",
            "request_fingerprint",
            "conversation_id",
            "message_id",
            "task_id",
            "owner_scope",
            "message_content_sha256",
            "routing_mode",
            "requested_capability_id",
            "model_options",
            "bundle_revisions",
            "execution_metadata",
            "upload_refs",
            "sheet_selections",
            "mcp_binding",
            "mcp_assignment",
            "available_mcp_servers",
            "pending_context",
            "initial_no_server_eligible",
        ],
        "submission continuation",
    )?;
    let content = message_object["content"]
        .as_str()
        .ok_or_else(|| write_failed("message projection content must be a string"))?;
    let content_sha256 = format!("{:x}", Sha256::digest(content.as_bytes()));
    if conversation["schema"].as_str() != Some("maf.submission.conversation_projection.v1")
        || conversation["status"].as_str() != Some("active")
        || conversation["create_if_missing"].as_bool().is_none()
        || conversation_object["created_at"].as_str().is_none()
        || conversation_object["updated_at"].as_str().is_none()
        || conversation["conversation_id"].as_str() != Some(request.conversation_id.as_str())
        || conversation["username"].as_str() != Some(request.username.as_str())
        || conversation["current_task_id"].as_str() != Some(request.task_id.as_str())
        || message["schema"].as_str() != Some("maf.submission.message_projection.v1")
        || message["message_id"].as_str() != Some(request.message_id.as_str())
        || message["conversation_id"].as_str() != Some(request.conversation_id.as_str())
        || message["task_id"].as_str() != Some(request.task_id.as_str())
        || message["role"].as_str() != Some("user")
        || message["stream_status"].as_str() != Some("complete")
        || message_object["message_created_at"].as_str().is_none()
        || non_empty_string(message_object, "message_type", "message projection").is_err()
        || !message["metadata"].is_object()
        || message_object["updated_at"].as_str().is_none()
        || continuation["schema"].as_str() != Some("maf.submission.continuation.v1")
        || continuation["request_fingerprint"].as_str()
            != Some(request.request_fingerprint.as_str())
        || continuation["conversation_id"].as_str() != Some(request.conversation_id.as_str())
        || continuation["message_id"].as_str() != Some(request.message_id.as_str())
        || continuation["task_id"].as_str() != Some(request.task_id.as_str())
        || continuation["owner_scope"].as_str() != Some(request.username.as_str())
        || continuation["message_content_sha256"].as_str() != Some(content_sha256.as_str())
        || continuation["routing_mode"].as_str() != Some(request.task.routing_mode.as_str())
        || !is_nullable_string(&continuation["requested_capability_id"])
        || continuation["requested_capability_id"].as_str()
            != request.task.requested_capability_id.as_deref()
        || continuation["initial_no_server_eligible"]
            .as_bool()
            .is_none()
    {
        return Err(write_failed("submission envelope identity mismatch"));
    }
    validate_model_options(&continuation["model_options"])?;
    validate_bundle_revisions(&continuation["bundle_revisions"])?;
    validate_execution_metadata(&continuation["execution_metadata"])?;
    validate_safe_references(continuation_object, &request.conversation_id)?;
    reject_forbidden_keys(&message["metadata"])?;
    Ok(())
}

pub(crate) fn validate_prepared_execution(
    bytes: &[u8],
    digest: &str,
) -> Result<(), RuntimeSidecarError> {
    validate_canonical_json_object(
        bytes,
        SUBMISSION_PREPARED_EXECUTION_MAX_BYTES,
        &[
            "schema",
            "task_id",
            "conversation_id",
            "message_id",
            "prepared_kind",
            "owner_scope",
            "execution_text_source",
            "execution_text_sha256",
            "requested_capability_id",
            "initial_required_tool_name",
            "model_options",
            "bundle_revisions",
            "execution_metadata",
            "preparation_receipt",
            "upload_refs",
            "sheet_selections",
            "mcp_binding",
            "mcp_assignment",
            "available_mcp_servers",
            "pending_context",
            "planned_handoff_kind",
        ],
        "prepared execution",
    )?;
    if digest != domain_digest(b"maf.submission.prepared_execution.v1\0", &[bytes]) {
        return Err(write_failed("prepared execution digest mismatch"));
    }
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|_| write_failed("prepared execution JSON is invalid"))?;
    let object = exact_object(
        &value,
        &[
            "schema",
            "task_id",
            "conversation_id",
            "message_id",
            "prepared_kind",
            "owner_scope",
            "execution_text_source",
            "execution_text_sha256",
            "requested_capability_id",
            "initial_required_tool_name",
            "model_options",
            "bundle_revisions",
            "execution_metadata",
            "preparation_receipt",
            "upload_refs",
            "sheet_selections",
            "mcp_binding",
            "mcp_assignment",
            "available_mcp_servers",
            "pending_context",
            "planned_handoff_kind",
        ],
        "prepared execution",
    )?;
    let prepared_kind = non_empty_string(object, "prepared_kind", "prepared execution")?;
    if value["schema"].as_str() != Some("maf.submission.prepared_execution.v1")
        || !matches!(
            prepared_kind,
            "agent_run" | "interrupt" | "no_server_intent"
        )
        || value["planned_handoff_kind"].as_str() != Some(prepared_kind)
        || !matches!(
            value["execution_text_source"].as_str(),
            Some("root_message" | "pending_context" | "memory_context")
        )
        || value["execution_text_sha256"]
            .as_str()
            .is_none_or(|sha| !is_sha256(sha))
        || non_empty_string(object, "task_id", "prepared execution").is_err()
        || non_empty_string(object, "conversation_id", "prepared execution").is_err()
        || non_empty_string(object, "message_id", "prepared execution").is_err()
        || non_empty_string(object, "owner_scope", "prepared execution").is_err()
        || !is_nullable_string(&value["requested_capability_id"])
        || !is_nullable_string(&value["initial_required_tool_name"])
    {
        return Err(write_failed("prepared execution closed values are invalid"));
    }
    validate_model_options(&value["model_options"])?;
    validate_bundle_revisions(&value["bundle_revisions"])?;
    validate_execution_metadata(&value["execution_metadata"])?;
    validate_safe_references(
        object,
        value["conversation_id"].as_str().unwrap_or_default(),
    )?;
    let receipt = exact_object(
        &value["preparation_receipt"],
        &[
            "task_id",
            "receipt_sha256",
            "route_decision_sha256",
            "memory_context_sha256",
            "selector_decision_sha256",
        ],
        "preparation receipt",
    )?;
    if receipt["task_id"].as_str() != value["task_id"].as_str()
        || [
            "receipt_sha256",
            "route_decision_sha256",
            "memory_context_sha256",
            "selector_decision_sha256",
        ]
        .iter()
        .any(|key| receipt[*key].as_str().is_none_or(|sha| !is_sha256(sha)))
    {
        return Err(write_failed("preparation receipt binding is invalid"));
    }
    Ok(())
}

fn exact_object<'a>(
    value: &'a serde_json::Value,
    expected_keys: &[&str],
    label: &str,
) -> Result<&'a serde_json::Map<String, serde_json::Value>, RuntimeSidecarError> {
    let object = value
        .as_object()
        .ok_or_else(|| write_failed(&format!("{label} must be an object")))?;
    if object.keys().map(String::as_str).collect::<BTreeSet<_>>()
        != expected_keys.iter().copied().collect::<BTreeSet<_>>()
    {
        return Err(write_failed(&format!("{label} fields are invalid")));
    }
    Ok(object)
}

fn non_empty_string<'a>(
    object: &'a serde_json::Map<String, serde_json::Value>,
    key: &str,
    label: &str,
) -> Result<&'a str, RuntimeSidecarError> {
    object[key]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| write_failed(&format!("{label} {key} is invalid")))
}

fn is_nullable_string(value: &serde_json::Value) -> bool {
    value.is_null() || value.is_string()
}

fn validate_model_options(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    let object = exact_object(
        value,
        &["model_edition", "reasoning_effort", "thinking_enabled"],
        "model options",
    )?;
    if !is_nullable_string(&object["model_edition"])
        || non_empty_string(object, "reasoning_effort", "model options").is_err()
        || object["thinking_enabled"].as_bool().is_none()
    {
        return Err(write_failed("model options values are invalid"));
    }
    Ok(())
}

fn validate_bundle_revisions(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    let object = exact_object(
        value,
        &["skill_bundle_revision", "mcp_bundle_revision"],
        "bundle revisions",
    )?;
    if !is_nullable_string(&object["skill_bundle_revision"])
        || !is_nullable_string(&object["mcp_bundle_revision"])
    {
        return Err(write_failed("bundle revision values are invalid"));
    }
    Ok(())
}

fn validate_execution_metadata(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    let object = exact_object(
        value,
        &[
            "requested_capability_alias",
            "canonical_capability_id",
            "mcp_dispatch_server_id",
            "mcp_binding_mode",
            "mcp_command",
            "mcp_execution_mode",
            "mcp_rollout_config_version",
            "mcp_route_reason_code",
            "mcp_rollout_mode",
            "defer_task_completed_until_pending_skill_context_processed",
            "forced_by_mcp_command",
            "mcp_shadow_enabled",
        ],
        "execution metadata",
    )?;
    for key in [
        "requested_capability_alias",
        "canonical_capability_id",
        "mcp_dispatch_server_id",
        "mcp_binding_mode",
        "mcp_command",
        "mcp_execution_mode",
        "mcp_rollout_config_version",
        "mcp_route_reason_code",
        "mcp_rollout_mode",
    ] {
        if !is_nullable_string(&object[key]) {
            return Err(write_failed("execution metadata string value is invalid"));
        }
    }
    for key in [
        "defer_task_completed_until_pending_skill_context_processed",
        "forced_by_mcp_command",
        "mcp_shadow_enabled",
    ] {
        if !(object[key].is_null() || object[key].is_boolean()) {
            return Err(write_failed("execution metadata boolean value is invalid"));
        }
    }
    Ok(())
}

fn validate_safe_references(
    object: &serde_json::Map<String, serde_json::Value>,
    conversation_id: &str,
) -> Result<(), RuntimeSidecarError> {
    let uploads = object["upload_refs"]
        .as_array()
        .ok_or_else(|| write_failed("upload refs must be an array"))?;
    let mut previous_upload_id: Option<&str> = None;
    let mut selected_sheets = BTreeMap::new();
    for upload in uploads {
        let upload = exact_object(
            upload,
            &[
                "upload_id",
                "conversation_id",
                "sha256",
                "size_bytes",
                "selected_sheet",
            ],
            "upload ref",
        )?;
        let upload_id = non_empty_string(upload, "upload_id", "upload ref")?;
        if previous_upload_id.is_some_and(|previous| previous >= upload_id)
            || upload["conversation_id"].as_str() != Some(conversation_id)
            || upload["sha256"].as_str().is_none_or(|sha| !is_sha256(sha))
            || upload["size_bytes"].as_u64().is_none()
            || !is_nullable_string(&upload["selected_sheet"])
            || upload["selected_sheet"].as_str().is_some_and(str::is_empty)
        {
            return Err(write_failed("upload refs are not closed, sorted, or bound"));
        }
        previous_upload_id = Some(upload_id);
        if let Some(sheet) = upload["selected_sheet"].as_str() {
            selected_sheets.insert(upload_id, sheet);
        }
    }
    let sheets = object["sheet_selections"]
        .as_object()
        .ok_or_else(|| write_failed("sheet selections must be an object"))?;
    if sheets.len() != selected_sheets.len()
        || selected_sheets.iter().any(|(upload_id, sheet)| {
            sheets.get(*upload_id).and_then(serde_json::Value::as_str) != Some(*sheet)
        })
    {
        return Err(write_failed("sheet selections do not match upload refs"));
    }
    validate_mcp_binding(&object["mcp_binding"])?;
    validate_mcp_assignment(&object["mcp_assignment"])?;
    validate_available_mcp_servers(&object["available_mcp_servers"])?;
    validate_pending_context(&object["pending_context"])?;
    reject_forbidden_keys(&serde_json::Value::Object(object.clone()))?;
    Ok(())
}

fn validate_mcp_binding(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    if value.is_null() {
        return Ok(());
    }
    let object = exact_object(
        value,
        &[
            "server_id",
            "server_config_version",
            "server_security_version",
            "display_name",
            "command",
            "binding_mode",
        ],
        "MCP binding",
    )?;
    if ["server_id", "display_name", "command", "binding_mode"]
        .iter()
        .any(|key| non_empty_string(object, key, "MCP binding").is_err())
        || ["server_config_version", "server_security_version"]
            .iter()
            .any(|key| object[*key].as_u64().is_none_or(|value| value == 0))
    {
        return Err(write_failed("MCP binding values are invalid"));
    }
    Ok(())
}

fn validate_mcp_assignment(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    if value.is_null() {
        return Ok(());
    }
    let object = exact_object(
        value,
        &[
            "execution_mode",
            "shadow_enabled",
            "rollout_config_version",
            "route_reason_code",
            "rollout_mode",
        ],
        "MCP assignment",
    )?;
    if !matches!(
        object["execution_mode"].as_str(),
        Some("legacy" | "user_scoped" | "unavailable")
    ) || !matches!(
        object["rollout_mode"].as_str(),
        Some("off" | "shadow" | "enforce")
    ) || object["shadow_enabled"].as_bool().is_none()
        || non_empty_string(object, "rollout_config_version", "MCP assignment").is_err()
        || non_empty_string(object, "route_reason_code", "MCP assignment").is_err()
    {
        return Err(write_failed("MCP assignment values are invalid"));
    }
    Ok(())
}

fn validate_available_mcp_servers(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    let servers = value
        .as_array()
        .ok_or_else(|| write_failed("available MCP servers must be an array"))?;
    let mut previous_server_id: Option<&str> = None;
    for server in servers {
        let object = exact_object(
            server,
            &[
                "server_id",
                "display_name",
                "routing_description",
                "transport",
            ],
            "available MCP server",
        )?;
        let server_id = non_empty_string(object, "server_id", "available MCP server")?;
        if previous_server_id.is_some_and(|previous| previous >= server_id)
            || ["display_name", "transport"]
                .iter()
                .any(|key| non_empty_string(object, key, "available MCP server").is_err())
            || object["routing_description"].as_str().is_none()
        {
            return Err(write_failed(
                "available MCP servers are invalid or unsorted",
            ));
        }
        previous_server_id = Some(server_id);
    }
    Ok(())
}

fn validate_pending_context(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    if value.is_null() {
        return Ok(());
    }
    let object = exact_object(
        value,
        &[
            "context_id",
            "capability_id",
            "original_user_message",
            "assistant_message",
            "missing_requirements",
        ],
        "pending context",
    )?;
    if ["context_id", "capability_id"]
        .iter()
        .any(|key| non_empty_string(object, key, "pending context").is_err())
        || object["original_user_message"].as_str().is_none()
        || object["assistant_message"].as_str().is_none()
    {
        return Err(write_failed("pending context values are invalid"));
    }
    let requirements = object["missing_requirements"]
        .as_array()
        .ok_or_else(|| write_failed("pending requirements must be an array"))?;
    let mut previous: Option<&str> = None;
    for requirement in requirements {
        let requirement = requirement
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| write_failed("pending requirement is invalid"))?;
        if previous.is_some_and(|previous| previous >= requirement) {
            return Err(write_failed(
                "pending requirements must be sorted and unique",
            ));
        }
        previous = Some(requirement);
    }
    Ok(())
}

fn reject_forbidden_keys(value: &serde_json::Value) -> Result<(), RuntimeSidecarError> {
    const FORBIDDEN: &[&str] = &[
        "credential",
        "credentials",
        "password",
        "api_key",
        "access_token",
        "refresh_token",
        "file_content",
        "base64",
        "tool_arguments",
        "llm_prompt",
        "provider_response",
    ];
    match value {
        serde_json::Value::Object(object) => {
            for (key, nested) in object {
                if FORBIDDEN.contains(&key.as_str()) {
                    return Err(write_failed(
                        "submission envelope contains a forbidden field",
                    ));
                }
                reject_forbidden_keys(nested)?;
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                reject_forbidden_keys(item)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_canonical_json_object(
    bytes: &[u8],
    max_bytes: usize,
    expected_keys: &[&str],
    label: &str,
) -> Result<(), RuntimeSidecarError> {
    if bytes.is_empty() || bytes.len() > max_bytes {
        return Err(write_failed(&format!(
            "{label} exceeds its closed size contract"
        )));
    }
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|_| write_failed(&format!("{label} JSON is invalid")))?;
    let object = value
        .as_object()
        .ok_or_else(|| write_failed(&format!("{label} must be an object")))?;
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let expected = expected_keys.iter().copied().collect::<BTreeSet<_>>();
    if actual != expected {
        return Err(write_failed(&format!("{label} fields are invalid")));
    }
    let canonical = serde_json::to_vec(&value)
        .map_err(|_| write_failed(&format!("{label} cannot be canonicalized")))?;
    if canonical != bytes {
        return Err(write_failed(&format!("{label} JSON is not canonical")));
    }
    Ok(())
}

fn domain_digest(prefix: &[u8], parts: &[&[u8]]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(prefix);
    for part in parts {
        hasher.update(part);
    }
    format!("{:#x}", hasher.finalize())
        .trim_start_matches("0x")
        .to_owned()
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn validate_message_identity(
    identity: &MessageIdentityRecord,
    allow_submission: bool,
) -> Result<(), RuntimeSidecarError> {
    if identity.message_id.trim().is_empty()
        || identity.conversation_id.trim().is_empty()
        || identity.username.trim().is_empty()
        || identity.reserved_at_ms < 0
        || (!allow_submission && identity.identity_kind == MessageIdentityKind::Submission)
        || identity.identity_kind == MessageIdentityKind::LegacyConflictOnly
    {
        return Err(write_failed(
            "Message identity is outside the online closed contract",
        ));
    }
    let task_required = matches!(
        identity.identity_kind,
        MessageIdentityKind::Interrupt | MessageIdentityKind::ServerInternal
    );
    if task_required && identity.task_id.as_deref().is_none_or(str::is_empty) {
        return Err(write_failed("Message identity kind requires task_id"));
    }
    if identity.role.as_deref().is_none_or(str::is_empty)
        || identity.message_type.as_deref().is_none_or(str::is_empty)
        || identity.message_created_at_ms.is_none()
    {
        return Err(write_failed(
            "Message immutable identity fields are missing",
        ));
    }
    if identity.identity_kind == MessageIdentityKind::Interrupt {
        if identity
            .request_fingerprint
            .as_deref()
            .is_none_or(|value| !is_sha256(value))
        {
            return Err(write_failed(
                "Interrupt identity requires a request fingerprint",
            ));
        }
    } else if identity.request_fingerprint.is_some() {
        return Err(write_failed(
            "non-Interrupt reservation cannot carry a request fingerprint",
        ));
    }
    Ok(())
}

pub(crate) fn message_identity_is_exact_replay(
    existing: &MessageIdentityRecord,
    candidate: &MessageIdentityRecord,
) -> bool {
    if existing.identity_kind == MessageIdentityKind::LegacyConflictOnly
        || existing.identity_kind != candidate.identity_kind
        || existing.message_id != candidate.message_id
        || existing.conversation_id != candidate.conversation_id
        || existing.username != candidate.username
        || existing.role != candidate.role
        || existing.message_type != candidate.message_type
        || existing.task_id != candidate.task_id
        || existing.request_fingerprint != candidate.request_fingerprint
    {
        return false;
    }
    candidate.identity_kind == MessageIdentityKind::Interrupt
        || existing.message_created_at_ms == candidate.message_created_at_ms
}

fn validate_expected_status(
    expected: Option<&str>,
    current: Option<&String>,
) -> Result<(), RuntimeSidecarError> {
    match (expected, current) {
        (None | Some(""), None) => Ok(()),
        (Some(expected), Some(current)) if expected == current => Ok(()),
        _ => Err(idempotency_conflict(
            "expected status does not match current authoritative status",
        )),
    }
}

pub(crate) fn validate_agent_run_record(run: &AgentRunRecord) -> Result<(), RuntimeSidecarError> {
    if run.run_id.is_empty()
        || run.task_id.is_empty()
        || run.conversation_id.is_empty()
        || run.model_edition.is_empty()
        || run.reasoning_effort.is_empty()
        || run.next_item_sequence == 0
        || run.compacted_through_sequence >= run.next_item_sequence
        || !matches!(
            run.status.as_str(),
            "running"
                | "waiting_for_input"
                | "waiting_for_dependency"
                | "completed"
                | "failed"
                | "cancelled"
        )
    {
        return Err(write_failed("AgentRunRecord violates the closed contract"));
    }
    let digests: serde_json::Value = serde_json::from_slice(&run.binding_option_digests_json)
        .map_err(|_| write_failed("AgentRun binding digests JSON is invalid"))?;
    if !digests.is_object() {
        return Err(write_failed(
            "AgentRun binding digests JSON must be an object",
        ));
    }
    if run
        .waiting_call_item_ids
        .iter()
        .collect::<BTreeSet<_>>()
        .len()
        != run.waiting_call_item_ids.len()
    {
        return Err(write_failed("AgentRun waiting call IDs must be unique"));
    }
    let claim_shape = (
        run.claim_owner.is_some(),
        run.claim_token.is_some(),
        run.lease_expires_at_ms.is_some(),
    );
    if !matches!(claim_shape, (false, false, false) | (true, true, true)) {
        return Err(write_failed("AgentRun claim fields must be all-or-none"));
    }
    Ok(())
}

pub(crate) fn validate_agent_item_record(
    item: &AgentItemRecord,
    run: &AgentRunRecord,
) -> Result<(), RuntimeSidecarError> {
    if item.item_id.is_empty()
        || item.run_id != run.run_id
        || item.task_id != run.task_id
        || item.sequence == 0
        || !matches!(item.state.as_str(), "reserved" | "committed")
        || !matches!(
            item.kind.as_str(),
            "user_message"
                | "assistant_message"
                | "tool_call"
                | "tool_result"
                | "skill_activation"
                | "context_summary"
                | "continuation"
        )
        || item.payload_json.len() > 131_072
        || item.payload_size_bytes != item.payload_json.len() as u64
        || !item.payload_json.ends_with(b"\n")
    {
        return Err(write_failed("AgentItemRecord violates the closed contract"));
    }
    let parsed: serde_json::Value =
        serde_json::from_slice(&item.payload_json[..item.payload_json.len() - 1])
            .map_err(|_| write_failed("AgentItem payload JSON is invalid"))?;
    let mut canonical = serde_json::to_vec(&parsed)
        .map_err(|_| write_failed("AgentItem payload JSON cannot be canonicalized"))?;
    canonical.push(b'\n');
    if canonical != item.payload_json {
        return Err(write_failed("AgentItem payload JSON is not canonical"));
    }
    let digest = format!("{:x}", Sha256::digest(&item.payload_json));
    if digest != item.payload_sha256 {
        return Err(write_failed("AgentItem payload SHA-256 mismatch"));
    }
    if item.kind == "tool_result" && item.source_call_item_id.is_none() {
        return Err(write_failed("tool_result requires source_call_item_id"));
    }
    Ok(())
}

pub(crate) fn validate_agent_item_update(
    existing: &AgentItemRecord,
    updated: &AgentItemRecord,
) -> Result<(), RuntimeSidecarError> {
    let immutable_identity_matches = existing.item_id == updated.item_id
        && existing.run_id == updated.run_id
        && existing.task_id == updated.task_id
        && existing.sequence == updated.sequence
        && existing.kind == updated.kind
        && existing.parent_item_id == updated.parent_item_id
        && existing.source_call_item_id == updated.source_call_item_id
        && existing.provider_sample_id == updated.provider_sample_id
        && existing.call_ordinal == updated.call_ordinal
        && existing.created_at_ms == updated.created_at_ms;
    if !immutable_identity_matches
        || existing.kind != "tool_result"
        || existing.state != "reserved"
        || !matches!(updated.state.as_str(), "reserved" | "committed")
    {
        return Err(write_failed(
            "AgentItem update violates reserved result contract",
        ));
    }
    Ok(())
}

pub(crate) fn validate_agent_item_relationships(
    existing: &[AgentItemRecord],
    requested: &[AgentItemRecord],
) -> Result<(), RuntimeSidecarError> {
    let call_ids = existing
        .iter()
        .chain(requested.iter())
        .filter(|item| item.kind == "tool_call")
        .map(|item| item.item_id.clone())
        .collect::<BTreeSet<_>>();
    let mut result_ids_by_call = BTreeMap::new();
    for item in existing.iter().chain(requested.iter()) {
        if item.kind != "tool_result" {
            continue;
        }
        let source_call_item_id = item
            .source_call_item_id
            .as_ref()
            .ok_or_else(|| write_failed("Agent tool result source call is missing"))?;
        if !call_ids.contains(source_call_item_id) {
            return Err(write_failed("Agent tool result references an unknown call"));
        }
        if let Some(existing_result_id) =
            result_ids_by_call.insert(source_call_item_id.clone(), item.item_id.clone())
            && existing_result_id != item.item_id
        {
            return Err(write_failed("Agent call has more than one result"));
        }
    }
    Ok(())
}

pub(crate) fn validate_agent_final_projection(
    projection: &[u8],
    run: &AgentRunRecord,
    nodes: &[TaskNodeRecord],
    artifacts: &[ArtifactRecord],
    items: &[AgentItemRecord],
) -> Result<(), RuntimeSidecarError> {
    let value: serde_json::Value = serde_json::from_slice(projection)
        .map_err(|_| write_failed("Agent final projection JSON is invalid"))?;
    let root = value
        .as_object()
        .ok_or_else(|| write_failed("Agent final projection must be an object"))?;
    if root.keys().map(String::as_str).collect::<BTreeSet<_>>()
        != BTreeSet::from(["event", "message", "receipt"])
    {
        return Err(write_failed("Agent final projection fields are invalid"));
    }
    let event = root["event"]
        .as_object()
        .ok_or_else(|| write_failed("Agent final Event projection is invalid"))?;
    let message = root["message"]
        .as_object()
        .ok_or_else(|| write_failed("Agent final Message projection is invalid"))?;
    let receipt = root["receipt"]
        .as_object()
        .ok_or_else(|| write_failed("Agent final receipt projection is invalid"))?;
    let message_id = json_non_empty_string(message, "message_id")?;
    let event_id = json_non_empty_string(event, "event_id")?;
    let node_id = json_non_empty_string(receipt, "node_id")?;
    let artifact_id = json_non_empty_string(receipt, "artifact_id")?;
    let assistant_item_id = json_non_empty_string(receipt, "assistant_item_id")?;
    if json_non_empty_string(message, "conversation_id")? != run.conversation_id
        || json_non_empty_string(message, "task_id")? != run.task_id
        || json_non_empty_string(message, "role")? != "assistant"
        || json_non_empty_string(event, "event_type")? != "agent.final_output"
        || json_non_empty_string(event, "message_id")? != message_id
        || json_non_empty_string(receipt, "event_id")? != event_id
        || json_non_empty_string(receipt, "message_id")? != message_id
        || json_non_empty_string(receipt, "run_id")? != run.run_id
        || json_non_empty_string(receipt, "task_id")? != run.task_id
        || !nodes.iter().any(|node| node.node_id == node_id)
        || !artifacts
            .iter()
            .any(|artifact| artifact.artifact_id == artifact_id)
        || !items.iter().any(|item| {
            item.item_id == assistant_item_id
                && item.kind == "assistant_message"
                && item.state == "committed"
        })
    {
        return Err(write_failed(
            "Agent final projection references are inconsistent",
        ));
    }
    json_non_empty_string(receipt, "receipt_id")?;
    json_non_empty_string(receipt, "text_sha256")?;
    json_non_empty_string(message, "content")?;
    Ok(())
}

fn json_non_empty_string<'a>(
    object: &'a serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<&'a str, RuntimeSidecarError> {
    object
        .get(key)
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| write_failed("Agent final projection identity is invalid"))
}

pub(crate) fn validate_agent_final_commit_shape(
    operation: &str,
    run: &AgentRunRecord,
    items: &[AgentItemRecord],
    nodes: &[TaskNodeRecord],
    artifacts: &[ArtifactRecord],
    task: Option<&TaskRecord>,
    projection: Option<&[u8]>,
) -> Result<(), RuntimeSidecarError> {
    if operation != "commit_final" {
        if projection.is_some() {
            return Err(write_failed("Agent final projection operation conflict"));
        }
        return Ok(());
    }
    let task = task.ok_or_else(|| write_failed("Agent final Task projection is required"))?;
    if projection.is_none()
        || run.status != "completed"
        || run.terminal_at_ms.is_none()
        || run.claim_owner.is_some()
        || run.claim_token.is_some()
        || run.lease_expires_at_ms.is_some()
        || !run.waiting_call_item_ids.is_empty()
        || items.len() != 1
        || nodes.len() != 1
        || artifacts.len() != 1
        || items[0].kind != "assistant_message"
        || items[0].state != "committed"
        || nodes[0].capability_id != "agent.final_output"
        || nodes[0].status != "completed"
        || artifacts[0].producer_node_id != nodes[0].node_id
        || task.status != "completed"
    {
        return Err(write_failed("Agent final commit shape is invalid"));
    }
    Ok(())
}

pub(crate) fn idempotency_conflict(message: &str) -> RuntimeSidecarError {
    RuntimeSidecarError::new(
        RuntimeSidecarErrorCode::RuntimeStoreIdempotencyConflict,
        message,
    )
}

pub(crate) fn migration_blocked(message: &str) -> RuntimeSidecarError {
    RuntimeSidecarError::new(
        RuntimeSidecarErrorCode::RuntimeStoreMigrationBlocked,
        message,
    )
}

fn write_failed(message: &str) -> RuntimeSidecarError {
    RuntimeSidecarError::new(RuntimeSidecarErrorCode::RuntimeStoreWriteFailed, message)
}

fn config_untrusted(message: &str) -> RuntimeSidecarError {
    RuntimeSidecarError::new(
        RuntimeSidecarErrorCode::RuntimeStoreConfigUntrusted,
        message,
    )
}

fn shutdown_drain_ms() -> i64 {
    runtime_sidecar_contract_artifact()
        .resource_limits
        .get("shutdown_drain_ms")
        .copied()
        .unwrap_or(30_000) as i64
}

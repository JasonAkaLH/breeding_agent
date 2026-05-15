//! Runtime sidecar service kernel.
//!
//! This crate owns the transport-independent runtime sidecar write semantics.
//! A tonic/gRPC wrapper can delegate to this kernel without reintroducing
//! Python-owned dispatcher, lease, event, cancellation, or bundle state.

use maf_event_log::EventLog;
use maf_runtime_store::{
    ERROR_CODE_TABLE_HASH as RUNTIME_ERROR_CODE_TABLE_HASH, FEATURE_EVENT_LOG,
    FEATURE_RUNTIME_STORE, FEATURE_TASK_DISPATCHER, LeaseRegistry,
    PROTOCOL_VERSION as RUNTIME_PROTOCOL_VERSION, RuntimeSidecarError, RuntimeSidecarErrorCode,
    SCHEMA_HASH, TaskLease, runtime_sidecar_contract_artifact,
};
use maf_task_dispatcher::{
    TaskDispatcher, TaskSubmitRequest as DispatcherTaskSubmitRequest, TaskSubmitResult,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fs;
use std::future::Future;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use tokio_stream::wrappers::TcpListenerStream;
#[cfg(unix)]
use tokio_stream::wrappers::UnixListenerStream;
use tonic::transport::{Certificate, Identity, ServerTlsConfig};

mod sqlite_adapter;
pub use sqlite_adapter::RuntimeSidecarSqliteAdapter;

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
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SubmitTaskResponse {
    pub task_id: String,
    pub duplicate: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionNodeRequest {
    pub task_id: String,
    pub node_id: String,
    pub to_status: String,
    pub idempotency: Option<Idempotency>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionNodeResponse {
    pub node_id: String,
    pub status: String,
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

#[derive(Debug)]
pub struct RuntimeSidecarKernel {
    dispatcher: TaskDispatcher,
    event_log: EventLog,
    leases: LeaseRegistry,
    node_statuses: BTreeMap<(String, String), String>,
    cancellation_tokens: BTreeMap<String, CancellationToken>,
    bundle_pins: BTreeMap<(String, String), BundlePin>,
    event_append_idempotency: BTreeMap<String, EventCursor>,
    lease_acquire_idempotency: BTreeMap<String, TaskLease>,
    task_submit_idempotency: BTreeMap<String, TaskSubmitResult>,
    node_transition_idempotency: BTreeMap<String, NodeTransitionResult>,
    cancellation_idempotency: BTreeMap<String, bool>,
    bundle_revision_idempotency: BTreeMap<String, BundleRevisionResult>,
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
            cancellation_tokens: BTreeMap::new(),
            bundle_pins: BTreeMap::new(),
            event_append_idempotency: BTreeMap::new(),
            lease_acquire_idempotency: BTreeMap::new(),
            task_submit_idempotency: BTreeMap::new(),
            node_transition_idempotency: BTreeMap::new(),
            cancellation_idempotency: BTreeMap::new(),
            bundle_revision_idempotency: BTreeMap::new(),
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
        self.task_submit_idempotency
            .insert(idempotency_key, result.clone());
        Ok(result)
    }

    pub fn transition_node(
        &mut self,
        task_id: impl Into<String>,
        node_id: impl Into<String>,
        to_status: impl Into<String>,
        idempotency_key: impl Into<String>,
    ) -> Result<NodeTransitionResult, RuntimeSidecarError> {
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(result) = self.node_transition_idempotency.get(&idempotency_key) {
            return Ok(result.clone());
        }
        let result = NodeTransitionResult {
            task_id: task_id.into(),
            node_id: node_id.into(),
            status: to_status.into(),
        };
        self.node_statuses.insert(
            (result.task_id.clone(), result.node_id.clone()),
            result.status.clone(),
        );
        self.node_transition_idempotency
            .insert(idempotency_key, result.clone());
        Ok(result)
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
        self.ensure_accepting_writes()?;
        let idempotency_key = require_idempotency_key(idempotency_key)?;
        if let Some(cursor) = self.event_append_idempotency.get(&idempotency_key) {
            return Ok(cursor.clone());
        }
        let event = self.event_log.append(
            conversation_id,
            task_id,
            event_type,
            payload_json,
            created_at_ms,
        )?;
        let cursor = EventCursor {
            conversation_id: event.conversation_id,
            task_id: event.task_id,
            sequence: event.sequence,
            created_at_ms: event.created_at_ms,
        };
        self.event_append_idempotency
            .insert(idempotency_key, cursor.clone());
        Ok(cursor)
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
        match self.kernel.submit_task(
            request.task_id,
            request.conversation_id,
            idempotency_key(request.idempotency),
        ) {
            Ok(result) => SubmitTaskResponse {
                task_id: result.task_id,
                duplicate: result.duplicate,
                error: None,
            },
            Err(error) => SubmitTaskResponse {
                task_id: String::new(),
                duplicate: false,
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn transition_node(&mut self, request: TransitionNodeRequest) -> TransitionNodeResponse {
        match self.kernel.transition_node(
            request.task_id,
            request.node_id,
            request.to_status,
            idempotency_key(request.idempotency),
        ) {
            Ok(result) => TransitionNodeResponse {
                node_id: result.node_id,
                status: result.status,
                error: None,
            },
            Err(error) => TransitionNodeResponse {
                node_id: String::new(),
                status: String::new(),
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    pub fn append_event(&mut self, request: AppendEventRequest) -> AppendEventResponse {
        match self.kernel.append_event(
            request.conversation_id,
            request.task_id,
            request.event_type,
            request.payload_json,
            request.created_at_ms,
            idempotency_key(request.idempotency),
        ) {
            Ok(cursor) => AppendEventResponse {
                cursor: Some(cursor),
                error: None,
            },
            Err(error) => AppendEventResponse {
                cursor: None,
                error: Some(TypedErrorEnvelope::from(error)),
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
            let response = match adapter.submit_task(
                &request.task_id,
                &request.conversation_id,
                &pb_idempotency_key(request.idempotency),
            ) {
                Ok(result) => SubmitTaskResponse {
                    task_id: result.task_id,
                    duplicate: result.duplicate,
                    error: None,
                },
                Err(error) => SubmitTaskResponse {
                    task_id: String::new(),
                    duplicate: false,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(runtime_pb::SubmitTaskResponse {
                task_id: response.task_id,
                duplicate: response.duplicate,
                error: response.error.map(typed_error_to_pb),
            }));
        }
        let response = self.lock()?.submit_task(SubmitTaskRequest {
            task_id: request.task_id,
            conversation_id: request.conversation_id,
            idempotency: request.idempotency.map(idempotency_from_pb),
        });
        Ok(tonic::Response::new(runtime_pb::SubmitTaskResponse {
            task_id: response.task_id,
            duplicate: response.duplicate,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn transition_node(
        &self,
        request: tonic::Request<runtime_pb::TransitionNodeRequest>,
    ) -> Result<tonic::Response<runtime_pb::TransitionNodeResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match adapter.transition_node(
                &request.task_id,
                &request.node_id,
                &request.to_status,
                &pb_idempotency_key(request.idempotency),
            ) {
                Ok(result) => TransitionNodeResponse {
                    node_id: result.node_id,
                    status: result.status,
                    error: None,
                },
                Err(error) => TransitionNodeResponse {
                    node_id: String::new(),
                    status: String::new(),
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(runtime_pb::TransitionNodeResponse {
                node_id: response.node_id,
                status: response.status,
                error: response.error.map(typed_error_to_pb),
            }));
        }
        let response = self.lock()?.transition_node(TransitionNodeRequest {
            task_id: request.task_id,
            node_id: request.node_id,
            to_status: request.to_status,
            idempotency: request.idempotency.map(idempotency_from_pb),
        });
        Ok(tonic::Response::new(runtime_pb::TransitionNodeResponse {
            node_id: response.node_id,
            status: response.status,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn append_event(
        &self,
        request: tonic::Request<runtime_pb::AppendEventRequest>,
    ) -> Result<tonic::Response<runtime_pb::AppendEventResponse>, tonic::Status> {
        let request = request.into_inner();
        if let Some(adapter) = &self.sqlite_adapter {
            let response = match adapter.append_event(
                &request.conversation_id,
                &request.task_id,
                &request.event_type,
                request.payload_json,
                0,
                &pb_idempotency_key(request.idempotency),
            ) {
                Ok(cursor) => AppendEventResponse {
                    cursor: Some(cursor),
                    error: None,
                },
                Err(error) => AppendEventResponse {
                    cursor: None,
                    error: Some(TypedErrorEnvelope::from(error)),
                },
            };
            return Ok(tonic::Response::new(runtime_pb::AppendEventResponse {
                cursor: response.cursor.map(cursor_to_pb),
                error: response.error.map(typed_error_to_pb),
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
            let response = match adapter.acquire_lease(
                &request.task_id,
                &request.owner_id,
                request.now_ms,
                request.ttl_ms,
                &pb_idempotency_key(request.idempotency),
            ) {
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
            let response = match adapter.renew_lease(
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
            let response = match adapter.release_lease(&request.task_id, &request.renew_token) {
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
            let response = match adapter.write_cancellation_token(
                &request.task_id,
                request.requested_at_ms,
                &request.reason,
                &request.terminal_policy,
                &pb_idempotency_key(request.idempotency),
            ) {
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
            let response = match adapter.pin_bundle_revision(
                &request.task_id,
                &request.bundle_kind,
                &request.revision,
                &pb_idempotency_key(request.idempotency),
            ) {
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
            let response = match adapter.release_bundle_revision(
                &request.task_id,
                &request.bundle_kind,
                &request.revision,
                request.released_at_ms,
                &pb_idempotency_key(request.idempotency),
            ) {
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
        .add_service(runtime_pb::runtime_sidecar_server::RuntimeSidecarServer::new(service))
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
        .add_service(runtime_pb::runtime_sidecar_server::RuntimeSidecarServer::new(service))
        .serve(listen_addr)
        .await?;
    Ok(())
}

#[cfg(unix)]
pub async fn serve_runtime_sidecar_unix_socket(
    socket_path: &Path,
    service: RuntimeSidecarGrpcService,
) -> Result<(), Box<dyn Error + Send + Sync>> {
    let listener = tokio::net::UnixListener::bind(socket_path)?;
    let incoming = UnixListenerStream::new(listener);
    tonic::transport::Server::builder()
        .add_service(runtime_pb::runtime_sidecar_server::RuntimeSidecarServer::new(service))
        .serve_with_incoming(incoming)
        .await?;
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
        .add_service(runtime_pb::runtime_sidecar_server::RuntimeSidecarServer::new(service))
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await
}

#[must_use]
pub fn supported_features() -> Vec<String> {
    vec![
        FEATURE_RUNTIME_STORE.to_owned(),
        FEATURE_EVENT_LOG.to_owned(),
        FEATURE_TASK_DISPATCHER.to_owned(),
    ]
}

fn idempotency_key(idempotency: Option<Idempotency>) -> String {
    idempotency.map_or_else(String::new, |value| value.key)
}

fn pb_idempotency_key(idempotency: Option<runtime_pb::Idempotency>) -> String {
    idempotency.map_or_else(String::new, |value| value.key)
}

fn missing_features_from_error(error: &RuntimeSidecarError) -> Vec<String> {
    error
        .safe_metadata
        .get("missing_features")
        .map(|features| {
            features
                .split(',')
                .filter(|feature| !feature.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn version_to_pb(version: RuntimeSidecarVersion) -> common_pb::VersionInfo {
    common_pb::VersionInfo {
        component: version.component,
        build_version: env!("CARGO_PKG_VERSION").to_owned(),
        protocol_version: version.protocol_version,
        schema_hash: version.schema_hash,
        error_code_table_hash: version.error_code_table_hash,
        supported_features: version.supported_features,
        min_client_version: "0.1.0".to_owned(),
        max_client_version: "0.1.x".to_owned(),
    }
}

fn health_state_to_pb(state: HealthState) -> common_pb::HealthState {
    match state {
        HealthState::Serving => common_pb::HealthState::Serving,
        HealthState::NotServing => common_pb::HealthState::NotServing,
        HealthState::Degraded => common_pb::HealthState::Degraded,
    }
}

fn readiness_state_to_pb(state: ReadinessState) -> common_pb::ReadinessState {
    match state {
        ReadinessState::Ready => common_pb::ReadinessState::Ready,
        ReadinessState::NotReady => common_pb::ReadinessState::NotReady,
    }
}

fn typed_error_to_pb(error: TypedErrorEnvelope) -> common_pb::TypedError {
    common_pb::TypedError {
        code: error.code,
        message: error.message,
        retriable: error.retriable,
        category: error_category_to_pb(&error.category) as i32,
        safe_metadata: error.safe_metadata.into_iter().collect(),
    }
}

fn error_category_to_pb(category: &str) -> common_pb::ErrorCategory {
    match category {
        "configuration" => common_pb::ErrorCategory::Configuration,
        "compatibility" => common_pb::ErrorCategory::Compatibility,
        "security" => common_pb::ErrorCategory::Security,
        "resource_limit" => common_pb::ErrorCategory::ResourceLimit,
        "protocol" => common_pb::ErrorCategory::Protocol,
        "upstream" => common_pb::ErrorCategory::Upstream,
        "cancellation" => common_pb::ErrorCategory::Cancellation,
        _ => common_pb::ErrorCategory::Internal,
    }
}

fn idempotency_from_pb(idempotency: runtime_pb::Idempotency) -> Idempotency {
    Idempotency {
        key: idempotency.key,
        owner: idempotency.owner,
        deadline_ms: idempotency.deadline_ms,
    }
}

fn cursor_to_pb(cursor: EventCursor) -> runtime_pb::EventCursor {
    runtime_pb::EventCursor {
        conversation_id: cursor.conversation_id,
        task_id: cursor.task_id,
        sequence: cursor.sequence,
        created_at_ms: cursor.created_at_ms,
    }
}

fn lease_response_to_pb(response: LeaseResponse) -> runtime_pb::LeaseResponse {
    let lease = response.lease;
    runtime_pb::LeaseResponse {
        task_id: lease
            .as_ref()
            .map(|lease| lease.task_id.clone())
            .unwrap_or_default(),
        owner_id: lease
            .as_ref()
            .map(|lease| lease.owner_id.clone())
            .unwrap_or_default(),
        revision: lease.as_ref().map_or(0, |lease| lease.revision),
        expires_at_ms: lease.as_ref().map_or(0, |lease| lease.expires_at_ms),
        renew_token: lease
            .as_ref()
            .map(|lease| lease.renew_token.clone())
            .unwrap_or_default(),
        error: response.error.map(typed_error_to_pb),
    }
}

fn bundle_revision_response_to_pb(
    response: BundleRevisionResponse,
) -> runtime_pb::BundleRevisionResponse {
    let result = response.result;
    runtime_pb::BundleRevisionResponse {
        task_id: result
            .as_ref()
            .map(|result| result.task_id.clone())
            .unwrap_or_default(),
        bundle_kind: result
            .as_ref()
            .map(|result| result.bundle_kind.clone())
            .unwrap_or_default(),
        revision: result
            .as_ref()
            .map(|result| result.revision.clone())
            .unwrap_or_default(),
        released: result.as_ref().is_some_and(|result| result.released),
        error: response.error.map(typed_error_to_pb),
    }
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

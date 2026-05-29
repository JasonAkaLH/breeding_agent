//! Rust policy kernel for generic Skill runtime and sandbox contracts.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error as StdError;
use std::future::Future;
use std::io::{Read, Write};
use std::net::SocketAddr;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;

pub mod pb {
    pub mod common {
        pub mod v1 {
            tonic::include_proto!("maf.common.v1");
        }
    }

    pub mod skill {
        pub mod v1 {
            tonic::include_proto!("maf.skill.v1");
        }
    }
}

use pb::common::v1 as common_pb;
use pb::skill::v1 as skill_pb;

pub const COMPONENT_ID: &str = "maf_skill_runtime";
pub const PROTOCOL_VERSION: &str = "maf.skill.v1";
pub const CONTRACT_VERSION: &str = "skill_runtime.v1";
pub const SCHEMA_HASH: &str = "maf_skill_runtime_schema_gates_20260515";
pub const ERROR_CODE_TABLE_HASH: &str = "maf_skill_runtime_error_table_v1_gates_20260515";
pub const DEFAULT_SKILL_SANDBOX_LISTEN_ADDR: &str = "127.0.0.1:50052";
pub const MIN_CLIENT_VERSION: &str = "0.1.0";
pub const MAX_CLIENT_VERSION: &str = "0.1.x";
const SANDBOX_ENV_PATH: &str = "/usr/bin:/bin";
const STDIO_DRAIN_GRACE_MS: u64 = 5_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorCodeEntry {
    pub code: String,
    pub category: String,
    pub retriable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactProvenancePolicy {
    pub allowed_sources: Vec<String>,
    pub allowed_artifact_kinds: Vec<String>,
    pub required_fields: Vec<String>,
    pub require_contract_version_match: bool,
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
pub struct SkillRuntimeContractArtifact {
    pub component: String,
    pub protocol_version: String,
    pub contract_version: String,
    pub client_version: String,
    pub min_client_version: String,
    pub max_client_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub mode_env: String,
    pub modes: Vec<String>,
    pub supported_features: Vec<String>,
    pub allowed_execution_modes: Vec<String>,
    pub default_execution_modes: BTreeMap<String, String>,
    pub allowed_answer_modes: Vec<String>,
    pub default_answer_mode_by_execution_mode: BTreeMap<String, String>,
    pub answer_mode_required_execution_modes: Vec<String>,
    pub allowed_rust_adapters: Vec<String>,
    pub forbidden_x_runtime_rust_keys: Vec<String>,
    pub sandbox_limits: BTreeMap<String, u64>,
    pub artifact_policy: ArtifactProvenancePolicy,
    pub benchmark_policy: BenchmarkPolicy,
    pub promotion_policy: PromotionPolicy,
    pub ops_policy: OpsPolicy,
    pub decommission_policy: DecommissionPolicy,
    pub error_codes: Vec<ErrorCodeEntry>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SkillRuntimeErrorCode {
    ManifestInvalid,
    PublicRootEscape,
    ServiceNotAllowlisted,
    HandlerNotAllowlisted,
    RustAdapterInvalid,
    SandboxPolicyDenied,
    SandboxTimeout,
    OutputTooLarge,
    ContractMismatch,
    ArtifactUntrusted,
    BenchmarkInvalid,
    PromotionBlocked,
    OpsReadinessBlocked,
    DecommissionBlocked,
}

impl SkillRuntimeErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ManifestInvalid => "skill_runtime_manifest_invalid",
            Self::PublicRootEscape => "skill_runtime_public_root_escape",
            Self::ServiceNotAllowlisted => "skill_runtime_service_not_allowlisted",
            Self::HandlerNotAllowlisted => "skill_runtime_handler_not_allowlisted",
            Self::RustAdapterInvalid => "skill_runtime_rust_adapter_invalid",
            Self::SandboxPolicyDenied => "skill_runtime_sandbox_policy_denied",
            Self::SandboxTimeout => "skill_runtime_sandbox_timeout",
            Self::OutputTooLarge => "skill_runtime_output_too_large",
            Self::ContractMismatch => "skill_runtime_contract_mismatch",
            Self::ArtifactUntrusted => "skill_runtime_artifact_untrusted",
            Self::BenchmarkInvalid => "skill_runtime_benchmark_invalid",
            Self::PromotionBlocked => "skill_runtime_promotion_blocked",
            Self::OpsReadinessBlocked => "skill_runtime_ops_readiness_blocked",
            Self::DecommissionBlocked => "skill_runtime_decommission_blocked",
        }
    }

    #[must_use]
    pub const fn category(self) -> &'static str {
        match self {
            Self::SandboxTimeout | Self::OutputTooLarge => "resource_limit",
            Self::ContractMismatch => "compatibility",
            Self::BenchmarkInvalid
            | Self::PromotionBlocked
            | Self::OpsReadinessBlocked
            | Self::DecommissionBlocked => "quality_gate",
            _ => "security",
        }
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{code}: {message}")]
pub struct SkillRuntimeError {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

impl SkillRuntimeError {
    #[must_use]
    pub fn new(code: SkillRuntimeErrorCode, message: impl Into<String>) -> Self {
        Self {
            code: code.as_str().to_owned(),
            message: message.into(),
            retriable: false,
            category: code.category().to_owned(),
            safe_metadata: BTreeMap::new(),
        }
    }
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
pub struct SkillRuntimeVersion {
    pub component: String,
    pub protocol_version: String,
    pub schema_hash: String,
    pub error_code_table_hash: String,
    pub supported_features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompatibilityCheck {
    pub client_version: String,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HealthStatus {
    pub state: HealthState,
    pub version: SkillRuntimeVersion,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadinessStatus {
    pub state: ReadinessState,
    pub version: SkillRuntimeVersion,
    pub compatibility_handshake_passed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TypedErrorEnvelope {
    pub code: String,
    pub message: String,
    pub retriable: bool,
    pub category: String,
    pub safe_metadata: BTreeMap<String, String>,
}

impl From<SkillRuntimeError> for TypedErrorEnvelope {
    fn from(error: SkillRuntimeError) -> Self {
        Self {
            code: error.code,
            message: error.message,
            retriable: error.retriable,
            category: error.category,
            safe_metadata: error.safe_metadata,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkillPolicyInput {
    pub skill_name: String,
    pub capability_id: String,
    pub execution_mode: String,
    pub trust_scope: String,
    pub handler: String,
    pub manifest_services: Vec<String>,
    pub runtime_allowlist_services: Vec<String>,
    pub requested_services: Vec<String>,
    pub runtime_allowlist_handlers: Vec<String>,
    pub x_runtime_rust: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkillPolicyDecision {
    pub allowed: bool,
    pub bundle_fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidatePolicyResponse {
    pub allowed: bool,
    pub bundle_fingerprint: String,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecuteSandboxedRequest {
    pub skill_name: String,
    pub execution_mode: String,
    pub cwd_under_public_root: String,
    pub argv: Vec<String>,
    pub timeout_ms: u32,
    pub stdout_limit_bytes: u32,
    pub stderr_limit_bytes: u32,
    pub stdin_payload: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecuteSandboxedResponse {
    pub exit_code: i32,
    pub stdout_prefix: Vec<u8>,
    pub stderr_prefix: Vec<u8>,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
    pub error: Option<TypedErrorEnvelope>,
}

#[derive(Debug, Clone)]
pub struct SandboxProcessManager {
    sandbox_root: PathBuf,
}

impl SandboxProcessManager {
    #[must_use]
    pub fn new(sandbox_root: impl AsRef<Path>) -> Self {
        Self {
            sandbox_root: sandbox_root.as_ref().to_path_buf(),
        }
    }

    #[must_use]
    pub fn execute(&self, request: &ExecuteSandboxedRequest) -> ExecuteSandboxedResponse {
        match self.try_execute(request) {
            Ok(response) => response,
            Err(error) => sandbox_error_response(error),
        }
    }

    fn try_execute(
        &self,
        request: &ExecuteSandboxedRequest,
    ) -> Result<ExecuteSandboxedResponse, SkillRuntimeError> {
        let program = request.argv.first().ok_or_else(|| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox execution argv is required",
            )
        })?;
        if !program.contains('/') {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox executable must be a relative path under the sandbox root",
            ));
        }
        guard_public_root_path(&request.cwd_under_public_root)?;
        guard_public_root_path(program)?;

        let root = self.canonical_root()?;
        let current_dir = checked_join_under_root(&root, &request.cwd_under_public_root)?;
        let executable = checked_join_under_root(&current_dir, program)?;
        let timeout = bounded_timeout(request.timeout_ms)?;
        validate_stdin_limit(request.stdin_payload.len())?;
        let stdout_limit = requested_stream_limit("stdout_bytes", request.stdout_limit_bytes)?;
        let stderr_limit = requested_stream_limit("stderr_bytes", request.stderr_limit_bytes)?;
        let mut command = Command::new(&executable);
        command
            .args(request.argv.iter().skip(1))
            .current_dir(&current_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        configure_sandbox_environment(&mut command);
        configure_child_process_group(&mut command);
        let mut child = command.spawn().map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox process failed to start",
            )
        })?;
        let child_id = child.id();
        let stdout_reader = child
            .stdout
            .take()
            .map(|pipe| spawn_limited_reader(pipe, stdout_limit, "stdout"));
        let stderr_reader = child
            .stderr
            .take()
            .map(|pipe| spawn_limited_reader(pipe, stderr_limit, "stderr"));
        let stdin_writer = child
            .stdin
            .take()
            .map(|stdin| spawn_stdin_writer(stdin, request.stdin_payload.clone()));

        let deadline = Instant::now() + timeout;
        let status = loop {
            if let Some(status) = child.try_wait().map_err(|_| {
                SkillRuntimeError::new(
                    SkillRuntimeErrorCode::SandboxPolicyDenied,
                    "sandbox process wait failed",
                )
            })? {
                break status;
            }
            if Instant::now() >= deadline {
                kill_child_process_group(child_id);
                let _ = child.kill();
                let _ = child.wait().map_err(|_| {
                    SkillRuntimeError::new(
                        SkillRuntimeErrorCode::SandboxTimeout,
                        "sandbox process timed out and wait failed",
                    )
                })?;
                let drain_deadline = stdio_drain_deadline();
                let (stdout_prefix, stdout_truncated) =
                    receive_limited_reader(stdout_reader, drain_deadline)?;
                let (stderr_prefix, stderr_truncated) =
                    receive_limited_reader(stderr_reader, drain_deadline)?;
                let _ = receive_stdin_writer(stdin_writer, drain_deadline);
                return Ok(ExecuteSandboxedResponse {
                    exit_code: -1,
                    stdout_prefix,
                    stderr_prefix,
                    stdout_truncated,
                    stderr_truncated,
                    error: Some(TypedErrorEnvelope::from(SkillRuntimeError::new(
                        SkillRuntimeErrorCode::SandboxTimeout,
                        "sandbox process timed out",
                    ))),
                });
            }
            thread::sleep(Duration::from_millis(5));
        };

        kill_child_process_group(child_id);
        let drain_deadline = stdio_drain_deadline();
        receive_stdin_writer(stdin_writer, drain_deadline)?;
        let (stdout_prefix, stdout_truncated) =
            receive_limited_reader(stdout_reader, drain_deadline)?;
        let (stderr_prefix, stderr_truncated) =
            receive_limited_reader(stderr_reader, drain_deadline)?;
        Ok(bounded_output_response(
            status.code().unwrap_or(-1),
            stdout_prefix,
            stdout_truncated,
            stderr_prefix,
            stderr_truncated,
        ))
    }

    fn canonical_root(&self) -> Result<PathBuf, SkillRuntimeError> {
        self.sandbox_root.canonicalize().map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox root is not accessible",
            )
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct SkillSandboxService {
    compatibility_handshake_passed: bool,
    process_manager: Option<SandboxProcessManager>,
}

impl SkillSandboxService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            compatibility_handshake_passed: false,
            process_manager: None,
        }
    }

    #[must_use]
    pub fn with_process_manager(process_manager: SandboxProcessManager) -> Self {
        Self {
            compatibility_handshake_passed: false,
            process_manager: Some(process_manager),
        }
    }

    #[must_use]
    pub fn version(&self) -> SkillRuntimeVersion {
        let artifact = skill_runtime_contract_artifact();
        SkillRuntimeVersion {
            component: artifact.component,
            protocol_version: artifact.protocol_version,
            schema_hash: artifact.schema_hash,
            error_code_table_hash: artifact.error_code_table_hash,
            supported_features: artifact.supported_features,
        }
    }

    #[must_use]
    pub fn health(&self) -> HealthStatus {
        HealthStatus {
            state: HealthState::Serving,
            version: self.version(),
        }
    }

    #[must_use]
    pub fn readiness(&self) -> ReadinessStatus {
        ReadinessStatus {
            state: if self.compatibility_handshake_passed {
                ReadinessState::Ready
            } else {
                ReadinessState::NotReady
            },
            version: self.version(),
            compatibility_handshake_passed: self.compatibility_handshake_passed,
        }
    }

    pub fn check_compatibility(
        &self,
        check: CompatibilityCheck,
    ) -> Result<CompatibilityResult, SkillRuntimeError> {
        let version = self.version();
        if check.expected_component != version.component
            || check.expected_protocol_version != version.protocol_version
            || check.expected_schema_hash != version.schema_hash
            || check.expected_error_code_table_hash != version.error_code_table_hash
        {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::ContractMismatch,
                "skill sandbox protocol handshake is incompatible",
            ));
        }
        validate_client_version(&check.client_version)?;

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
            let mut error = SkillRuntimeError::new(
                SkillRuntimeErrorCode::ContractMismatch,
                "skill sandbox required features are missing",
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
    ) -> Result<CompatibilityResult, SkillRuntimeError> {
        let result = self.check_compatibility(check)?;
        self.compatibility_handshake_passed = true;
        Ok(result)
    }

    #[must_use]
    pub fn validate_policy(&self, input: SkillPolicyInput) -> ValidatePolicyResponse {
        match validate_policy(&input) {
            Ok(decision) => ValidatePolicyResponse {
                allowed: decision.allowed,
                bundle_fingerprint: decision.bundle_fingerprint,
                error: None,
            },
            Err(error) => ValidatePolicyResponse {
                allowed: false,
                bundle_fingerprint: String::new(),
                error: Some(TypedErrorEnvelope::from(error)),
            },
        }
    }

    #[must_use]
    pub fn execute_sandboxed(&self, request: ExecuteSandboxedRequest) -> ExecuteSandboxedResponse {
        if let Err(error) = guard_public_root_path(&request.cwd_under_public_root) {
            return sandbox_error_response(error);
        }
        if !allowed_execution_modes().contains(&request.execution_mode) {
            return sandbox_error_response(SkillRuntimeError::new(
                SkillRuntimeErrorCode::ManifestInvalid,
                "execution mode is not supported by generic skill runtime",
            ));
        }
        if let Some(process_manager) = &self.process_manager {
            return process_manager.execute(&request);
        }
        sandbox_error_response(SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "rust skill sandbox process manager is not enabled yet",
        ))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkillSandboxServeConfig {
    pub listen_addr: SocketAddr,
    pub sandbox_root: Option<PathBuf>,
}

impl SkillSandboxServeConfig {
    pub fn from_env_or_default() -> Result<Self, SkillRuntimeError> {
        let listen_addr = std::env::var("MAF_SKILL_SANDBOX_LISTEN_ADDR")
            .unwrap_or_else(|_| DEFAULT_SKILL_SANDBOX_LISTEN_ADDR.to_owned());
        let mut config = Self::from_listen_addr(&listen_addr)?;
        if let Ok(sandbox_root) = std::env::var("MAF_SKILL_SANDBOX_ROOT") {
            config = config.with_sandbox_root(sandbox_root)?;
        }
        Ok(config)
    }

    pub fn from_listen_addr(listen_addr: &str) -> Result<Self, SkillRuntimeError> {
        let listen_addr = listen_addr.parse::<SocketAddr>().map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "skill sandbox listen address is invalid",
            )
        })?;
        Self::from_socket_addr(listen_addr)
    }

    pub fn from_socket_addr(listen_addr: SocketAddr) -> Result<Self, SkillRuntimeError> {
        if !listen_addr.ip().is_loopback() {
            let mut error = SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "skill sandbox listener must bind loopback until mTLS endpoint support is implemented",
            );
            error
                .safe_metadata
                .insert("listen_addr".to_owned(), listen_addr.to_string());
            return Err(error);
        }
        Ok(Self {
            listen_addr,
            sandbox_root: None,
        })
    }

    pub fn with_sandbox_root(
        mut self,
        sandbox_root: impl AsRef<Path>,
    ) -> Result<Self, SkillRuntimeError> {
        let sandbox_root = sandbox_root.as_ref();
        if sandbox_root.as_os_str().is_empty() {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "skill sandbox root must be non-empty",
            ));
        }
        self.sandbox_root = Some(sandbox_root.to_path_buf());
        Ok(self)
    }

    #[must_use]
    pub fn build_service(&self) -> SkillSandboxGrpcService {
        match self.sandbox_root.as_ref() {
            Some(sandbox_root) => SkillSandboxGrpcService::with_process_manager(
                SandboxProcessManager::new(sandbox_root),
            ),
            None => SkillSandboxGrpcService::new(),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct SkillSandboxGrpcService {
    inner: std::sync::Arc<std::sync::Mutex<SkillSandboxService>>,
}

impl SkillSandboxGrpcService {
    #[must_use]
    pub fn new() -> Self {
        Self {
            inner: std::sync::Arc::new(std::sync::Mutex::new(SkillSandboxService::new())),
        }
    }

    #[must_use]
    pub fn with_process_manager(process_manager: SandboxProcessManager) -> Self {
        Self {
            inner: std::sync::Arc::new(std::sync::Mutex::new(
                SkillSandboxService::with_process_manager(process_manager),
            )),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, SkillSandboxService>, tonic::Status> {
        self.inner
            .lock()
            .map_err(|_| tonic::Status::internal("skill sandbox service lock is poisoned"))
    }
}

pub async fn serve_skill_sandbox(
    config: SkillSandboxServeConfig,
) -> Result<(), Box<dyn StdError + Send + Sync>> {
    serve_skill_sandbox_service(config.listen_addr, config.build_service()).await?;
    Ok(())
}

pub async fn serve_skill_sandbox_service(
    listen_addr: SocketAddr,
    service: SkillSandboxGrpcService,
) -> Result<(), tonic::transport::Error> {
    tonic::transport::Server::builder()
        .add_service(skill_pb::skill_sandbox_server::SkillSandboxServer::new(
            service,
        ))
        .serve(listen_addr)
        .await
}

pub async fn serve_skill_sandbox_with_shutdown<F>(
    listen_addr: SocketAddr,
    service: SkillSandboxGrpcService,
    shutdown: F,
) -> Result<(), tonic::transport::Error>
where
    F: Future<Output = ()>,
{
    tonic::transport::Server::builder()
        .add_service(skill_pb::skill_sandbox_server::SkillSandboxServer::new(
            service,
        ))
        .serve_with_shutdown(listen_addr, shutdown)
        .await
}

#[tonic::async_trait]
impl skill_pb::skill_sandbox_server::SkillSandbox for SkillSandboxGrpcService {
    async fn health(
        &self,
        _request: tonic::Request<skill_pb::HealthRequest>,
    ) -> Result<tonic::Response<skill_pb::HealthResponse>, tonic::Status> {
        let status = self.lock()?.health();
        Ok(tonic::Response::new(skill_pb::HealthResponse {
            state: health_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            error: None,
        }))
    }

    async fn readiness(
        &self,
        _request: tonic::Request<skill_pb::ReadinessRequest>,
    ) -> Result<tonic::Response<skill_pb::ReadinessResponse>, tonic::Status> {
        let status = self.lock()?.readiness();
        Ok(tonic::Response::new(skill_pb::ReadinessResponse {
            state: readiness_state_to_pb(status.state) as i32,
            version: Some(version_to_pb(status.version)),
            compatibility_handshake_passed: status.compatibility_handshake_passed,
            error: None,
        }))
    }

    async fn version(
        &self,
        _request: tonic::Request<skill_pb::VersionRequest>,
    ) -> Result<tonic::Response<skill_pb::VersionResponse>, tonic::Status> {
        let version = self.lock()?.version();
        Ok(tonic::Response::new(skill_pb::VersionResponse {
            version: Some(version_to_pb(version)),
        }))
    }

    async fn check_compatibility(
        &self,
        request: tonic::Request<skill_pb::CompatibilityCheckRequest>,
    ) -> Result<tonic::Response<skill_pb::CompatibilityCheckResponse>, tonic::Status> {
        let request = request.into_inner();
        let mut service = self.lock()?;
        let version = service.version();
        let result = service.accept_compatibility_handshake(CompatibilityCheck {
            client_version: request.client_version,
            expected_component: request.expected_component,
            expected_protocol_version: request.expected_protocol_version,
            expected_schema_hash: request.expected_schema_hash,
            expected_error_code_table_hash: request.expected_error_code_table_hash,
            required_features: request.required_features,
        });
        match result {
            Ok(result) => Ok(tonic::Response::new(skill_pb::CompatibilityCheckResponse {
                compatible: result.compatible,
                version: Some(version_to_pb(version)),
                missing_features: result.missing_features,
                error: None,
            })),
            Err(error) => Ok(tonic::Response::new(skill_pb::CompatibilityCheckResponse {
                compatible: false,
                version: Some(version_to_pb(version)),
                missing_features: missing_features_from_error(&error),
                error: Some(typed_error_to_pb(TypedErrorEnvelope::from(error))),
            })),
        }
    }

    async fn validate_policy(
        &self,
        request: tonic::Request<skill_pb::ValidatePolicyRequest>,
    ) -> Result<tonic::Response<skill_pb::ValidatePolicyResponse>, tonic::Status> {
        let request = request.into_inner();
        let x_runtime_rust = request
            .x_runtime_rust
            .into_iter()
            .collect::<BTreeMap<_, _>>();
        let input = SkillPolicyInput {
            skill_name: request.skill_name,
            capability_id: request.capability_id,
            execution_mode: request.execution_mode,
            trust_scope: request.trust_scope,
            handler: request.handler,
            requested_services: if request.requested_services.is_empty() {
                request.manifest_services.clone()
            } else {
                request.requested_services
            },
            manifest_services: request.manifest_services,
            runtime_allowlist_services: request.runtime_allowlist_services,
            runtime_allowlist_handlers: request.runtime_allowlist_handlers,
            x_runtime_rust,
        };
        let response = self.lock()?.validate_policy(input);
        Ok(tonic::Response::new(skill_pb::ValidatePolicyResponse {
            allowed: response.allowed,
            bundle_fingerprint: response.bundle_fingerprint,
            error: response.error.map(typed_error_to_pb),
        }))
    }

    async fn execute_sandboxed(
        &self,
        request: tonic::Request<skill_pb::ExecuteSandboxedRequest>,
    ) -> Result<tonic::Response<skill_pb::ExecuteSandboxedResponse>, tonic::Status> {
        let request = request.into_inner();
        let response = self.lock()?.execute_sandboxed(ExecuteSandboxedRequest {
            skill_name: request.skill_name,
            execution_mode: request.execution_mode,
            cwd_under_public_root: request.cwd_under_public_root,
            argv: request.argv,
            timeout_ms: request.timeout_ms,
            stdout_limit_bytes: request.stdout_limit_bytes,
            stderr_limit_bytes: request.stderr_limit_bytes,
            stdin_payload: request.stdin_payload,
        });
        Ok(tonic::Response::new(skill_pb::ExecuteSandboxedResponse {
            exit_code: response.exit_code,
            stdout_prefix: response.stdout_prefix,
            stderr_prefix: response.stderr_prefix,
            stdout_truncated: response.stdout_truncated,
            stderr_truncated: response.stderr_truncated,
            error: response.error.map(typed_error_to_pb),
        }))
    }
}

#[must_use]
pub fn allowed_execution_modes() -> Vec<String> {
    [
        "delegated_main_agent",
        "platform_service",
        "python_subprocess",
    ]
    .iter()
    .map(|value| (*value).to_owned())
    .collect()
}

#[must_use]
pub fn default_execution_modes() -> BTreeMap<String, String> {
    BTreeMap::from([
        (
            "instruction_only".to_owned(),
            "delegated_main_agent".to_owned(),
        ),
        ("scripted".to_owned(), "python_subprocess".to_owned()),
    ])
}

#[must_use]
pub fn allowed_answer_modes() -> Vec<String> {
    ["direct", "requires_finalizer", "none"]
        .iter()
        .map(|value| (*value).to_owned())
        .collect()
}

#[must_use]
pub fn default_answer_mode_by_execution_mode() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("delegated_main_agent".to_owned(), "direct".to_owned()),
        (
            "python_subprocess".to_owned(),
            "requires_finalizer".to_owned(),
        ),
    ])
}

#[must_use]
pub fn answer_mode_required_execution_modes() -> Vec<String> {
    ["platform_service"]
        .iter()
        .map(|value| (*value).to_owned())
        .collect()
}

#[must_use]
pub fn allowed_rust_adapters() -> Vec<String> {
    ["pyo3", "binary", "sidecar"]
        .iter()
        .map(|value| (*value).to_owned())
        .collect()
}

#[must_use]
pub fn forbidden_x_runtime_rust_keys() -> Vec<String> {
    [
        "secret",
        "endpoint",
        "socket_path",
        "mtls_key",
        "download_url",
        "local_path",
    ]
    .iter()
    .map(|value| (*value).to_owned())
    .collect()
}

pub fn guard_public_root_path(path: &str) -> Result<(), SkillRuntimeError> {
    if path.starts_with('/') || path.contains("..") || path.contains('~') || path.contains('\\') {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::PublicRootEscape,
            "path escapes skill public root",
        ));
    }
    Ok(())
}

pub fn validate_service_binding(input: &SkillPolicyInput) -> Result<(), SkillRuntimeError> {
    let manifest = input.manifest_services.iter().collect::<BTreeSet<_>>();
    let runtime = input
        .runtime_allowlist_services
        .iter()
        .collect::<BTreeSet<_>>();
    for service in &input.requested_services {
        if !manifest.contains(service) || !runtime.contains(service) {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::ServiceNotAllowlisted,
                "service binding requires manifest declaration and runtime allowlist",
            ));
        }
    }
    Ok(())
}

pub fn validate_handler_allowlist(input: &SkillPolicyInput) -> Result<(), SkillRuntimeError> {
    if input.handler.is_empty() || !input.runtime_allowlist_handlers.contains(&input.handler) {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::HandlerNotAllowlisted,
            "skill handler is not allowlisted by runtime",
        ));
    }
    Ok(())
}

pub fn validate_rust_metadata(
    metadata: &BTreeMap<String, String>,
) -> Result<(), SkillRuntimeError> {
    for forbidden in forbidden_x_runtime_rust_keys() {
        if metadata.contains_key(&forbidden) {
            return Err(SkillRuntimeError::new(
                SkillRuntimeErrorCode::RustAdapterInvalid,
                "x_runtime.rust contains forbidden sensitive or authority-bearing key",
            ));
        }
    }
    if let Some(adapter) = metadata.get("adapter")
        && !allowed_rust_adapters().contains(adapter)
    {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::RustAdapterInvalid,
            "x_runtime.rust adapter is not supported",
        ));
    }
    Ok(())
}

#[must_use]
pub fn bundle_fingerprint(input: &SkillPolicyInput) -> String {
    let serialized = serde_json::to_vec(input)
        .expect("SkillPolicyInput serializes deterministically enough for hashing");
    let digest = Sha256::digest(serialized);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn validate_policy(input: &SkillPolicyInput) -> Result<SkillPolicyDecision, SkillRuntimeError> {
    if !allowed_execution_modes().contains(&input.execution_mode) {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::ManifestInvalid,
            "execution mode is not supported by generic skill runtime",
        ));
    }
    validate_handler_allowlist(input)?;
    validate_service_binding(input)?;
    validate_rust_metadata(&input.x_runtime_rust)?;
    Ok(SkillPolicyDecision {
        allowed: true,
        bundle_fingerprint: bundle_fingerprint(input),
    })
}

fn validate_client_version(client_version: &str) -> Result<(), SkillRuntimeError> {
    let mut parts = client_version.split('.');
    let major = parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .ok_or_else(|| unsupported_client_version_error(client_version))?;
    let minor = parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .ok_or_else(|| unsupported_client_version_error(client_version))?;
    parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .ok_or_else(|| unsupported_client_version_error(client_version))?;
    if parts.next().is_some() || major != 0 || minor != 1 {
        return Err(unsupported_client_version_error(client_version));
    }
    Ok(())
}

fn unsupported_client_version_error(client_version: &str) -> SkillRuntimeError {
    let mut error = SkillRuntimeError::new(
        SkillRuntimeErrorCode::ContractMismatch,
        "skill sandbox client version is outside the supported range",
    );
    error
        .safe_metadata
        .insert("client_version".to_owned(), client_version.to_owned());
    error.safe_metadata.insert(
        "min_client_version".to_owned(),
        MIN_CLIENT_VERSION.to_owned(),
    );
    error.safe_metadata.insert(
        "max_client_version".to_owned(),
        MAX_CLIENT_VERSION.to_owned(),
    );
    error
}

#[must_use]
pub fn artifact_provenance_policy() -> ArtifactProvenancePolicy {
    ArtifactProvenancePolicy {
        allowed_sources: ["ci_pipeline", "deployment_pipeline", "runtime_allowlist"]
            .iter()
            .map(|source| (*source).to_owned())
            .collect(),
        allowed_artifact_kinds: [
            "skill_policy_pyo3_wheel",
            "skill_sandbox_sidecar_binary",
            "skill_owned_rust_artifact",
        ]
        .iter()
        .map(|kind| (*kind).to_owned())
        .collect(),
        required_fields: [
            "source",
            "artifact_kind",
            "checksum_sha256",
            "cargo_lock_digest",
            "contract_version",
            "bundle_revision",
            "schema_hash",
            "sbom_digest",
            "provenance_attestation",
        ]
        .iter()
        .map(|field| (*field).to_owned())
        .collect(),
        require_contract_version_match: true,
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
        required_baselines: ["python_legacy", "rust_skill_runtime"]
            .iter()
            .map(|baseline| (*baseline).to_owned())
            .collect(),
        required_operations: [
            "manifest_parse",
            "bundle_fingerprint",
            "allowlist_decision",
            "sandbox_execution",
            "stdout_stderr_handling",
            "artifact_handoff",
            "process_cleanup",
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
        allowed_scopes: ["skill_policy", "skill_sandbox"]
            .iter()
            .map(|scope| (*scope).to_owned())
            .collect(),
        required_evidence: [
            "artifact_provenance",
            "benchmark_report",
            "audit_redaction",
            "rollback_runbook",
            "ops_runbook",
            "regression_tests",
            "cargo_tests",
            "cargo_fmt",
            "cargo_clippy",
            "secret_leak_tests",
            "process_cleanup_drill",
        ]
        .iter()
        .map(|evidence| (*evidence).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn ops_policy() -> OpsPolicy {
    OpsPolicy {
        required_observability: ["dashboard", "alerts", "slo"]
            .iter()
            .map(|item| (*item).to_owned())
            .collect(),
        required_runbooks: [
            "drain",
            "restart",
            "rollback",
            "artifact_quarantine",
            "secret_rotation",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
        required_drills: [
            "sandbox_unavailable",
            "queue_full",
            "timeout",
            "process_cleanup_failure",
            "artifact_handoff_failure",
            "secret_identity_mismatch",
            "public_endpoint_denial",
        ]
        .iter()
        .map(|item| (*item).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn decommission_policy() -> DecommissionPolicy {
    DecommissionPolicy {
        required_removed_legacy_paths: ["python_trust_gate", "python_subprocess_sandbox_policy"]
            .iter()
            .map(|path| (*path).to_owned())
            .collect(),
        required_facade_only_paths: ["python_facade", "platform_handler_adapter"]
            .iter()
            .map(|path| (*path).to_owned())
            .collect(),
        required_evidence: [
            "promotion_threshold_passed",
            "ops_readiness_passed",
            "benchmark_passed",
            "architecture_guard",
            "regression_tests",
            "decommission_grep",
        ]
        .iter()
        .map(|evidence| (*evidence).to_owned())
        .collect(),
        allowed_rollback_paths: [
            "deployment_rollback",
            "artifact_rollback",
            "sandbox_restore",
        ]
        .iter()
        .map(|path| (*path).to_owned())
        .collect(),
    }
}

#[must_use]
pub fn error_code_table() -> Vec<ErrorCodeEntry> {
    [
        SkillRuntimeErrorCode::ManifestInvalid,
        SkillRuntimeErrorCode::PublicRootEscape,
        SkillRuntimeErrorCode::ServiceNotAllowlisted,
        SkillRuntimeErrorCode::HandlerNotAllowlisted,
        SkillRuntimeErrorCode::RustAdapterInvalid,
        SkillRuntimeErrorCode::SandboxPolicyDenied,
        SkillRuntimeErrorCode::SandboxTimeout,
        SkillRuntimeErrorCode::OutputTooLarge,
        SkillRuntimeErrorCode::ContractMismatch,
        SkillRuntimeErrorCode::ArtifactUntrusted,
        SkillRuntimeErrorCode::BenchmarkInvalid,
        SkillRuntimeErrorCode::PromotionBlocked,
        SkillRuntimeErrorCode::OpsReadinessBlocked,
        SkillRuntimeErrorCode::DecommissionBlocked,
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
pub fn skill_runtime_contract_artifact() -> SkillRuntimeContractArtifact {
    SkillRuntimeContractArtifact {
        component: COMPONENT_ID.to_owned(),
        protocol_version: PROTOCOL_VERSION.to_owned(),
        contract_version: CONTRACT_VERSION.to_owned(),
        client_version: MIN_CLIENT_VERSION.to_owned(),
        min_client_version: MIN_CLIENT_VERSION.to_owned(),
        max_client_version: MAX_CLIENT_VERSION.to_owned(),
        schema_hash: SCHEMA_HASH.to_owned(),
        error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        mode_env: "MAF_RUST_SKILL_RUNTIME_MODE".to_owned(),
        modes: vec!["off".to_owned(), "shadow".to_owned(), "enforce".to_owned()],
        supported_features: vec![
            "policy_kernel".to_owned(),
            "pyo3_policy_facade".to_owned(),
            "sandbox_sidecar".to_owned(),
            "skill_owned_rust_metadata".to_owned(),
        ],
        allowed_execution_modes: allowed_execution_modes(),
        default_execution_modes: default_execution_modes(),
        allowed_answer_modes: allowed_answer_modes(),
        default_answer_mode_by_execution_mode: default_answer_mode_by_execution_mode(),
        answer_mode_required_execution_modes: answer_mode_required_execution_modes(),
        allowed_rust_adapters: allowed_rust_adapters(),
        forbidden_x_runtime_rust_keys: forbidden_x_runtime_rust_keys(),
        sandbox_limits: BTreeMap::from([
            ("max_concurrent_executions".to_owned(), 4),
            ("per_skill_concurrent".to_owned(), 2),
            ("queue_size".to_owned(), 64),
            ("queue_wait_ms".to_owned(), 10_000),
            ("default_timeout_ms".to_owned(), 60_000),
            ("hard_timeout_ms".to_owned(), 300_000),
            ("stdin_bytes".to_owned(), 1024 * 1024),
            ("stdout_bytes".to_owned(), 1024 * 1024),
            ("stderr_bytes".to_owned(), 1024 * 1024),
            ("structured_result_bytes".to_owned(), 4 * 1024 * 1024),
            ("artifact_bytes".to_owned(), 32 * 1024 * 1024),
        ]),
        artifact_policy: artifact_provenance_policy(),
        benchmark_policy: benchmark_policy(),
        promotion_policy: promotion_policy(),
        ops_policy: ops_policy(),
        decommission_policy: decommission_policy(),
        error_codes: error_code_table(),
    }
}

pub fn skill_runtime_contract_json() -> Result<String, serde_json::Error> {
    let mut json = serde_json::to_string_pretty(&skill_runtime_contract_artifact())?;
    json.push('\n');
    Ok(json)
}

#[must_use]
pub fn skill_policy_validate_json(payload: &str) -> String {
    let response = match serde_json::from_str::<SkillPolicyInput>(payload) {
        Ok(input) => validate_policy(&input)
            .map(|decision| ValidatePolicyResponse {
                allowed: decision.allowed,
                bundle_fingerprint: decision.bundle_fingerprint,
                error: None,
            })
            .unwrap_or_else(policy_denied_response),
        Err(_) => policy_denied_response(SkillRuntimeError::new(
            SkillRuntimeErrorCode::ContractMismatch,
            "Skill Runtime PyO3 policy input is not compatible with Rust contract",
        )),
    };
    serde_json::to_string(&response).expect("ValidatePolicyResponse serializes to JSON")
}

fn policy_denied_response(error: SkillRuntimeError) -> ValidatePolicyResponse {
    ValidatePolicyResponse {
        allowed: false,
        bundle_fingerprint: String::new(),
        error: Some(TypedErrorEnvelope::from(error)),
    }
}

fn sandbox_error_response(error: SkillRuntimeError) -> ExecuteSandboxedResponse {
    ExecuteSandboxedResponse {
        exit_code: -1,
        stdout_prefix: Vec::new(),
        stderr_prefix: Vec::new(),
        stdout_truncated: false,
        stderr_truncated: false,
        error: Some(TypedErrorEnvelope::from(error)),
    }
}

fn checked_join_under_root(root: &Path, relative: &str) -> Result<PathBuf, SkillRuntimeError> {
    let joined = root.join(relative);
    let canonical = joined.canonicalize().map_err(|_| {
        SkillRuntimeError::new(
            SkillRuntimeErrorCode::PublicRootEscape,
            "sandbox path is not accessible under public root",
        )
    })?;
    if !canonical.starts_with(root) {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::PublicRootEscape,
            "sandbox path escapes public root",
        ));
    }
    Ok(canonical)
}

fn bounded_timeout(timeout_ms: u32) -> Result<Duration, SkillRuntimeError> {
    let hard_timeout_ms = sandbox_limit("hard_timeout_ms")?;
    let requested = u64::from(timeout_ms);
    if requested == 0 || requested > hard_timeout_ms {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "sandbox timeout is outside the allowed range",
        ));
    }
    Ok(Duration::from_millis(requested))
}

fn validate_stdin_limit(stdin_bytes: usize) -> Result<(), SkillRuntimeError> {
    let max_stdin_bytes = sandbox_limit("stdin_bytes")?;
    if stdin_bytes as u64 > max_stdin_bytes {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::OutputTooLarge,
            "sandbox stdin exceeded configured limit",
        ));
    }
    Ok(())
}

fn requested_stream_limit(name: &str, requested: u32) -> Result<usize, SkillRuntimeError> {
    let hard_limit = sandbox_limit(name)?;
    if u64::from(requested) > hard_limit {
        return Err(SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "sandbox output limit request exceeds runtime hard cap",
        ));
    }
    Ok(requested as usize)
}

fn sandbox_limit(name: &str) -> Result<u64, SkillRuntimeError> {
    skill_runtime_contract_artifact()
        .sandbox_limits
        .get(name)
        .copied()
        .ok_or_else(|| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::ContractMismatch,
                format!("sandbox contract limit {name} is missing"),
            )
        })
}

fn stdio_drain_deadline() -> Instant {
    Instant::now() + Duration::from_millis(STDIO_DRAIN_GRACE_MS)
}

fn bounded_output_response(
    exit_code: i32,
    stdout_prefix: Vec<u8>,
    stdout_truncated: bool,
    stderr_prefix: Vec<u8>,
    stderr_truncated: bool,
) -> ExecuteSandboxedResponse {
    let error = if stdout_truncated || stderr_truncated {
        Some(TypedErrorEnvelope::from(SkillRuntimeError::new(
            SkillRuntimeErrorCode::OutputTooLarge,
            "sandbox stdout or stderr exceeded configured limit",
        )))
    } else {
        None
    };
    ExecuteSandboxedResponse {
        exit_code,
        stdout_prefix,
        stderr_prefix,
        stdout_truncated,
        stderr_truncated,
        error,
    }
}

fn spawn_stdin_writer(
    mut stdin: std::process::ChildStdin,
    payload: Vec<u8>,
) -> mpsc::Receiver<Result<(), SkillRuntimeError>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let result = stdin.write_all(&payload).map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox stdin write failed",
            )
        });
        let _ = sender.send(result);
    });
    receiver
}

#[derive(Debug)]
struct LimitedReaderHandle {
    state: Arc<Mutex<LimitedReaderState>>,
    done: mpsc::Receiver<()>,
}

#[derive(Debug, Clone)]
struct LimitedReaderState {
    prefix: Vec<u8>,
    truncated: bool,
    error: Option<SkillRuntimeError>,
    done: bool,
}

fn spawn_limited_reader<R>(
    mut pipe: R,
    limit: usize,
    stream_name: &'static str,
) -> LimitedReaderHandle
where
    R: Read + Send + 'static,
{
    let (sender, receiver) = mpsc::channel();
    let state = Arc::new(Mutex::new(LimitedReaderState {
        prefix: Vec::with_capacity(limit.min(8192)),
        truncated: false,
        error: None,
        done: false,
    }));
    let state_for_thread = Arc::clone(&state);
    thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        if let Err(error) = read_limited_prefix(
            &mut pipe,
            &state_for_thread,
            &mut buffer,
            limit,
            stream_name,
        ) && let Ok(mut state) = state_for_thread.lock()
        {
            state.error = Some(error);
        }
        if let Ok(mut state) = state_for_thread.lock() {
            state.done = true;
        }
        let _ = sender.send(());
    });
    LimitedReaderHandle {
        state,
        done: receiver,
    }
}

fn receive_stdin_writer(
    receiver: Option<mpsc::Receiver<Result<(), SkillRuntimeError>>>,
    deadline: Instant,
) -> Result<(), SkillRuntimeError> {
    match receiver {
        Some(receiver) => receive_before_deadline(receiver, deadline),
        None => Ok(()),
    }
}

fn receive_limited_reader(
    receiver: Option<LimitedReaderHandle>,
    deadline: Instant,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    match receiver {
        Some(receiver) => receive_reader_before_deadline(receiver, deadline),
        None => Ok((Vec::new(), false)),
    }
}

fn receive_reader_before_deadline(
    receiver: LimitedReaderHandle,
    deadline: Instant,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    let now = Instant::now();
    let timeout = deadline.saturating_duration_since(now);
    let _ = receiver.done.recv_timeout(timeout);
    snapshot_limited_reader(&receiver)
}

fn snapshot_limited_reader(
    receiver: &LimitedReaderHandle,
) -> Result<(Vec<u8>, bool), SkillRuntimeError> {
    let state = receiver.state.lock().map_err(|_| {
        SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxPolicyDenied,
            "sandbox stdio reader state is poisoned",
        )
    })?;
    if let Some(error) = state.error.clone() {
        return Err(error);
    }
    Ok((state.prefix.clone(), state.truncated))
}

fn receive_before_deadline<T>(
    receiver: mpsc::Receiver<Result<T, SkillRuntimeError>>,
    deadline: Instant,
) -> Result<T, SkillRuntimeError> {
    let now = Instant::now();
    let timeout = deadline.saturating_duration_since(now);
    receiver.recv_timeout(timeout).map_err(|_| {
        SkillRuntimeError::new(
            SkillRuntimeErrorCode::SandboxTimeout,
            "sandbox stdio did not close before deadline",
        )
    })?
}

fn read_limited_prefix<R>(
    pipe: &mut R,
    state: &Arc<Mutex<LimitedReaderState>>,
    buffer: &mut [u8; 8192],
    limit: usize,
    stream_name: &str,
) -> Result<(), SkillRuntimeError>
where
    R: Read,
{
    loop {
        let read = pipe.read(buffer).map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                format!("sandbox {stream_name} collection failed"),
            )
        })?;
        if read == 0 {
            return Ok(());
        }

        let mut state = state.lock().map_err(|_| {
            SkillRuntimeError::new(
                SkillRuntimeErrorCode::SandboxPolicyDenied,
                "sandbox stdio reader state is poisoned",
            )
        })?;
        let remaining = limit.saturating_sub(state.prefix.len());
        if remaining > 0 {
            let to_copy = remaining.min(read);
            state.prefix.extend_from_slice(&buffer[..to_copy]);
        }
        if read > remaining {
            state.truncated = true;
        }
    }
}

fn configure_sandbox_environment(command: &mut Command) {
    command.env_clear();
    command.env("PATH", SANDBOX_ENV_PATH);
    command.env("LC_ALL", "C");
}

fn configure_child_process_group(command: &mut Command) {
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if setpgid(0, 0) == 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        });
    }
}

fn kill_child_process_group(child_id: u32) {
    #[cfg(unix)]
    unsafe {
        let pgid = -(child_id as i32);
        let _ = kill(pgid, SIGKILL);
    }
}

#[cfg(unix)]
const SIGKILL: i32 = 9;

#[cfg(unix)]
unsafe extern "C" {
    fn setpgid(pid: i32, pgid: i32) -> i32;
    fn kill(pid: i32, sig: i32) -> i32;
}

fn version_to_pb(version: SkillRuntimeVersion) -> common_pb::VersionInfo {
    common_pb::VersionInfo {
        component: version.component,
        build_version: env!("CARGO_PKG_VERSION").to_owned(),
        protocol_version: version.protocol_version,
        schema_hash: version.schema_hash,
        error_code_table_hash: version.error_code_table_hash,
        supported_features: version.supported_features,
        min_client_version: MIN_CLIENT_VERSION.to_owned(),
        max_client_version: MAX_CLIENT_VERSION.to_owned(),
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

fn missing_features_from_error(error: &SkillRuntimeError) -> Vec<String> {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io;
    use std::path::PathBuf;
    use std::sync::mpsc;

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    fn policy_input() -> SkillPolicyInput {
        SkillPolicyInput {
            skill_name: "example".to_owned(),
            capability_id: "skill.example".to_owned(),
            execution_mode: "platform_service".to_owned(),
            trust_scope: "project".to_owned(),
            handler: "skill.example.platform_handler".to_owned(),
            manifest_services: vec!["llm.generate".to_owned()],
            runtime_allowlist_services: vec!["llm.generate".to_owned()],
            requested_services: vec!["llm.generate".to_owned()],
            runtime_allowlist_handlers: vec!["skill.example.platform_handler".to_owned()],
            x_runtime_rust: BTreeMap::from([
                ("adapter".to_owned(), "pyo3".to_owned()),
                ("contract_version".to_owned(), "1".to_owned()),
            ]),
        }
    }

    #[test]
    fn service_binding_requires_manifest_and_runtime_allowlist() {
        let mut input = policy_input();
        assert!(validate_policy(&input).expect("policy").allowed);
        input.runtime_allowlist_services.clear();
        let err = validate_policy(&input).expect_err("missing runtime allowlist must fail");
        assert_eq!(err.code, "skill_runtime_service_not_allowlisted");
    }

    #[test]
    fn rust_metadata_cannot_carry_endpoint_or_secret_authority() {
        let mut metadata = BTreeMap::from([("adapter".to_owned(), "sidecar".to_owned())]);
        metadata.insert("endpoint".to_owned(), "http://127.0.0.1:9000".to_owned());
        let err = validate_rust_metadata(&metadata).expect_err("endpoint must be forbidden");
        assert_eq!(err.code, "skill_runtime_rust_adapter_invalid");
    }

    #[test]
    fn public_root_guard_rejects_escape_paths() {
        assert!(guard_public_root_path("runtime/handler.py").is_ok());
        assert!(guard_public_root_path("../secrets.txt").is_err());
        assert!(guard_public_root_path("/tmp/secret").is_err());
    }

    #[test]
    fn execution_defaults_are_rust_owned() {
        let artifact = skill_runtime_contract_artifact();
        assert_eq!(
            artifact
                .default_execution_modes
                .get("instruction_only")
                .map(String::as_str),
            Some("delegated_main_agent")
        );
        assert_eq!(
            artifact
                .default_execution_modes
                .get("scripted")
                .map(String::as_str),
            Some("python_subprocess")
        );
        assert_eq!(
            artifact
                .default_answer_mode_by_execution_mode
                .get("delegated_main_agent")
                .map(String::as_str),
            Some("direct")
        );
        assert_eq!(
            artifact.answer_mode_required_execution_modes,
            vec!["platform_service".to_owned()]
        );
    }

    #[test]
    fn checked_in_contract_artifact_matches_rust_canonical_export() {
        let artifact = fs::read_to_string(
            repo_root()
                .join("src/integrations/agent_skills/rust_contracts/skill_runtime_contract.json"),
        )
        .expect("checked-in skill runtime contract artifact must exist");
        assert_eq!(
            artifact,
            skill_runtime_contract_json().expect("serialize skill runtime contract"),
        );
    }

    #[test]
    fn skill_policy_json_bridge_uses_rust_policy_kernel() {
        let input = policy_input();
        let payload = serde_json::to_string(&input).expect("serialize input");
        let response: ValidatePolicyResponse =
            serde_json::from_str(&skill_policy_validate_json(&payload)).expect("parse response");

        assert!(response.allowed);
        assert!(response.error.is_none());
        assert_eq!(response.bundle_fingerprint, bundle_fingerprint(&input));
    }

    #[test]
    fn skill_policy_json_bridge_fails_closed_on_bad_payload() {
        let response: ValidatePolicyResponse =
            serde_json::from_str(&skill_policy_validate_json("{not-json")).expect("parse response");

        assert!(!response.allowed);
        assert_eq!(
            response.error.expect("typed error").code,
            "skill_runtime_contract_mismatch"
        );
    }

    #[test]
    fn compatibility_errors_preserve_safe_metadata() {
        let service = SkillSandboxService::new();
        let version = service.version();

        let mismatch = service
            .check_compatibility(CompatibilityCheck {
                client_version: MIN_CLIENT_VERSION.to_owned(),
                expected_component: "wrong_component".to_owned(),
                expected_protocol_version: version.protocol_version.clone(),
                expected_schema_hash: version.schema_hash.clone(),
                expected_error_code_table_hash: version.error_code_table_hash.clone(),
                required_features: Vec::new(),
            })
            .expect_err("component mismatch must fail closed");
        assert_eq!(
            mismatch.code,
            SkillRuntimeErrorCode::ContractMismatch.as_str()
        );

        let missing = service
            .check_compatibility(CompatibilityCheck {
                client_version: MIN_CLIENT_VERSION.to_owned(),
                expected_component: version.component,
                expected_protocol_version: version.protocol_version,
                expected_schema_hash: version.schema_hash,
                expected_error_code_table_hash: version.error_code_table_hash,
                required_features: vec!["sandbox_sidecar".to_owned(), "future_feature".to_owned()],
            })
            .expect_err("unknown required feature must fail closed");
        assert_eq!(
            missing.safe_metadata.get("missing_features"),
            Some(&"future_feature".to_owned())
        );
        assert_eq!(
            missing_features_from_error(&missing),
            vec!["future_feature".to_owned()]
        );
    }

    #[test]
    fn validation_rejects_unknown_modes_and_rust_adapters() {
        let mut input = policy_input();
        input.execution_mode = "shell".to_owned();
        let mode_error = validate_policy(&input).expect_err("unknown mode must fail closed");
        assert_eq!(
            mode_error.code,
            SkillRuntimeErrorCode::ManifestInvalid.as_str()
        );

        let metadata = BTreeMap::from([("adapter".to_owned(), "python_import".to_owned())]);
        let adapter_error =
            validate_rust_metadata(&metadata).expect_err("unknown adapter must fail closed");
        assert_eq!(
            adapter_error.code,
            SkillRuntimeErrorCode::RustAdapterInvalid.as_str()
        );
    }

    #[test]
    fn sandbox_config_and_service_preflight_errors_are_fail_closed() {
        let invalid_addr =
            SkillSandboxServeConfig::from_listen_addr("not-a-socket").expect_err("invalid addr");
        assert_eq!(
            invalid_addr.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );

        let empty_root =
            SkillSandboxServeConfig::from_listen_addr(DEFAULT_SKILL_SANDBOX_LISTEN_ADDR)
                .expect("loopback")
                .with_sandbox_root("")
                .expect_err("empty sandbox root must fail closed");
        assert_eq!(
            empty_root.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );

        let service = SkillSandboxService::new();
        let escape = service.execute_sandboxed(ExecuteSandboxedRequest {
            skill_name: "example".to_owned(),
            execution_mode: "python_subprocess".to_owned(),
            cwd_under_public_root: "../escape".to_owned(),
            argv: vec!["./handler.sh".to_owned()],
            timeout_ms: 1_000,
            stdout_limit_bytes: 1024,
            stderr_limit_bytes: 1024,
            stdin_payload: Vec::new(),
        });
        assert_eq!(
            escape.error.expect("escape error").code,
            SkillRuntimeErrorCode::PublicRootEscape.as_str()
        );

        let invalid_mode = service.execute_sandboxed(ExecuteSandboxedRequest {
            skill_name: "example".to_owned(),
            execution_mode: "shell".to_owned(),
            cwd_under_public_root: ".".to_owned(),
            argv: vec!["./handler.sh".to_owned()],
            timeout_ms: 1_000,
            stdout_limit_bytes: 1024,
            stderr_limit_bytes: 1024,
            stdin_payload: Vec::new(),
        });
        assert_eq!(
            invalid_mode.error.expect("mode error").code,
            SkillRuntimeErrorCode::ManifestInvalid.as_str()
        );
    }

    #[test]
    fn sandbox_private_helpers_report_typed_errors() {
        let missing_root = std::env::temp_dir().join("maf-skill-runtime-missing-root-for-coverage");
        let missing_path =
            checked_join_under_root(&missing_root, "missing").expect_err("missing path");
        assert_eq!(
            missing_path.code,
            SkillRuntimeErrorCode::PublicRootEscape.as_str()
        );

        let stream_limit = requested_stream_limit("stdout_bytes", 1024 * 1024 + 1)
            .expect_err("stream limit above hard cap must fail");
        assert_eq!(
            stream_limit.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );

        let missing_limit = sandbox_limit("missing_limit").expect_err("unknown limit");
        assert_eq!(
            missing_limit.code,
            SkillRuntimeErrorCode::ContractMismatch.as_str()
        );

        assert!(receive_stdin_writer(None, Instant::now()).is_ok());
        assert_eq!(
            receive_limited_reader(None, Instant::now()).expect("empty reader"),
            (Vec::new(), false)
        );

        let (_sender, receiver) = mpsc::channel::<Result<(), SkillRuntimeError>>();
        let timeout = receive_before_deadline(receiver, Instant::now())
            .expect_err("missing stdio completion must timeout");
        assert_eq!(timeout.code, SkillRuntimeErrorCode::SandboxTimeout.as_str());

        let (_done_sender, done_receiver) = mpsc::channel();
        let poisoned_reader = LimitedReaderHandle {
            state: Arc::new(Mutex::new(LimitedReaderState {
                prefix: b"partial".to_vec(),
                truncated: true,
                error: Some(SkillRuntimeError::new(
                    SkillRuntimeErrorCode::SandboxPolicyDenied,
                    "reader failed",
                )),
                done: true,
            })),
            done: done_receiver,
        };
        let reader_error =
            snapshot_limited_reader(&poisoned_reader).expect_err("reader error must propagate");
        assert_eq!(
            reader_error.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );
    }

    #[test]
    fn limited_reader_reports_io_errors_and_truncation() {
        struct FailingReader;
        impl Read for FailingReader {
            fn read(&mut self, _buf: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("boom"))
            }
        }

        let failing = spawn_limited_reader(FailingReader, 8, "stdout");
        let error =
            receive_reader_before_deadline(failing, Instant::now() + Duration::from_millis(1_000))
                .expect_err("read errors must be reported");
        assert_eq!(
            error.code,
            SkillRuntimeErrorCode::SandboxPolicyDenied.as_str()
        );

        let state = Arc::new(Mutex::new(LimitedReaderState {
            prefix: Vec::new(),
            truncated: false,
            error: None,
            done: false,
        }));
        let mut buffer = [0_u8; 8192];
        let mut input = io::Cursor::new(b"abcdef".to_vec());
        read_limited_prefix(&mut input, &state, &mut buffer, 3, "stdout").expect("read prefix");
        let state = state.lock().expect("state");
        assert_eq!(state.prefix, b"abc".to_vec());
        assert!(state.truncated);
    }

    #[test]
    fn protobuf_mapping_helpers_cover_all_error_categories() {
        assert_eq!(
            health_state_to_pb(HealthState::Serving),
            common_pb::HealthState::Serving
        );
        assert_eq!(
            health_state_to_pb(HealthState::NotServing),
            common_pb::HealthState::NotServing
        );
        assert_eq!(
            health_state_to_pb(HealthState::Degraded),
            common_pb::HealthState::Degraded
        );
        assert_eq!(
            readiness_state_to_pb(ReadinessState::NotReady),
            common_pb::ReadinessState::NotReady
        );

        for (category, expected) in [
            ("configuration", common_pb::ErrorCategory::Configuration),
            ("compatibility", common_pb::ErrorCategory::Compatibility),
            ("resource_limit", common_pb::ErrorCategory::ResourceLimit),
            ("protocol", common_pb::ErrorCategory::Protocol),
            ("upstream", common_pb::ErrorCategory::Upstream),
            ("cancellation", common_pb::ErrorCategory::Cancellation),
            ("unknown", common_pb::ErrorCategory::Internal),
        ] {
            assert_eq!(error_category_to_pb(category), expected);
        }
    }

    #[test]
    fn artifact_provenance_policy_requires_prebuilt_allowlisted_artifacts() {
        let policy = artifact_provenance_policy();
        assert!(policy.allowed_sources.contains(&"ci_pipeline".to_owned()));
        assert!(
            policy
                .allowed_artifact_kinds
                .contains(&"skill_policy_pyo3_wheel".to_owned())
        );
        assert!(
            policy
                .allowed_artifact_kinds
                .contains(&"skill_sandbox_sidecar_binary".to_owned())
        );
        assert!(
            policy
                .required_fields
                .contains(&"contract_version".to_owned())
        );
        assert!(
            policy
                .required_fields
                .contains(&"bundle_revision".to_owned())
        );
        assert!(policy.require_checksum_allowlist);
        assert!(policy.require_cargo_lock_digest_allowlist);
        assert!(policy.require_contract_version_match);
        assert!(policy.require_schema_hash_match);
        assert!(policy.require_sbom);
        assert!(policy.require_provenance_attestation);
        assert_eq!(
            SkillRuntimeErrorCode::ArtifactUntrusted.category(),
            "security"
        );
    }

    #[test]
    fn benchmark_and_promotion_policies_match_skill_runtime_release_gates() {
        let benchmark = benchmark_policy();
        assert_eq!(
            benchmark.required_baselines,
            vec!["python_legacy".to_owned(), "rust_skill_runtime".to_owned()]
        );
        assert!(
            benchmark
                .required_operations
                .contains(&"sandbox_execution".to_owned())
        );
        assert!(
            benchmark
                .required_operations
                .contains(&"process_cleanup".to_owned())
        );
        assert!(benchmark.required_metrics.contains(&"p95_ms".to_owned()));
        assert!(
            benchmark
                .required_metrics
                .contains(&"queue_wait_ms".to_owned())
        );

        let promotion = promotion_policy();
        assert_eq!(promotion.min_shadow_days, 7);
        assert_eq!(promotion.min_shadow_samples, 1_000);
        assert_eq!(promotion.max_contract_mismatch_rate_ppm, 0);
        assert_eq!(promotion.max_panic_count, 0);
        assert_eq!(promotion.max_crash_count, 0);
        assert_eq!(promotion.max_p95_latency_ratio_percent, 110);
        assert!(promotion.error_rate_must_not_exceed_legacy);
        assert!(
            promotion
                .allowed_scopes
                .contains(&"skill_policy".to_owned())
        );
        assert!(
            promotion
                .allowed_scopes
                .contains(&"skill_sandbox".to_owned())
        );
        assert!(
            promotion
                .required_evidence
                .contains(&"artifact_provenance".to_owned())
        );
        assert!(
            promotion
                .required_evidence
                .contains(&"process_cleanup_drill".to_owned())
        );
        assert_eq!(
            SkillRuntimeErrorCode::PromotionBlocked.category(),
            "quality_gate"
        );
    }

    #[test]
    fn ops_and_decommission_policies_gate_python_legacy_removal() {
        let ops = ops_policy();
        assert!(ops.required_observability.contains(&"dashboard".to_owned()));
        assert!(ops.required_observability.contains(&"alerts".to_owned()));
        assert!(
            ops.required_runbooks
                .contains(&"artifact_quarantine".to_owned())
        );
        assert!(
            ops.required_drills
                .contains(&"process_cleanup_failure".to_owned())
        );

        let decommission = decommission_policy();
        assert!(
            decommission
                .required_removed_legacy_paths
                .contains(&"python_trust_gate".to_owned())
        );
        assert!(
            decommission
                .required_removed_legacy_paths
                .contains(&"python_subprocess_sandbox_policy".to_owned())
        );
        assert!(
            decommission
                .required_facade_only_paths
                .contains(&"python_facade".to_owned())
        );
        assert!(
            decommission
                .required_evidence
                .contains(&"decommission_grep".to_owned())
        );
        assert!(
            decommission
                .allowed_rollback_paths
                .contains(&"deployment_rollback".to_owned())
        );
        assert_eq!(
            SkillRuntimeErrorCode::DecommissionBlocked.category(),
            "quality_gate"
        );
    }
}

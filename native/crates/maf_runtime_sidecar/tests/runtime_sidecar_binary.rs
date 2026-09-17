use maf_runtime_sidecar::pb::runtime::v1 as runtime_pb;
use maf_runtime_sidecar::{COMPONENT_ID, RuntimeSidecarSqliteAdapter};
use sha2::{Digest, Sha256};
use std::fs::OpenOptions;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, symlink};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn binary_empty_finalization(finalized_at_ms: i64) -> (String, Vec<u8>, Vec<u8>) {
    let inventory = |kind: &str| {
        let hash = |suffix: &str, value: &[u8]| {
            let mut hasher = Sha256::new();
            hasher.update(format!("maf.submission_authority.inventory.{suffix}.v1\0").as_bytes());
            hasher.update(kind.as_bytes());
            hasher.update(b"\0");
            hasher.update(value);
            format!("{:x}", hasher.finalize())
        };
        serde_json::json!({
            "canonical_sha256": hash("rows", b"[]"),
            "count": 0,
            "finalize_empty": true,
            "pk_sha256": hash("pk", b"[]"),
        })
    };
    let features = serde_json::to_vec(&maf_runtime_sidecar::supported_features()).unwrap();
    let subject = serde_json::to_vec(&serde_json::json!({
        "active_task_inventory": inventory("active_tasks"),
        "conversation_inventory": inventory("conversations"),
        "message_identity_inventory": inventory("message_identities"),
        "proto_hash": maf_runtime_store::PROTO_HASH,
        "report_sha256": "1".repeat(64),
        "schema": "maf.submission_authority.finalization_subject.v1",
        "schema_hash": maf_runtime_store::SCHEMA_HASH,
        "snapshot_boundary_sha256": "2".repeat(64),
        "source_backend": "sqlite",
        "source_identity_sha256": "3".repeat(64),
        "supported_features_sha256": format!("{:x}", Sha256::digest(features)),
        "writer_fence_sha256": "4".repeat(64),
    }))
    .unwrap();
    let mut hasher = Sha256::new();
    hasher.update(b"maf.submission_authority.finalization.v1\0");
    hasher.update(&subject);
    let digest = format!("{:x}", hasher.finalize());
    let subject_value: serde_json::Value = serde_json::from_slice(&subject).unwrap();
    let receipt = serde_json::to_vec(&serde_json::json!({
        "destination_schema_sha256": "c".repeat(64),
        "finalization_receipt_sha256": digest,
        "finalized_at_ms": finalized_at_ms,
        "inventories": {
            "active_tasks": subject_value["active_task_inventory"],
            "conversations": subject_value["conversation_inventory"],
            "message_identities": subject_value["message_identity_inventory"],
        },
        "result": "finalized",
        "schema": "maf.submission_authority.import_receipt.v1",
        "snapshot_boundary_sha256": "2".repeat(64),
        "source_identity_sha256": "3".repeat(64),
        "writer_fence_sha256": "4".repeat(64),
    }))
    .unwrap();
    (digest, subject, receipt)
}

fn binary_admission_request() -> runtime_pb::AdmitSubmissionRequest {
    let canonical = |value| serde_json::to_vec(&value).unwrap();
    let conversation = canonical(serde_json::json!({
        "conversation_id":"binary-conversation","create_if_missing":true,"created_at":"2026-08-26T00:00:00Z","current_task_id":"binary-task","schema":"maf.submission.conversation_projection.v1","status":"active","updated_at":"2026-08-26T00:00:00Z","username":"owner"
    }));
    let message = canonical(serde_json::json!({
        "content":"hello","conversation_id":"binary-conversation","message_created_at":"2026-08-26T00:00:00Z","message_id":"binary-message","message_type":"text","metadata":{},"role":"user","schema":"maf.submission.message_projection.v1","stream_status":"complete","task_id":"binary-task","updated_at":"2026-08-26T00:00:00Z"
    }));
    let continuation = canonical(serde_json::json!({
        "available_mcp_servers":[],"bundle_revisions":{"mcp_bundle_revision":null,"skill_bundle_revision":null},"conversation_id":"binary-conversation","execution_metadata":{"canonical_capability_id":null,"defer_task_completed_until_pending_skill_context_processed":null,"forced_by_mcp_command":null,"mcp_binding_mode":null,"mcp_command":null,"mcp_dispatch_server_id":null,"mcp_execution_mode":null,"mcp_rollout_config_version":null,"mcp_rollout_mode":null,"mcp_route_reason_code":null,"mcp_shadow_enabled":null,"requested_capability_alias":null},"initial_no_server_eligible":false,"mcp_assignment":null,"mcp_binding":null,"message_content_sha256":format!("{:x}",Sha256::digest(b"hello")),"message_id":"binary-message","model_options":{"model_edition":null,"reasoning_effort":"medium","thinking_enabled":false},"owner_scope":"owner","pending_context":null,"request_fingerprint":"a".repeat(64),"requested_capability_id":null,"routing_mode":"auto","schema":"maf.submission.continuation.v1","sheet_selections":{},"task_id":"binary-task","upload_refs":[]
    }));
    let domain = |prefix: &[u8], parts: &[&[u8]]| {
        let mut hasher = Sha256::new();
        hasher.update(prefix);
        for part in parts {
            hasher.update(part);
        }
        format!("{:x}", hasher.finalize())
    };
    runtime_pb::AdmitSubmissionRequest {
        message_id: "binary-message".to_owned(),
        task_id: "binary-task".to_owned(),
        conversation_id: "binary-conversation".to_owned(),
        username: "owner".to_owned(),
        request_fingerprint: "a".repeat(64),
        projection_sha256: domain(
            b"maf.submission.projection.v1\0",
            &[&conversation, b"\0", &message],
        ),
        continuation_sha256: domain(b"maf.submission.continuation.v1\0", &[&continuation]),
        conversation_projection_json: conversation,
        message_projection_json: message,
        continuation_json: continuation,
        message_created_at_ms: 1,
        workflow_owner: "binary-worker".to_owned(),
        now_ms: 1,
        claim_ttl_ms: 1_000,
        task: Some(runtime_pb::TaskRecord {
            task_id: "binary-task".to_owned(),
            conversation_id: "binary-conversation".to_owned(),
            root_message_id: "binary-message".to_owned(),
            status: "accepted".to_owned(),
            routing_mode: "auto".to_owned(),
            requested_capability_id: None,
            summary: None,
            cancel_requested_at: None,
            created_at: Some("2026-08-26T00:00:00Z".to_owned()),
            updated_at: None,
            assignment: None,
        }),
        idempotency_key: "submission:binary-message".to_owned(),
    }
}

fn binary_temp_db() -> PathBuf {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "maf-runtime-sidecar-binary-{}-{}.sqlite",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    path
}

#[test]
fn runtime_sidecar_binary_exposes_version_without_starting_python() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let output = Command::new(binary)
        .arg("--version")
        .output()
        .expect("run runtime sidecar binary");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert!(stdout.contains(COMPONENT_ID));
    assert!(stdout.contains("maf.runtime.v1"));
}

#[test]
fn runtime_sidecar_binary_rejects_partial_mtls_flags_without_serving() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let output = Command::new(binary)
        .args(["--serve", "127.0.0.1:0", "--tls-cert", "/tmp/server.crt"])
        .output()
        .expect("run runtime sidecar binary");

    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).expect("utf8 stderr");
    assert!(stderr.contains("mTLS requires --tls-cert, --tls-key, and --client-ca"));
}

#[cfg(unix)]
#[test]
fn runtime_sidecar_binary_rejects_operator_fence_through_sqlite_symlink() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let db_path = binary_temp_db();
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    drop(adapter);
    let alias_path = db_path.with_extension("alias.sqlite");
    symlink(&db_path, &alias_path).unwrap();
    let lock_path = format!("{}.submission-authority-migration.lock", db_path.display());
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    options.mode(0o600);
    let lock = options.open(&lock_path).unwrap();
    lock.lock().unwrap();

    let mut child = Command::new(binary)
        .args([
            "--serve",
            "127.0.0.1:0",
            "--sqlite",
            alias_path.to_str().unwrap(),
        ])
        .stderr(Stdio::null())
        .spawn()
        .expect("run runtime sidecar binary");
    let mut status = None;
    for _ in 0..100 {
        status = child.try_wait().unwrap();
        if status.is_some() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    if status.is_none() {
        child.kill().unwrap();
        child.wait().unwrap();
    }
    lock.unlock().unwrap();
    let _ = std::fs::remove_file(lock_path);
    let _ = std::fs::remove_file(alias_path);
    let _ = std::fs::remove_file(db_path);
    let status = status.expect("symlink alias must not bypass the operator writer fence");
    assert!(!status.success());
}

#[cfg(unix)]
#[test]
fn runtime_sidecar_symlink_shared_fence_blocks_real_path_operator() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar")
        .expect("runtime sidecar binary should be built for integration test");
    let db_path = binary_temp_db();
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    drop(adapter);
    let alias_path = db_path.with_extension("alias.sqlite");
    symlink(&db_path, &alias_path).unwrap();
    let lock_path = format!("{}.submission-authority-migration.lock", db_path.display());

    let mut child = Command::new(binary)
        .args([
            "--serve",
            "127.0.0.1:0",
            "--sqlite",
            alias_path.to_str().unwrap(),
        ])
        .spawn()
        .expect("start runtime sidecar binary");
    for _ in 0..100 {
        if PathBuf::from(&lock_path).exists() && child.try_wait().unwrap().is_none() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(child.try_wait().unwrap().is_none());
    let operator_lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
        .unwrap();
    assert!(matches!(
        operator_lock.try_lock(),
        Err(std::fs::TryLockError::WouldBlock)
    ));

    child.kill().unwrap();
    child.wait().unwrap();
    let alias_lock = format!(
        "{}.submission-authority-migration.lock",
        alias_path.display()
    );
    assert!(!PathBuf::from(&alias_lock).exists());
    let _ = std::fs::remove_file(lock_path);
    let _ = std::fs::remove_file(alias_path);
    let _ = std::fs::remove_file(db_path);
}

#[tokio::test]
async fn runtime_sidecar_binary_restarts_with_durable_sqlite_admission() {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar").unwrap();
    let db_path = binary_temp_db();
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    let (digest, subject, receipt) = binary_empty_finalization(1);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
        .unwrap();
    drop(adapter);
    let request = binary_admission_request();

    let reserve_addr = || {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        addr
    };
    let connect = |addr| async move {
        let endpoint = format!("http://{addr}");
        for _ in 0..100 {
            if let Ok(client) =
                runtime_pb::runtime_sidecar_client::RuntimeSidecarClient::connect(endpoint.clone())
                    .await
            {
                return client;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        panic!("binary sidecar did not become ready")
    };

    let first_addr = reserve_addr();
    let mut first_child = Command::new(&binary)
        .args([
            "--serve",
            &first_addr.to_string(),
            "--sqlite",
            db_path.to_str().unwrap(),
        ])
        .spawn()
        .unwrap();
    let mut first_client = connect(first_addr).await;
    let lock_path = format!("{}.submission-authority-migration.lock", db_path.display());
    let operator_lock = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
        .unwrap();
    assert!(matches!(
        operator_lock.try_lock(),
        Err(std::fs::TryLockError::WouldBlock)
    ));
    let created = first_client
        .admit_submission(request.clone())
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        created.disposition,
        runtime_pb::SubmissionAdmissionDisposition::Created as i32
    );
    let blocked = first_client
        .claim_pending_submission(runtime_pb::ClaimPendingSubmissionRequest {
            workflow_owner: "binary-recovery".to_owned(),
            now_ms: 2,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .await
        .unwrap()
        .into_inner();
    assert!(!blocked.found);
    assert_eq!(blocked.pending_count, 1);
    assert_eq!(blocked.earliest_claim_expires_at_ms, Some(1_001));
    first_child.kill().unwrap();
    first_child.wait().unwrap();
    operator_lock.try_lock().unwrap();
    operator_lock.unlock().unwrap();

    let second_addr = reserve_addr();
    let mut second_child = Command::new(&binary)
        .args([
            "--serve",
            &second_addr.to_string(),
            "--sqlite",
            db_path.to_str().unwrap(),
        ])
        .spawn()
        .unwrap();
    let mut second_client = connect(second_addr).await;
    let replay = second_client
        .admit_submission(request)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        replay.disposition,
        runtime_pb::SubmissionAdmissionDisposition::IdempotentReplay as i32
    );
    assert_eq!(replay.admission.unwrap().task_id, "binary-task");
    second_child.kill().unwrap();
    second_child.wait().unwrap();
    let _ = std::fs::remove_file(lock_path);
    let _ = std::fs::remove_file(db_path);
}

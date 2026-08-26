use maf_runtime_sidecar::{RuntimeSidecarSqliteAdapter, TaskRecord};
use rusqlite::Connection;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn canonical(value: serde_json::Value) -> Vec<u8> {
    serde_json::to_vec(&value).expect("canonical JSON")
}

fn empty_request() -> Vec<u8> {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../tests/fixtures/runtime_sidecar_submission_import_vectors.json"
    )))
    .unwrap();
    fixture["empty_sqlite"]["request_canonical_json"]
        .as_str()
        .unwrap()
        .as_bytes()
        .to_vec()
}

#[derive(Clone, Serialize)]
struct TestInventory {
    count: u32,
    pk_sha256: String,
    canonical_sha256: String,
    finalize_empty: bool,
}

fn inventory(kind: &str, records: &[serde_json::Value], primary_key: &str) -> TestInventory {
    let keys = records
        .iter()
        .map(|record| record[primary_key].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    let digest = |domain: &str, value: &[u8]| {
        let mut hasher = Sha256::new();
        hasher.update(domain.as_bytes());
        hasher.update(b"\0");
        hasher.update(kind.as_bytes());
        hasher.update(b"\0");
        hasher.update(value);
        format!("{:x}", hasher.finalize())
    };
    TestInventory {
        count: records.len() as u32,
        pk_sha256: digest(
            "maf.submission_authority.inventory.pk.v1",
            &serde_json::to_vec(&keys).unwrap(),
        ),
        canonical_sha256: digest(
            "maf.submission_authority.inventory.rows.v1",
            &serde_json::to_vec(records).unwrap(),
        ),
        finalize_empty: records.is_empty(),
    }
}

fn request_for(
    conversations: Vec<serde_json::Value>,
    message_identities: Vec<serde_json::Value>,
    active_tasks: Vec<serde_json::Value>,
    report_digit: char,
) -> Vec<u8> {
    let conversation_inventory = inventory("conversations", &conversations, "conversation_id");
    let message_inventory = inventory("message_identities", &message_identities, "message_id");
    let active_inventory = inventory("active_tasks", &active_tasks, "task_id");
    let features = serde_json::to_vec(&maf_runtime_sidecar::supported_features()).unwrap();
    let supported_features_sha256 = format!("{:x}", Sha256::digest(features));
    let report_sha256 = report_digit.to_string().repeat(64);
    let subject = canonical(serde_json::json!({
        "active_task_inventory": active_inventory,
        "conversation_inventory": conversation_inventory,
        "message_identity_inventory": message_inventory,
        "proto_hash": maf_runtime_store::PROTO_HASH,
        "report_sha256": report_sha256,
        "schema": "maf.submission_authority.finalization_subject.v1",
        "schema_hash": maf_runtime_store::SCHEMA_HASH,
        "snapshot_boundary_sha256": "2".repeat(64),
        "source_backend": "sqlite",
        "source_identity_sha256": "1".repeat(64),
        "supported_features_sha256": supported_features_sha256,
        "writer_fence_sha256": "3".repeat(64),
    }));
    let mut hasher = Sha256::new();
    hasher.update(b"maf.submission_authority.finalization.v1\0");
    hasher.update(subject);
    let finalization_receipt_sha256 = format!("{:x}", hasher.finalize());
    canonical(serde_json::json!({
        "conversations": conversations,
        "finalization_receipt_sha256": finalization_receipt_sha256,
        "inventories": {
            "active_tasks": active_inventory,
            "conversations": conversation_inventory,
            "message_identities": message_inventory,
        },
        "message_identities": message_identities,
        "proto_hash": maf_runtime_store::PROTO_HASH,
        "report_sha256": report_sha256,
        "schema": "maf.submission_authority.import_request.v1",
        "schema_hash": maf_runtime_store::SCHEMA_HASH,
        "snapshot_boundary_sha256": "2".repeat(64),
        "source_backend": "sqlite",
        "source_identity_sha256": "1".repeat(64),
        "supported_features_sha256": supported_features_sha256,
        "writer_fence_sha256": "3".repeat(64),
    }))
}

fn legacy_identity_request() -> Vec<u8> {
    let conversations = vec![serde_json::json!({
        "active_task_id": null,
        "conversation_id": "legacy-conversation-secret",
        "status": "active",
        "updated_at_ms": 6,
        "username": "legacy-owner-secret"
    })];
    let message_identities = vec![serde_json::json!({
        "conversation_id": "legacy-conversation-secret",
        "identity_kind": "legacy_conflict_only",
        "message_created_at_ms": null,
        "message_id": "legacy-message-secret",
        "message_type": null,
        "request_fingerprint": null,
        "reserved_at_ms": 6,
        "role": null,
        "task_id": null,
        "username": "legacy-owner-secret"
    })];
    request_for(conversations, message_identities, Vec::new(), '4')
}

#[test]
fn offline_stdin_import_finalizes_and_returns_first_safe_receipt_on_replay() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let request = legacy_identity_request();
    let first = adapter
        .import_submission_authority_from_stdin(&request, 7)
        .expect("first import");
    let replay = adapter
        .import_submission_authority_from_stdin(&request, 99)
        .expect("exact replay");
    let first: serde_json::Value = serde_json::from_slice(&first).expect("first receipt");
    let replay: serde_json::Value = serde_json::from_slice(&replay).expect("replay receipt");

    assert_eq!(
        first["schema"],
        "maf.submission_authority.import_receipt.v1"
    );
    assert_eq!(first["result"], "finalized");
    assert_eq!(first["finalized_at_ms"], 7);
    assert_eq!(replay["result"], "exact_replay");
    assert_eq!(replay["finalized_at_ms"], 7);
    assert_eq!(
        first["finalization_receipt_sha256"],
        replay["finalization_receipt_sha256"]
    );
    let keys = first
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(
        keys,
        [
            "schema",
            "result",
            "finalization_receipt_sha256",
            "finalized_at_ms",
            "source_identity_sha256",
            "snapshot_boundary_sha256",
            "writer_fence_sha256",
            "destination_schema_sha256",
            "inventories",
        ]
        .into_iter()
        .collect()
    );
    for inventory in first["inventories"].as_object().unwrap().values() {
        assert_eq!(
            inventory
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<std::collections::BTreeSet<_>>(),
            ["count", "pk_sha256", "canonical_sha256", "finalize_empty"]
                .into_iter()
                .collect()
        );
    }
    for sensitive in [
        "legacy-owner-secret",
        "legacy-conversation-secret",
        "legacy-message-secret",
    ] {
        assert!(!first.to_string().contains(sensitive));
    }
}

#[test]
fn offline_stdin_import_rejects_noncanonical_unknown_missing_forbidden_and_unsorted_input() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let base: serde_json::Value = serde_json::from_slice(&empty_request()).unwrap();
    let mut cases = Vec::new();

    let mut unknown = base.clone();
    unknown["unknown"] = serde_json::json!(true);
    cases.push(canonical(unknown));
    let mut missing = base.clone();
    missing.as_object_mut().unwrap().remove("report_sha256");
    cases.push(canonical(missing));
    for forbidden in [
        "path",
        "finalized_at_ms",
        "finalization_subject_json",
        "finalization_receipt_json",
        "active_task_ids",
    ] {
        let mut value = base.clone();
        value[forbidden] = serde_json::json!("forbidden");
        cases.push(canonical(value));
    }
    let mut count_drift = base.clone();
    count_drift["inventories"]["conversations"]["count"] = serde_json::json!(1);
    count_drift["inventories"]["conversations"]["finalize_empty"] = serde_json::json!(false);
    cases.push(canonical(count_drift));
    let mut inventory_missing = base.clone();
    inventory_missing["inventories"]["conversations"]
        .as_object_mut()
        .unwrap()
        .remove("canonical_sha256");
    cases.push(canonical(inventory_missing));
    let mut inventory_unknown = base.clone();
    inventory_unknown["inventories"]["conversations"]["unknown"] = serde_json::json!(true);
    cases.push(canonical(inventory_unknown));
    for field in ["schema_hash", "proto_hash", "supported_features_sha256"] {
        let mut value = base.clone();
        value[field] = serde_json::json!("0".repeat(64));
        cases.push(canonical(value));
    }
    let nested = serde_json::from_slice::<serde_json::Value>(&request_for(
        vec![serde_json::json!({
            "active_task_id": null,
            "conversation_id": "a",
            "extra": true,
            "status": "active",
            "updated_at_ms": 1,
            "username": "owner"
        })],
        Vec::new(),
        Vec::new(),
        '4',
    ))
    .unwrap();
    let mut nested = nested;
    nested["conversations"][0]["extra"] = serde_json::json!(true);
    cases.push(canonical(nested));
    let mut nested_missing: serde_json::Value = serde_json::from_slice(&request_for(
        vec![serde_json::json!({"active_task_id":null,"conversation_id":"a","status":"active","updated_at_ms":1,"username":"owner"})],
        Vec::new(),
        Vec::new(),
        '4',
    ))
    .unwrap();
    nested_missing["conversations"][0]
        .as_object_mut()
        .unwrap()
        .remove("active_task_id");
    cases.push(canonical(nested_missing));
    let mut unsorted: serde_json::Value = serde_json::from_slice(&request_for(
        vec![
            serde_json::json!({"active_task_id":null,"conversation_id":"a","status":"active","updated_at_ms":1,"username":"owner"}),
            serde_json::json!({"active_task_id":null,"conversation_id":"b","status":"active","updated_at_ms":1,"username":"owner"}),
        ],
        Vec::new(),
        Vec::new(),
        '4',
    ))
    .unwrap();
    unsorted["conversations"].as_array_mut().unwrap().reverse();
    cases.push(canonical(unsorted));
    let mut pretty: serde_json::Value = serde_json::from_slice(&empty_request()).unwrap();
    pretty["report_sha256"] = serde_json::json!("5".repeat(64));
    cases.push(serde_json::to_string_pretty(&pretty).unwrap().into_bytes());

    for request in cases {
        assert_eq!(
            adapter
                .import_submission_authority_from_stdin(&request, 7)
                .expect_err("closed request must reject drift")
                .code,
            "runtime_store_write_failed"
        );
    }
}

#[test]
fn offline_stdin_import_enforces_record_size_and_accepts_one_thousand_sorted_rows() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let oversized = request_for(
        vec![serde_json::json!({
            "active_task_id": null,
            "conversation_id": "oversized",
            "status": "active",
            "updated_at_ms": 1,
            "username": "x".repeat(64 * 1024)
        })],
        Vec::new(),
        Vec::new(),
        '4',
    );
    assert_eq!(
        adapter
            .import_submission_authority_from_stdin(&oversized, 7)
            .expect_err("record above 64 KiB must fail")
            .code,
        "runtime_store_write_failed"
    );

    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let conversations = (0..1_000)
        .map(|index| {
            serde_json::json!({
                "active_task_id": null,
                "conversation_id": format!("conversation-{index:04}"),
                "status": "active",
                "updated_at_ms": index,
                "username": "owner"
            })
        })
        .collect();
    let request = request_for(conversations, Vec::new(), Vec::new(), '4');
    let receipt = adapter
        .import_submission_authority_from_stdin(&request, 8)
        .expect("1,000-row boundary is accepted");
    let receipt: serde_json::Value = serde_json::from_slice(&receipt).unwrap();
    assert_eq!(receipt["inventories"]["conversations"]["count"], 1_000);
}

#[test]
fn offline_stdin_import_rejects_a_different_finalization_subject() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    adapter
        .import_submission_authority_from_stdin(&empty_request(), 7)
        .expect("first import");
    let changed = request_for(Vec::new(), Vec::new(), Vec::new(), '9');
    assert_eq!(
        adapter
            .import_submission_authority_from_stdin(&changed, 8)
            .expect_err("different subject conflicts")
            .code,
        "runtime_store_idempotency_conflict"
    );
}

#[test]
fn offline_stdin_exact_replay_uses_first_active_task_inventory_after_task_evolves() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let task = TaskRecord {
        task_id: "active-task".to_owned(),
        conversation_id: "active-conversation".to_owned(),
        root_message_id: "active-root".to_owned(),
        status: "running".to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: None,
        cancel_requested_at: None,
        created_at: Some("1".to_owned()),
        updated_at: None,
        assignment: None,
    };
    adapter
        .submit_task_record(task.clone(), "seed-active-task", None)
        .unwrap();
    let request = request_for(
        vec![serde_json::json!({
            "active_task_id": "active-task",
            "conversation_id": "active-conversation",
            "status": "active",
            "updated_at_ms": 6,
            "username": "owner"
        })],
        Vec::new(),
        vec![serde_json::to_value(&task).unwrap()],
        '4',
    );
    let first = adapter
        .import_submission_authority_from_stdin(&request, 7)
        .unwrap();
    let mut terminal = task;
    terminal.status = "completed".to_owned();
    terminal.updated_at = Some("8".to_owned());
    adapter
        .submit_task_record(terminal, "complete-active-task", Some("running"))
        .unwrap();
    let replay = adapter
        .import_submission_authority_from_stdin(&request, 99)
        .expect("same subject remains replayable after Task evolution");
    let first: serde_json::Value = serde_json::from_slice(&first).unwrap();
    let replay: serde_json::Value = serde_json::from_slice(&replay).unwrap();
    assert_eq!(replay["result"], "exact_replay");
    assert_eq!(first["finalized_at_ms"], replay["finalized_at_ms"]);
    assert_eq!(first["inventories"], replay["inventories"]);
}

#[test]
fn offline_stdin_import_rejects_escaped_active_task_record_above_sixty_four_kib_before_write() {
    let path = temp_db_path();
    let adapter = RuntimeSidecarSqliteAdapter::open(&path).expect("open adapter");
    let task = TaskRecord {
        task_id: "oversized-active-task".to_owned(),
        conversation_id: "oversized-active-conversation".to_owned(),
        root_message_id: "oversized-active-root".to_owned(),
        status: "running".to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: Some("\0".repeat(11_000)),
        cancel_requested_at: None,
        created_at: Some("1".to_owned()),
        updated_at: None,
        assignment: None,
    };
    adapter
        .submit_task_record(task.clone(), "seed-oversized-active-task", None)
        .unwrap();
    let canonical_task = serde_json::to_vec(&serde_json::to_value(&task).unwrap()).unwrap();
    assert!(canonical_task.len() > 64 * 1024);
    let request = request_for(
        vec![serde_json::json!({
            "active_task_id": "oversized-active-task",
            "conversation_id": "oversized-active-conversation",
            "status": "active",
            "updated_at_ms": 6,
            "username": "owner"
        })],
        Vec::new(),
        vec![serde_json::to_value(&task).unwrap()],
        '4',
    );
    assert_eq!(
        adapter
            .import_submission_authority_from_stdin(&request, 7)
            .expect_err("oversized active Task row must fail before import")
            .code,
        "runtime_store_write_failed"
    );
    let inspection = Connection::open(&path).unwrap();
    let (state, conversations, identities): (String, i64, i64) = inspection
        .query_row(
            "SELECT state, (SELECT count(*) FROM submission_conversations), (SELECT count(*) FROM submission_message_identities) FROM submission_authority_meta WHERE singleton_key=1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(state, "uninitialized");
    assert_eq!((conversations, identities), (0, 0));
    drop(inspection);
    drop(adapter);
    let _ = std::fs::remove_file(path);
}

fn temp_db_path() -> PathBuf {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "maf-submission-import-{}-{}.sqlite",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    path
}

fn run_binary(path: &Path, request: &[u8]) -> std::process::Output {
    let binary = std::env::var("CARGO_BIN_EXE_maf-runtime-sidecar-submission-import")
        .expect("offline import binary is built");
    let mut child = Command::new(binary)
        .args(["--sqlite", path.to_str().unwrap()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn offline importer");
    child.stdin.take().unwrap().write_all(request).unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn offline_import_binary_restarts_with_exact_receipt_and_never_leaks_target_path() {
    let path = temp_db_path();
    let first = run_binary(&path, &empty_request());
    assert!(first.status.success());
    let replay = run_binary(&path, &empty_request());
    assert!(replay.status.success());
    let first: serde_json::Value = serde_json::from_slice(&first.stdout).unwrap();
    let replay: serde_json::Value = serde_json::from_slice(&replay.stdout).unwrap();
    assert_eq!(first["result"], "finalized");
    assert_eq!(replay["result"], "exact_replay");
    assert_eq!(first["finalized_at_ms"], replay["finalized_at_ms"]);
    assert_eq!(
        first["finalization_receipt_sha256"],
        replay["finalization_receipt_sha256"]
    );
    let invalid = run_binary(&path, br#"{"path":"secret-target"}"#);
    assert!(!invalid.status.success());
    let stderr = String::from_utf8(invalid.stderr).unwrap();
    assert_eq!(stderr, "submission_import_failed\n");
    assert!(!stderr.contains(path.to_str().unwrap()));
    let unopened_path = temp_db_path();
    let invalid = run_binary(&unopened_path, br#"{"path":"secret-target"}"#);
    assert!(!invalid.status.success());
    assert!(
        !unopened_path.exists(),
        "invalid stdin is rejected before DB open"
    );
    let _ = std::fs::remove_file(path);
}

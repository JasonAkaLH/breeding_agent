use maf_runtime_sidecar::pb::common::v1 as common_pb;
use maf_runtime_sidecar::pb::runtime::v1 as runtime_pb;
use maf_runtime_sidecar::{
    COMPONENT_ID, GRPC_MAX_MESSAGE_BYTES, RuntimeSidecarGrpcService, RuntimeSidecarServeConfig,
    RuntimeSidecarSqliteAdapter, runtime_sidecar_service_from_config,
    serve_runtime_sidecar_with_incoming,
};
#[cfg(unix)]
use maf_runtime_sidecar::{
    semantic_probe_runtime_sidecar_unix_socket, serve_runtime_sidecar_unix_socket,
};
use prost::Message as _;
use runtime_pb::runtime_sidecar_server::RuntimeSidecar;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tonic::Request;

static TEMP_PATH_COUNTER: AtomicU64 = AtomicU64::new(0);

fn sqlite_empty_finalization(finalized_at_ms: i64) -> (String, Vec<u8>, Vec<u8>) {
    let inventory = |kind: &str| {
        let pk = serde_json::to_vec(&Vec::<String>::new()).unwrap();
        let records = serde_json::to_vec(&Vec::<serde_json::Value>::new()).unwrap();
        let digest = |suffix: &str, bytes: &[u8]| {
            let mut hasher = Sha256::new();
            hasher.update(
                format!("maf.submission_authority.inventory.{kind}.{suffix}.v1\0").as_bytes(),
            );
            hasher.update(bytes);
            format!("{:x}", hasher.finalize())
        };
        serde_json::json!({
            "canonical_sha256": digest("records", &records),
            "count": 0,
            "finalize_empty": true,
            "pk_sha256": digest("pk", &pk),
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
    .expect("canonical finalization receipt");
    (digest, subject, receipt)
}

fn large_pb_admit_request() -> runtime_pb::AdmitSubmissionRequest {
    let target_content_bytes = 49 * 1024 * 1024;
    let mut content = "\"\\中".to_owned();
    content.push_str(&"a".repeat(target_content_bytes - content.len()));
    pb_admit_request("large", &content)
}

fn pb_admit_request(prefix: &str, content: &str) -> runtime_pb::AdmitSubmissionRequest {
    let conversation_id = format!("{prefix}-conversation");
    let task_id = format!("{prefix}-task");
    let message_id = format!("{prefix}-message");
    let canonical = |value: serde_json::Value| serde_json::to_vec(&value).expect("canonical JSON");
    let conversation = canonical(serde_json::json!({
        "conversation_id": conversation_id,
        "create_if_missing": true,
        "created_at": "2026-08-26T00:00:00Z",
        "current_task_id": task_id,
        "schema": "maf.submission.conversation_projection.v1",
        "status": "active",
        "updated_at": "2026-08-26T00:00:00Z",
        "username": "owner"
    }));
    let message = canonical(serde_json::json!({
        "content": &content,
        "conversation_id": conversation_id,
        "message_created_at": "2026-08-26T00:00:00Z",
        "message_id": message_id,
        "message_type": "text",
        "metadata": {},
        "role": "user",
        "schema": "maf.submission.message_projection.v1",
        "stream_status": "complete",
        "task_id": task_id,
        "updated_at": "2026-08-26T00:00:00Z"
    }));
    let continuation = canonical(serde_json::json!({
        "available_mcp_servers": [],
        "bundle_revisions": {"mcp_bundle_revision": null, "skill_bundle_revision": null},
        "conversation_id": conversation_id,
        "execution_metadata": {
            "requested_capability_alias": null,
            "canonical_capability_id": null,
            "mcp_dispatch_server_id": null,
            "mcp_binding_mode": null,
            "mcp_command": null,
            "mcp_execution_mode": null,
            "mcp_rollout_config_version": null,
            "mcp_route_reason_code": null,
            "mcp_rollout_mode": null,
            "defer_task_completed_until_pending_skill_context_processed": null,
            "forced_by_mcp_command": null,
            "mcp_shadow_enabled": null
        },
        "initial_no_server_eligible": false,
        "mcp_assignment": null,
        "mcp_binding": null,
        "message_content_sha256": format!("{:x}", Sha256::digest(content.as_bytes())),
        "message_id": message_id,
        "model_options": {"model_edition": null, "reasoning_effort": "medium", "thinking_enabled": false},
        "owner_scope": "owner",
        "pending_context": null,
        "request_fingerprint": "a".repeat(64),
        "requested_capability_id": null,
        "routing_mode": "auto",
        "schema": "maf.submission.continuation.v1",
        "sheet_selections": {},
        "task_id": task_id,
        "upload_refs": []
    }));
    let mut projection_hasher = Sha256::new();
    projection_hasher.update(b"maf.submission.projection.v1\0");
    projection_hasher.update(&conversation);
    projection_hasher.update(b"\0");
    projection_hasher.update(&message);
    let mut continuation_hasher = Sha256::new();
    continuation_hasher.update(b"maf.submission.continuation.v1\0");
    continuation_hasher.update(&continuation);
    runtime_pb::AdmitSubmissionRequest {
        message_id: message_id.clone(),
        task_id: task_id.clone(),
        conversation_id: conversation_id.clone(),
        username: "owner".to_owned(),
        request_fingerprint: "a".repeat(64),
        conversation_projection_json: conversation,
        message_projection_json: message,
        projection_sha256: format!("{:x}", projection_hasher.finalize()),
        continuation_json: continuation,
        continuation_sha256: format!("{:x}", continuation_hasher.finalize()),
        message_created_at_ms: 1,
        workflow_owner: format!("{prefix}-worker"),
        now_ms: 1,
        claim_ttl_ms: 1_000,
        task: Some(runtime_pb::TaskRecord {
            task_id,
            conversation_id,
            root_message_id: message_id.clone(),
            status: "accepted".to_owned(),
            routing_mode: "auto".to_owned(),
            requested_capability_id: None,
            summary: None,
            cancel_requested_at: None,
            created_at: Some("2026-08-26T00:00:00Z".to_owned()),
            updated_at: None,
            assignment: None,
        }),
        idempotency_key: format!("submission:{message_id}"),
    }
}

fn pb_prepared_execution(prefix: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "available_mcp_servers": [],
        "bundle_revisions": {"mcp_bundle_revision": null, "skill_bundle_revision": null},
        "conversation_id": format!("{prefix}-conversation"),
        "execution_metadata": {
            "canonical_capability_id": null,
            "defer_task_completed_until_pending_skill_context_processed": null,
            "forced_by_mcp_command": null,
            "mcp_binding_mode": null,
            "mcp_command": null,
            "mcp_dispatch_server_id": null,
            "mcp_execution_mode": null,
            "mcp_rollout_config_version": null,
            "mcp_rollout_mode": null,
            "mcp_route_reason_code": null,
            "mcp_shadow_enabled": null,
            "requested_capability_alias": null
        },
        "execution_text_sha256": "c".repeat(64),
        "execution_text_source": "root_message",
        "initial_required_tool_name": null,
        "mcp_assignment": null,
        "mcp_binding": null,
        "message_id": format!("{prefix}-message"),
        "model_options": {"model_edition": null, "reasoning_effort": "medium", "thinking_enabled": false},
        "owner_scope": "owner",
        "pending_context": null,
        "planned_handoff_kind": "agent_run",
        "preparation_receipt": {
            "memory_context_sha256": "f".repeat(64),
            "receipt_sha256": "d".repeat(64),
            "route_decision_sha256": "e".repeat(64),
            "selector_decision_sha256": "0".repeat(64),
            "task_id": format!("{prefix}-task")
        },
        "prepared_kind": "agent_run",
        "requested_capability_id": null,
        "schema": "maf.submission.prepared_execution.v1",
        "sheet_selections": {},
        "task_id": format!("{prefix}-task"),
        "upload_refs": [],
    }))
    .expect("canonical prepared execution")
}

#[tokio::test]
async fn submission_identity_rpc_round_trips_in_memory_and_sqlite_cutover_is_explicit() {
    assert!(std::hint::black_box(GRPC_MAX_MESSAGE_BYTES) >= 140 * 1024 * 1024);
    let request = runtime_pb::ReserveMessageIdentityRequest {
        identity: Some(runtime_pb::MessageIdentityRecord {
            message_id: "grpc-file-message".to_owned(),
            conversation_id: "grpc-file-conversation".to_owned(),
            username: "owner".to_owned(),
            identity_kind: runtime_pb::MessageIdentityKind::FileVisible as i32,
            role: Some("assistant".to_owned()),
            message_type: Some("file".to_owned()),
            message_created_at_ms: Some(1),
            task_id: None,
            request_fingerprint: None,
            reserved_at_ms: 2,
        }),
    };
    let in_memory =
        RuntimeSidecarGrpcService::new_with_finalized_submission_authority("f".repeat(64));
    let created = in_memory
        .reserve_message_identity(Request::new(request.clone()))
        .await
        .expect("reserve in-memory identity")
        .into_inner();
    assert_eq!(
        created.disposition,
        runtime_pb::MessageIdentityDisposition::Created as i32
    );
    let exact = in_memory
        .reserve_message_identity(Request::new(request.clone()))
        .await
        .expect("exact in-memory identity")
        .into_inner();
    assert_eq!(
        exact.disposition,
        runtime_pb::MessageIdentityDisposition::ExactReplay as i32
    );

    let db_path = temp_db_path("grpc-submission-a1-blocked");
    let sqlite = RuntimeSidecarGrpcService::with_sqlite_adapter(
        RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter"),
    );
    let blocked = sqlite
        .reserve_message_identity(Request::new(request))
        .await
        .expect("typed migration response")
        .into_inner();
    assert_eq!(
        blocked.error.expect("migration error").code,
        "runtime_store_migration_blocked"
    );
    let finalized_path = temp_db_path("grpc-submission-a2-finalized");
    let finalized_adapter = RuntimeSidecarSqliteAdapter::open(&finalized_path).unwrap();
    let (digest, subject, receipt) = sqlite_empty_finalization(1);
    finalized_adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
        .unwrap();
    let finalized = RuntimeSidecarGrpcService::with_sqlite_adapter(finalized_adapter);
    let created = finalized
        .reserve_message_identity(Request::new(runtime_pb::ReserveMessageIdentityRequest {
            identity: Some(runtime_pb::MessageIdentityRecord {
                message_id: "sqlite-file-message".to_owned(),
                conversation_id: "sqlite-file-conversation".to_owned(),
                username: "owner".to_owned(),
                identity_kind: runtime_pb::MessageIdentityKind::FileVisible as i32,
                role: Some("assistant".to_owned()),
                message_type: Some("file".to_owned()),
                message_created_at_ms: Some(1),
                task_id: None,
                request_fingerprint: None,
                reserved_at_ms: 2,
            }),
        }))
        .await
        .expect("SQLite reservation RPC")
        .into_inner();
    assert_eq!(
        created.disposition,
        runtime_pb::MessageIdentityDisposition::Created as i32
    );
    assert!(created.error.is_none());
    let _ = std::fs::remove_file(finalized_path);
}

#[tokio::test]
async fn sqlite_submission_rpc_sequence_persists_all_admission_phases() {
    let db_path = temp_db_path("grpc-submission-a2-sequence");
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    let (digest, subject, receipt) = sqlite_empty_finalization(2);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 2)
        .unwrap();
    let service = RuntimeSidecarGrpcService::with_sqlite_adapter(adapter);
    let created = service
        .admit_submission(Request::new(pb_admit_request("sqlite-flow", "hello")))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        created.disposition,
        runtime_pb::SubmissionAdmissionDisposition::Created as i32
    );
    let admission = created.admission.unwrap();
    let initial_claim = created.claim.unwrap();
    let renewed = service
        .renew_submission_claim(Request::new(runtime_pb::RenewSubmissionClaimRequest {
            message_id: admission.message_id.clone(),
            workflow_owner: initial_claim.owner,
            claim_token: initial_claim.token,
            now_ms: 2,
            claim_ttl_ms: 1_000,
        }))
        .await
        .unwrap()
        .into_inner()
        .claim
        .expect("renewed claim");
    let projected = service
        .acknowledge_submission_projection(Request::new(
            runtime_pb::AcknowledgeSubmissionProjectionRequest {
                message_id: admission.message_id.clone(),
                workflow_owner: renewed.owner.clone(),
                claim_token: renewed.token.clone(),
                projection_sha256: admission.projection_sha256.clone(),
                expected_state: runtime_pb::SubmissionProjectionState::Pending as i32,
                now_ms: 3,
            },
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(projected.error.is_none());
    let prepared = pb_prepared_execution("sqlite-flow");
    let mut hasher = Sha256::new();
    hasher.update(b"maf.submission.prepared_execution.v1\0");
    hasher.update(&prepared);
    let prepared_sha256 = format!("{:x}", hasher.finalize());
    let prepared_response = service
        .prepare_submission_handoff(Request::new(runtime_pb::PrepareSubmissionHandoffRequest {
            message_id: admission.message_id.clone(),
            workflow_owner: renewed.owner.clone(),
            claim_token: renewed.token.clone(),
            prepared_execution_json: prepared.clone(),
            prepared_execution_sha256: prepared_sha256.clone(),
            expected_state: runtime_pb::SubmissionPreparationState::Pending as i32,
            now_ms: 4,
        }))
        .await
        .unwrap()
        .into_inner();
    assert!(prepared_response.error.is_none());
    let stored = service
        .get_submission_preparation(Request::new(runtime_pb::GetSubmissionPreparationRequest {
            username: "owner".to_owned(),
            conversation_id: "sqlite-flow-conversation".to_owned(),
            task_id: "sqlite-flow-task".to_owned(),
        }))
        .await
        .unwrap()
        .into_inner();
    assert!(stored.found);
    assert_eq!(
        stored.admission.unwrap().prepared_execution_json,
        Some(prepared)
    );
    let handed_off = service
        .acknowledge_submission_handoff(Request::new(
            runtime_pb::AcknowledgeSubmissionHandoffRequest {
                message_id: admission.message_id,
                workflow_owner: renewed.owner,
                claim_token: renewed.token,
                prepared_execution_sha256: prepared_sha256,
                handoff_kind: "agent_run".to_owned(),
                handoff_identity: "run-sqlite-flow".to_owned(),
                expected_state: runtime_pb::SubmissionHandoffState::Pending as i32,
                now_ms: 5,
            },
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(handed_off.error.is_none());
    let empty = service
        .claim_pending_submission(Request::new(runtime_pb::ClaimPendingSubmissionRequest {
            workflow_owner: "recovery".to_owned(),
            now_ms: 2_000,
            claim_ttl_ms: 1_000,
            after_created_at_ms: None,
            after_message_id: None,
        }))
        .await
        .unwrap()
        .into_inner();
    assert!(!empty.found);
    assert_eq!(empty.finalization_receipt_sha256, Some(digest));
    let closed = service
        .close_conversation_admission(Request::new(
            runtime_pb::CloseConversationAdmissionRequest {
                username: "owner".to_owned(),
                conversation_id: "sqlite-flow-conversation".to_owned(),
                operation_id: "delete:sqlite-flow-conversation".to_owned(),
                now_ms: 6,
            },
        ))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        closed.disposition,
        runtime_pb::ConversationAdmissionCloseDisposition::Closed as i32
    );
    let _ = std::fs::remove_file(db_path);
}

fn pb_task(status: &str) -> runtime_pb::TaskRecord {
    runtime_pb::TaskRecord {
        task_id: "grpc-task".to_owned(),
        conversation_id: "conv".to_owned(),
        root_message_id: "message".to_owned(),
        status: status.to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: None,
        cancel_requested_at: None,
        created_at: Some("created".to_owned()),
        updated_at: None,
        assignment: Some(runtime_pb::TaskRouteAssignment {
            route_mode: "shadow".to_owned(),
            real_path: "legacy".to_owned(),
            shadow_path: "user_scoped".to_owned(),
            config_version: "v1".to_owned(),
            reason_code: "sample".to_owned(),
            cohort_id: None,
            assignment_key_hash: None,
            assigned_at: None,
        }),
    }
}

#[tokio::test]
async fn tonic_service_submits_and_gets_authoritative_task_record() {
    let service = RuntimeSidecarGrpcService::new();
    let submitted = service
        .submit_task(Request::new(runtime_pb::SubmitTaskRequest {
            task_id: "grpc-task".to_owned(),
            conversation_id: "conv".to_owned(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "grpc-task-key".to_owned(),
                owner: "test".to_owned(),
                deadline_ms: 1,
            }),
            task: Some(pb_task("accepted")),
            expected_from_status: None,
        }))
        .await
        .expect("submit")
        .into_inner();
    assert_eq!(submitted.task.expect("record").root_message_id, "message");
    let found = service
        .get_task(Request::new(runtime_pb::GetTaskRequest {
            task_id: "grpc-task".to_owned(),
        }))
        .await
        .expect("get")
        .into_inner();
    assert!(found.found);
    assert_eq!(
        found
            .task
            .expect("task")
            .assignment
            .expect("assignment")
            .config_version,
        "v1"
    );
    let missing = service
        .get_task(Request::new(runtime_pb::GetTaskRequest {
            task_id: "missing".to_owned(),
        }))
        .await
        .expect("get missing")
        .into_inner();
    assert!(!missing.found);
    assert!(missing.task.is_none());
    let listed = service
        .list_tasks_for_conversation(Request::new(runtime_pb::ListTasksForConversationRequest {
            conversation_id: "conv".to_owned(),
            statuses: vec!["accepted".to_owned()],
        }))
        .await
        .expect("list tasks")
        .into_inner();
    assert_eq!(listed.tasks.len(), 1);
    assert_eq!(listed.tasks[0].task_id, "grpc-task");
    let active = service
        .get_active_task_for_conversation(Request::new(
            runtime_pb::GetActiveTaskForConversationRequest {
                conversation_id: "conv".to_owned(),
            },
        ))
        .await
        .expect("get active task")
        .into_inner();
    assert!(active.found);
    assert_eq!(active.task.expect("active task").task_id, "grpc-task");
}

#[tokio::test]
async fn tonic_service_rejects_conflicting_top_level_task_identity() {
    let service = RuntimeSidecarGrpcService::new();
    let response = service
        .submit_task(Request::new(runtime_pb::SubmitTaskRequest {
            task_id: "different-task".to_owned(),
            conversation_id: "conv".to_owned(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "conflicting-identity".to_owned(),
                owner: "test".to_owned(),
                deadline_ms: 1,
            }),
            task: Some(pb_task("accepted")),
            expected_from_status: None,
        }))
        .await
        .expect("typed write failure response")
        .into_inner();

    assert_eq!(
        response.error.expect("identity conflict error").code,
        "runtime_store_write_failed"
    );
    assert!(response.task.is_none());
}

#[tokio::test]
async fn tonic_service_maps_pb_requests_to_rust_adapter_envelopes() {
    let service = RuntimeSidecarGrpcService::new();

    let version = service
        .version(Request::new(runtime_pb::VersionRequest {}))
        .await
        .expect("version")
        .into_inner()
        .version
        .expect("version info");
    assert_eq!(version.component, COMPONENT_ID);

    let compatibility = service
        .check_compatibility(Request::new(runtime_pb::CompatibilityCheckRequest {
            client_version: "python-client-1".to_owned(),
            expected_component: version.component.clone(),
            expected_protocol_version: version.protocol_version.clone(),
            expected_schema_hash: version.schema_hash.clone(),
            expected_error_code_table_hash: version.error_code_table_hash.clone(),
            required_features: version.supported_features.clone(),
        }))
        .await
        .expect("compatibility")
        .into_inner();
    assert!(compatibility.compatible);
    assert!(compatibility.error.is_none());

    let append = service
        .append_event(Request::new(runtime_pb::AppendEventRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            event_type: "task.accepted".to_owned(),
            payload_json: b"{}".to_vec(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "event-1".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("append")
        .into_inner();
    assert!(append.error.is_none());
    assert_eq!(append.cursor.expect("cursor").sequence, 1);

    service.begin_shutdown_drain(100);
    let rejected = service
        .append_event(Request::new(runtime_pb::AppendEventRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            event_type: "during-drain".to_owned(),
            payload_json: b"{}".to_vec(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "event-2".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("append rejected in response envelope")
        .into_inner();
    let error = rejected.error.expect("typed error");
    assert_eq!(error.code, "runtime_store_unavailable");
    assert_eq!(error.category, common_pb::ErrorCategory::Internal as i32);
    assert!(error.retriable);
    assert!(rejected.cursor.is_none());
}

#[tokio::test]
async fn sqlite_backed_tonic_service_rejects_writes_after_shutdown_drain() {
    let db_path = temp_db_path("grpc-sqlite-drain");
    let service = RuntimeSidecarGrpcService::with_sqlite_adapter(
        RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter"),
    );

    let before_drain = service
        .append_event(Request::new(runtime_pb::AppendEventRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            event_type: "task.accepted".to_owned(),
            payload_json: b"{}".to_vec(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "event-before-drain".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("append before drain")
        .into_inner();
    assert!(before_drain.error.is_none());

    service.begin_shutdown_drain(100);

    let submit = service
        .submit_task(Request::new(runtime_pb::SubmitTaskRequest {
            task_id: "task-after-drain".to_owned(),
            conversation_id: "conv".to_owned(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "task-after-drain".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 3_000,
            }),
            task: None,
            expected_from_status: None,
        }))
        .await
        .expect("submit rejected in envelope")
        .into_inner();
    assert_runtime_unavailable(submit.error);
    assert!(submit.task_id.is_empty());

    let artifact = service
        .save_artifact(Request::new(runtime_pb::SaveArtifactRequest {
            artifact: Some(runtime_pb::ArtifactRecord {
                artifact_id: "artifact-after-drain".to_owned(),
                task_id: "task".to_owned(),
                producer_node_id: "node-b".to_owned(),
                artifact_type: "json".to_owned(),
                storage_ref: "opaque://artifact".to_owned(),
                summary: "summary".to_owned(),
                is_complete: true,
                created_at: "".to_owned(),
            }),
            idempotency: Some(runtime_pb::Idempotency {
                key: "artifact-after-drain".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("artifact rejected in envelope")
        .into_inner();
    assert_runtime_unavailable(artifact.error);
    assert!(artifact.artifact.is_none());
    assert!(!artifact.found);

    let append = service
        .append_event(Request::new(runtime_pb::AppendEventRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            event_type: "during-drain".to_owned(),
            payload_json: b"{}".to_vec(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "event-after-drain".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("append rejected in envelope")
        .into_inner();
    assert_runtime_unavailable(append.error);
    assert!(append.cursor.is_none());

    let _ = std::fs::remove_file(db_path);
}

#[tokio::test]
async fn tonic_service_can_use_sqlite_adapter_for_durable_event_replay() {
    let db_path = temp_db_path("grpc-sqlite");
    {
        let service = RuntimeSidecarGrpcService::with_sqlite_adapter(
            RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter"),
        );
        let append = service
            .append_event(Request::new(runtime_pb::AppendEventRequest {
                conversation_id: "conv".to_owned(),
                task_id: "task".to_owned(),
                event_type: "task.accepted".to_owned(),
                payload_json: b"{}".to_vec(),
                idempotency: Some(runtime_pb::Idempotency {
                    key: "event-1".to_owned(),
                    owner: "python-runtime".to_owned(),
                    deadline_ms: 2_000,
                }),
            }))
            .await
            .expect("append")
            .into_inner();
        assert_eq!(append.cursor.expect("cursor").sequence, 1);

        let artifact = service
            .save_artifact(Request::new(runtime_pb::SaveArtifactRequest {
                artifact: Some(runtime_pb::ArtifactRecord {
                    artifact_id: "artifact".to_owned(),
                    task_id: "task".to_owned(),
                    producer_node_id: "node-b".to_owned(),
                    artifact_type: "json".to_owned(),
                    storage_ref: "opaque://artifact".to_owned(),
                    summary: "summary".to_owned(),
                    is_complete: true,
                    created_at: "".to_owned(),
                }),
                idempotency: Some(runtime_pb::Idempotency {
                    key: "artifact-1".to_owned(),
                    owner: "python-runtime".to_owned(),
                    deadline_ms: 2_000,
                }),
            }))
            .await
            .expect("save artifact")
            .into_inner();
        assert!(artifact.found);
    }

    let reopened = RuntimeSidecarGrpcService::with_sqlite_adapter(
        RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter"),
    );
    let replay = reopened
        .replay_events(Request::new(runtime_pb::ReplayEventsRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            after_sequence: 0,
            page_limit: 10,
            byte_limit: 1024,
        }))
        .await
        .expect("replay")
        .into_inner();
    assert_eq!(replay.cursors.len(), 1);
    assert_eq!(replay.cursors[0].sequence, 1);
    let artifact = reopened
        .get_artifact(Request::new(runtime_pb::GetArtifactRequest {
            artifact_id: "artifact".to_owned(),
        }))
        .await
        .expect("get artifact")
        .into_inner();
    assert!(artifact.found);
    assert_eq!(
        artifact.artifact.expect("artifact").storage_ref,
        "opaque://artifact"
    );
    let _ = std::fs::remove_file(db_path);
}

#[tokio::test]
async fn serve_config_can_construct_sqlite_backed_grpc_service() {
    let db_path = temp_db_path("serve-config-sqlite");
    let config =
        RuntimeSidecarServeConfig::from_listen_addr_with_sqlite_path("127.0.0.1:50051", &db_path)
            .expect("sqlite-backed serve config");
    assert_eq!(config.sqlite_path.as_deref(), Some(db_path.as_path()));

    let service = runtime_sidecar_service_from_config(&config).expect("build sqlite service");
    let append = service
        .append_event(Request::new(runtime_pb::AppendEventRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            event_type: "task.accepted".to_owned(),
            payload_json: b"{}".to_vec(),
            idempotency: Some(runtime_pb::Idempotency {
                key: "event-1".to_owned(),
                owner: "python-runtime".to_owned(),
                deadline_ms: 2_000,
            }),
        }))
        .await
        .expect("append")
        .into_inner();
    assert_eq!(append.cursor.expect("cursor").sequence, 1);
    let _ = std::fs::remove_file(db_path);
}

#[tokio::test]
async fn serve_config_builds_sqlite_backed_service_from_configured_path() {
    let db_path = temp_db_path("serve-config-sqlite");
    {
        let config = RuntimeSidecarServeConfig::from_listen_addr_with_sqlite_path(
            "127.0.0.1:50051",
            &db_path,
        )
        .expect("sqlite-backed serve config");
        let service = config.build_service().expect("build sqlite-backed service");
        let append = service
            .append_event(Request::new(runtime_pb::AppendEventRequest {
                conversation_id: "conv".to_owned(),
                task_id: "task".to_owned(),
                event_type: "task.accepted".to_owned(),
                payload_json: b"{}".to_vec(),
                idempotency: Some(runtime_pb::Idempotency {
                    key: "event-1".to_owned(),
                    owner: "python-runtime".to_owned(),
                    deadline_ms: 2_000,
                }),
            }))
            .await
            .expect("append")
            .into_inner();
        assert_eq!(append.cursor.expect("cursor").sequence, 1);
    }

    let reopened =
        RuntimeSidecarServeConfig::from_listen_addr_with_sqlite_path("127.0.0.1:50051", &db_path)
            .expect("reopen sqlite-backed serve config")
            .build_service()
            .expect("rebuild sqlite-backed service");
    let replay = reopened
        .replay_events(Request::new(runtime_pb::ReplayEventsRequest {
            conversation_id: "conv".to_owned(),
            task_id: "task".to_owned(),
            after_sequence: 0,
            page_limit: 10,
            byte_limit: 1024,
        }))
        .await
        .expect("replay")
        .into_inner();
    assert_eq!(replay.cursors.len(), 1);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn serve_config_rejects_public_bind_without_mtls_support() {
    let config = RuntimeSidecarServeConfig::from_listen_addr("127.0.0.1:50051")
        .expect("loopback bind is allowed");
    assert_eq!(config.listen_addr.to_string(), "127.0.0.1:50051");

    let error = RuntimeSidecarServeConfig::from_listen_addr("0.0.0.0:50051")
        .expect_err("public bind must fail closed until mTLS is implemented");
    assert_eq!(error.code, "runtime_store_config_untrusted");
}

#[test]
fn serve_config_accepts_public_bind_only_with_complete_mtls_paths() {
    let cert_path = temp_db_path("runtime-sidecar-server").with_extension("crt");
    let key_path = temp_db_path("runtime-sidecar-server").with_extension("key");
    let client_ca_path = temp_db_path("runtime-sidecar-ca").with_extension("crt");

    let config = RuntimeSidecarServeConfig::from_listen_addr_with_mtls_paths(
        "0.0.0.0:50051",
        &cert_path,
        &key_path,
        &client_ca_path,
    )
    .expect("public TCP bind is allowed only when complete mTLS config is present");

    assert_eq!(config.listen_addr.to_string(), "0.0.0.0:50051");
    let tls = config
        .tls_config
        .as_ref()
        .expect("mTLS config is recorded for the server");
    assert_eq!(tls.identity_cert_path, cert_path);
    assert_eq!(tls.identity_key_path, key_path);
    assert_eq!(tls.client_ca_path, client_ca_path);

    let error = RuntimeSidecarServeConfig::from_listen_addr_with_mtls_paths(
        "unix:///tmp/runtime-sidecar.sock",
        &tls.identity_cert_path,
        &tls.identity_key_path,
        &tls.client_ca_path,
    )
    .expect_err("Unix sockets must not accept redundant mTLS config");
    assert_eq!(error.code, "runtime_store_config_untrusted");

    let error = RuntimeSidecarServeConfig::from_listen_addr_with_mtls_paths(
        "127.0.0.1:50051",
        "relative-server.crt",
        &tls.identity_key_path,
        &tls.client_ca_path,
    )
    .expect_err("mTLS material must come from absolute deployment/secret paths");
    assert_eq!(error.code, "runtime_store_config_untrusted");
}

#[cfg(unix)]
#[test]
fn serve_config_accepts_unix_socket_endpoint_for_internal_runtime_access() {
    let socket_path = temp_db_path("runtime-sidecar").with_extension("sock");
    let endpoint = format!("unix://{}", socket_path.display());

    let config = RuntimeSidecarServeConfig::from_listen_addr(&endpoint)
        .expect("unix socket endpoint is an allowed internal runtime sidecar endpoint");

    assert_eq!(
        config.unix_socket_path.as_deref(),
        Some(socket_path.as_path())
    );
    assert!(config.listen_addr.ip().is_loopback());
}

#[tokio::test]
async fn sidecar_listener_accepts_generated_grpc_client_on_loopback() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback listener");
    let addr = listener.local_addr().expect("listener addr");
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);
    let service = RuntimeSidecarGrpcService::new();
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();

    let server = tokio::spawn(async move {
        serve_runtime_sidecar_with_incoming(service, incoming, async {
            let _ = shutdown_rx.await;
        })
        .await
        .expect("serve runtime sidecar")
    });

    let endpoint = format!("http://{addr}");
    let mut client = runtime_pb::runtime_sidecar_client::RuntimeSidecarClient::connect(endpoint)
        .await
        .expect("connect generated client");
    let response = client
        .version(runtime_pb::VersionRequest {})
        .await
        .expect("version")
        .into_inner();
    assert_eq!(
        response.version.expect("version info").component,
        COMPONENT_ID
    );

    let _ = shutdown_tx.send(());
    server.await.expect("server task");
}

#[tokio::test]
async fn tonic_transport_round_trips_near_fifty_mib_escaped_multibyte_admission() {
    let request = large_pb_admit_request();
    assert!(request.encoded_len() > 49 * 1024 * 1024);
    assert!(request.encoded_len() < GRPC_MAX_MESSAGE_BYTES);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback listener");
    let addr = listener.local_addr().expect("listener addr");
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);
    let service =
        RuntimeSidecarGrpcService::new_with_finalized_submission_authority("f".repeat(64));
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let server = tokio::spawn(async move {
        serve_runtime_sidecar_with_incoming(service, incoming, async {
            let _ = shutdown_rx.await;
        })
        .await
        .expect("serve large-message runtime sidecar")
    });
    let endpoint = format!("http://{addr}");
    let channel = tonic::transport::Endpoint::from_shared(endpoint)
        .expect("large-message endpoint")
        .connect()
        .await
        .expect("connect large-message client");
    let mut client = runtime_pb::runtime_sidecar_client::RuntimeSidecarClient::new(channel)
        .max_encoding_message_size(GRPC_MAX_MESSAGE_BYTES)
        .max_decoding_message_size(GRPC_MAX_MESSAGE_BYTES);
    let response = client
        .admit_submission(request)
        .await
        .expect("near-50MiB admission transport roundtrip")
        .into_inner();
    assert_eq!(
        response.disposition,
        runtime_pb::SubmissionAdmissionDisposition::Created as i32
    );
    assert!(response.error.is_none());
    assert!(
        response
            .admission
            .expect("large admission")
            .message_projection_json
            .len()
            > 49 * 1024 * 1024
    );
    let _ = shutdown_tx.send(());
    server.await.expect("large-message server task");
}

#[cfg(unix)]
#[tokio::test]
async fn semantic_probe_replaces_stale_socket_and_verifies_readiness() {
    let socket_path = temp_socket_path("probe");
    let stale = std::os::unix::net::UnixListener::bind(&socket_path)
        .expect("create stale runtime sidecar socket");
    drop(stale);

    let service = RuntimeSidecarGrpcService::new();
    let served_path = socket_path.clone();
    let server = tokio::spawn(async move {
        serve_runtime_sidecar_unix_socket(&served_path, service)
            .await
            .expect("serve runtime sidecar over Unix socket")
    });

    let mut probe_error = None;
    for _ in 0..50 {
        match semantic_probe_runtime_sidecar_unix_socket(&socket_path).await {
            Ok(()) => {
                probe_error = None;
                break;
            }
            Err(error) => {
                probe_error = Some(error.to_string());
                tokio::task::yield_now().await;
            }
        }
    }
    assert!(
        probe_error.is_none(),
        "semantic probe failed: {probe_error:?}"
    );

    server.abort();
    let _ = server.await;
    let _ = std::fs::remove_file(socket_path);
}

#[cfg(unix)]
#[tokio::test]
async fn unix_listener_rejects_non_socket_collision() {
    let socket_path = temp_socket_path("collision");
    std::fs::write(&socket_path, b"not a socket").expect("write collision file");

    let error = serve_runtime_sidecar_unix_socket(&socket_path, RuntimeSidecarGrpcService::new())
        .await
        .expect_err("non-socket collision must fail closed");
    assert!(error.to_string().contains("is not a socket"));

    let _ = std::fs::remove_file(socket_path);
}

fn temp_db_path(test_name: &str) -> PathBuf {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock")
        .as_nanos();
    let unique = TEMP_PATH_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut path = std::env::temp_dir();
    path.push(format!(
        "maf-runtime-sidecar-{test_name}-{}-{timestamp}-{unique}.sqlite",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    path
}

fn temp_socket_path(test_name: &str) -> PathBuf {
    let unique = TEMP_PATH_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut path = std::env::temp_dir();
    path.push(format!(
        "maf-rs-{test_name}-{}-{unique}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    path
}

fn assert_runtime_unavailable(error: Option<common_pb::TypedError>) {
    let error = error.expect("typed error");
    assert_eq!(error.code, "runtime_store_unavailable");
    assert_eq!(error.category, common_pb::ErrorCategory::Internal as i32);
    assert!(error.retriable);
}

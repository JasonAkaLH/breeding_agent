use maf_runtime_sidecar::{
    AcknowledgeSubmissionHandoffRequest, AcknowledgeSubmissionProjectionRequest,
    AdmitSubmissionRequest, AgentItemRecord, AgentRunRecord, AppendEventRequest,
    BundleRevisionResult, COMPONENT_ID, ClaimPendingSubmissionRequest,
    CloseConversationAdmissionRequest, CommitAgentStateRequest, CompatibilityCheck,
    CompatibilityCheckRequest, ConversationAdmissionCloseDisposition,
    GetSubmissionPreparationRequest, HealthState, Idempotency, MessageIdentityDisposition,
    MessageIdentityKind, MessageIdentityRecord, PROTOCOL_VERSION, PrepareSubmissionHandoffRequest,
    ReadinessState, RenewSubmissionClaimRequest, ReplayEventsRequest,
    ReserveMessageIdentityRequest, RuntimeSidecarKernel, RuntimeSidecarService,
    SUBMISSION_CONTINUATION_MAX_BYTES, SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES,
    SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES, SUBMISSION_PREPARED_EXECUTION_MAX_BYTES,
    SubmissionAdmissionDisposition, SubmissionHandoffState, SubmissionPreparationState,
    SubmissionProjectionState, TaskNodeRecord, TaskRecord, TaskRouteAssignment,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};

fn domain_digest(prefix: &[u8], parts: &[&[u8]]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(prefix);
    for part in parts {
        hasher.update(part);
    }
    format!("{:x}", hasher.finalize())
}

fn mutate_continuation(
    request: &mut AdmitSubmissionRequest,
    mutate: impl FnOnce(&mut serde_json::Value),
) {
    let mut continuation: serde_json::Value =
        serde_json::from_slice(&request.continuation_json).expect("continuation");
    mutate(&mut continuation);
    request.continuation_json = canonical(continuation);
    request.continuation_sha256 = domain_digest(
        b"maf.submission.continuation.v1\0",
        &[&request.continuation_json],
    );
}

#[test]
fn submission_nested_contract_rejects_schema_type_sorting_forbidden_and_binding_drift() {
    let mut kernel = RuntimeSidecarKernel::new_with_finalized_submission_authority("f".repeat(64));
    let mut schema = admission_request("schema-message", "schema-task", "schema-conv");
    mutate_continuation(&mut schema, |value| {
        value["schema"] = serde_json::Value::String("unknown".to_owned());
    });
    let mut typed = admission_request("typed-message", "typed-task", "typed-conv");
    mutate_continuation(&mut typed, |value| {
        value["model_options"]["thinking_enabled"] = serde_json::Value::String("false".to_owned());
    });
    let mut forbidden = admission_request("secret-message", "secret-task", "secret-conv");
    mutate_continuation(&mut forbidden, |value| {
        value["execution_metadata"]["api_key"] = serde_json::Value::String("secret".to_owned());
    });
    let mut unsorted = admission_request("sort-message", "sort-task", "sort-conv");
    mutate_continuation(&mut unsorted, |value| {
        value["upload_refs"] = serde_json::json!([
            {"upload_id":"z","conversation_id":"sort-conv","sha256":"a".repeat(64),"size_bytes":1,"selected_sheet":null},
            {"upload_id":"a","conversation_id":"sort-conv","sha256":"b".repeat(64),"size_bytes":1,"selected_sheet":null}
        ]);
    });
    let mut binding = admission_request("bind-message", "bind-task", "bind-conv");
    mutate_continuation(&mut binding, |value| {
        value["message_content_sha256"] = serde_json::Value::String("0".repeat(64));
    });
    for request in [schema, typed, forbidden, unsorted, binding] {
        assert_eq!(
            kernel
                .admit_submission(request)
                .expect_err("nested contract drift rejected")
                .code,
            "runtime_store_write_failed"
        );
    }
    let mut blank = admission_request("blank-message", "blank-task", "blank-conv");
    let mut message: serde_json::Value =
        serde_json::from_slice(&blank.message_projection_json).expect("message");
    message["content"] = serde_json::Value::String(String::new());
    blank.message_projection_json = canonical(message);
    blank.projection_sha256 = domain_digest(
        b"maf.submission.projection.v1\0",
        &[
            &blank.conversation_projection_json,
            b"\0",
            &blank.message_projection_json,
        ],
    );
    mutate_continuation(&mut blank, |value| {
        value["message_content_sha256"] =
            serde_json::Value::String(format!("{:x}", Sha256::digest(b"")));
    });
    assert_eq!(
        kernel
            .admit_submission(blank)
            .expect("existing blank content behavior remains accepted")
            .disposition,
        SubmissionAdmissionDisposition::Created
    );
}

#[test]
fn submission_admission_replays_before_busy_and_preserves_first_canonical_task() {
    let mut kernel = RuntimeSidecarKernel::new_with_finalized_submission_authority("f".repeat(64));
    let first = kernel
        .admit_submission(admission_request("message-1", "task-1", "conv-1"))
        .expect("created admission");
    assert_eq!(first.disposition, SubmissionAdmissionDisposition::Created);
    let canonical_admission = first.admission.expect("canonical admission");
    assert_eq!(canonical_admission.idempotency_key, "submission:message-1");

    let mut replay = admission_request("message-1", "new-candidate-task", "conv-1");
    replay.message_created_at_ms = 99;
    let replayed = kernel.admit_submission(replay).expect("exact replay");
    assert_eq!(
        replayed.disposition,
        SubmissionAdmissionDisposition::IdempotentReplay
    );
    assert_eq!(
        replayed.admission.expect("replayed admission"),
        canonical_admission
    );
    let mut changed_key = admission_request("message-1", "another-task", "conv-1");
    changed_key.idempotency_key = "submission:different".to_owned();
    assert_eq!(
        kernel
            .admit_submission(changed_key)
            .expect("same Message key drift is a closed conflict")
            .disposition,
        SubmissionAdmissionDisposition::MessageIdConflict
    );
    let mut reused_key = admission_request("message-key-reuse", "task-key-reuse", "conv-key-reuse");
    reused_key.idempotency_key = "submission:message-1".to_owned();
    assert_eq!(
        kernel
            .admit_submission(reused_key)
            .expect_err("different Message cannot reuse an admission key")
            .code,
        "runtime_store_idempotency_conflict"
    );

    let busy = kernel
        .admit_submission(admission_request("message-2", "task-2", "conv-1"))
        .expect("closed busy result");
    assert_eq!(
        busy.disposition,
        SubmissionAdmissionDisposition::ConversationBusy
    );

    let mut conflict = admission_request("message-1", "task-3", "conv-1");
    conflict.request_fingerprint = "d".repeat(64);
    let mut continuation: serde_json::Value =
        serde_json::from_slice(&conflict.continuation_json).expect("continuation");
    continuation["request_fingerprint"] = serde_json::Value::String("d".repeat(64));
    conflict.continuation_json = canonical(continuation);
    conflict.continuation_sha256 = domain_digest(
        b"maf.submission.continuation.v1\0",
        &[&conflict.continuation_json],
    );
    let conflict = kernel
        .admit_submission(conflict)
        .expect("closed conflict result");
    assert_eq!(
        conflict.disposition,
        SubmissionAdmissionDisposition::MessageIdConflict
    );

    let closed = kernel
        .close_conversation_admission(CloseConversationAdmissionRequest {
            username: "owner".to_owned(),
            conversation_id: "conv-1".to_owned(),
            operation_id: "close-conv-1".to_owned(),
            now_ms: 20,
        })
        .expect("close conversation");
    assert_eq!(
        closed.disposition,
        ConversationAdmissionCloseDisposition::Closed
    );
    let cancelled = kernel.get_task("task-1").expect("cancelled pending Task");
    assert_eq!(cancelled.status, "cancelled");
    assert_eq!(cancelled.cancel_requested_at.as_deref(), Some("20"));
    let mut message_type_conflict = admission_request("message-1", "task-4", "conv-1");
    let mut message: serde_json::Value =
        serde_json::from_slice(&message_type_conflict.message_projection_json).expect("message");
    message["message_type"] = serde_json::Value::String("different".to_owned());
    message_type_conflict.message_projection_json = canonical(message);
    message_type_conflict.projection_sha256 = domain_digest(
        b"maf.submission.projection.v1\0",
        &[
            &message_type_conflict.conversation_projection_json,
            b"\0",
            &message_type_conflict.message_projection_json,
        ],
    );
    assert_eq!(
        kernel
            .admit_submission(message_type_conflict)
            .expect("global Message conflict before unavailable guard")
            .disposition,
        SubmissionAdmissionDisposition::MessageIdConflict
    );
}

#[test]
fn submission_claim_projection_preparation_and_handoff_are_cas_closed() {
    let mut kernel = RuntimeSidecarKernel::new_with_finalized_submission_authority("f".repeat(64));
    let created = kernel
        .admit_submission(admission_request("message-flow", "task-flow", "conv-flow"))
        .expect("created");
    let first_claim = created.claim.expect("created claim");
    assert_eq!(
        kernel
            .renew_submission_claim(RenewSubmissionClaimRequest {
                message_id: "message-flow".to_owned(),
                workflow_owner: "worker-b".to_owned(),
                claim_token: first_claim.token,
                now_ms: 20,
                claim_ttl_ms: 100,
            })
            .expect_err("wrong owner rejected")
            .code,
        "runtime_store_idempotency_conflict"
    );
    let claimed = kernel
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "worker-b".to_owned(),
            now_ms: 111,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .expect("take over expired claim");
    assert!(claimed.found);
    assert_eq!(claimed.finalization_receipt_sha256, Some("f".repeat(64)));
    let claim = claimed.claim.expect("recovery claim");
    let admission = claimed.admission.expect("claimed admission");
    kernel
        .acknowledge_submission_projection(AcknowledgeSubmissionProjectionRequest {
            message_id: "message-flow".to_owned(),
            workflow_owner: claim.owner.clone(),
            claim_token: claim.token.clone(),
            projection_sha256: admission.projection_sha256,
            expected_state: SubmissionProjectionState::Pending,
            now_ms: 112,
        })
        .expect("projection ack");
    let prepared = prepared_execution("task-flow", "conv-flow", "message-flow");
    let prepared_digest = domain_digest(b"maf.submission.prepared_execution.v1\0", &[&prepared]);
    let prepared_result = kernel
        .prepare_submission_handoff(PrepareSubmissionHandoffRequest {
            message_id: "message-flow".to_owned(),
            workflow_owner: claim.owner.clone(),
            claim_token: claim.token.clone(),
            prepared_execution_json: prepared.clone(),
            prepared_execution_sha256: prepared_digest.clone(),
            expected_state: SubmissionPreparationState::Pending,
            now_ms: 113,
        })
        .expect("prepared");
    assert!(!prepared_result.1);
    assert!(
        kernel
            .get_submission_preparation(&GetSubmissionPreparationRequest {
                username: "owner".to_owned(),
                conversation_id: "conv-flow".to_owned(),
                task_id: "task-flow".to_owned(),
            })
            .is_some()
    );
    let handoff_request = AcknowledgeSubmissionHandoffRequest {
        message_id: "message-flow".to_owned(),
        workflow_owner: claim.owner,
        claim_token: claim.token,
        prepared_execution_sha256: prepared_digest,
        handoff_kind: "agent_run".to_owned(),
        handoff_identity: "agent-run:task-flow".to_owned(),
        expected_state: SubmissionHandoffState::Pending,
        now_ms: 114,
    };
    let handed_off = kernel
        .acknowledge_submission_handoff(handoff_request.clone())
        .expect("handoff ack");
    assert_eq!(
        handed_off.0.handoff_state,
        SubmissionHandoffState::HandedOff
    );
    assert!(
        kernel
            .acknowledge_submission_handoff(handoff_request.clone())
            .expect("ack-loss exact retry with retained claim")
            .1
    );
    let mut wrong_owner = handoff_request.clone();
    wrong_owner.workflow_owner = "worker-c".to_owned();
    assert_eq!(
        kernel
            .acknowledge_submission_handoff(wrong_owner)
            .expect_err("handoff duplicate cannot bypass claim owner")
            .code,
        "runtime_store_idempotency_conflict"
    );
    let mut wrong_token = handoff_request.clone();
    wrong_token.claim_token = "wrong-token".to_owned();
    assert_eq!(
        kernel
            .acknowledge_submission_handoff(wrong_token)
            .expect_err("handoff duplicate cannot bypass claim token")
            .code,
        "runtime_store_idempotency_conflict"
    );
    assert_eq!(
        kernel
            .renew_submission_claim(RenewSubmissionClaimRequest {
                message_id: handoff_request.message_id.clone(),
                workflow_owner: handoff_request.workflow_owner.clone(),
                claim_token: handoff_request.claim_token.clone(),
                now_ms: 115,
                claim_ttl_ms: 100,
            })
            .expect_err("handed-off claim cannot be renewed")
            .code,
        "runtime_store_idempotency_conflict"
    );
    let mut expired_ack = handoff_request.clone();
    expired_ack.now_ms = 212;
    assert_eq!(
        kernel
            .acknowledge_submission_handoff(expired_ack)
            .expect_err("expired handoff ack remains stale")
            .code,
        "runtime_store_idempotency_conflict"
    );
    let mut mismatched_handoff = handoff_request;
    mismatched_handoff.handoff_identity = "agent-run:different".to_owned();
    assert_eq!(
        kernel
            .acknowledge_submission_handoff(mismatched_handoff)
            .expect_err("ack-loss retry identity drift rejected")
            .code,
        "runtime_store_idempotency_conflict"
    );
    let empty = kernel
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "worker-c".to_owned(),
            now_ms: 300,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .expect("empty claim response");
    assert!(!empty.found);
    assert_eq!(empty.finalization_receipt_sha256, Some("f".repeat(64)));
}

#[test]
fn message_identity_reservation_and_conversation_close_are_closed() {
    let mut kernel = RuntimeSidecarKernel::new_with_finalized_submission_authority("f".repeat(64));
    let identity = MessageIdentityRecord {
        message_id: "server-message".to_owned(),
        conversation_id: "upload-conversation".to_owned(),
        username: "owner".to_owned(),
        identity_kind: MessageIdentityKind::FileVisible,
        role: Some("assistant".to_owned()),
        message_type: Some("file".to_owned()),
        message_created_at_ms: Some(1),
        task_id: None,
        request_fingerprint: None,
        reserved_at_ms: 2,
    };
    let created = kernel
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: identity.clone(),
        })
        .expect("reserve identity");
    assert_eq!(created.disposition, MessageIdentityDisposition::Created);
    let exact = kernel
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: identity.clone(),
        })
        .expect("exact reservation");
    assert_eq!(exact.disposition, MessageIdentityDisposition::ExactReplay);
    let closed = kernel
        .close_conversation_admission(CloseConversationAdmissionRequest {
            username: "owner".to_owned(),
            conversation_id: "upload-conversation".to_owned(),
            operation_id: "close-upload-conversation".to_owned(),
            now_ms: 3,
        })
        .expect("close guard");
    assert_eq!(
        closed.disposition,
        ConversationAdmissionCloseDisposition::Closed
    );
    let unavailable = kernel
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: MessageIdentityRecord {
                message_id: "late-message".to_owned(),
                ..identity
            },
        })
        .expect("closed result");
    assert_eq!(
        unavailable.disposition,
        MessageIdentityDisposition::ConversationNotAvailable
    );
}

#[test]
fn submission_resource_boundaries_and_unfinalized_authority_fail_closed() {
    assert_eq!(SUBMISSION_CONVERSATION_PROJECTION_MAX_BYTES, 64 * 1024);
    assert_eq!(SUBMISSION_MESSAGE_PROJECTION_MAX_BYTES, 64 * 1024 * 1024);
    assert_eq!(SUBMISSION_CONTINUATION_MAX_BYTES, 64 * 1024 * 1024);
    assert_eq!(SUBMISSION_PREPARED_EXECUTION_MAX_BYTES, 128 * 1024);
    let mut unfinalized = RuntimeSidecarKernel::new();
    assert_eq!(
        unfinalized
            .admit_submission(admission_request("message", "task", "conversation"))
            .expect_err("unfinalized authority blocked")
            .code,
        "runtime_store_migration_blocked"
    );
    let mut finalized =
        RuntimeSidecarKernel::new_with_finalized_submission_authority("f".repeat(64));
    let mut invalid = admission_request("message", "task", "conversation");
    invalid.continuation_sha256 = "0".repeat(64);
    assert_eq!(
        finalized
            .admit_submission(invalid)
            .expect_err("digest mismatch rejected")
            .code,
        "runtime_store_write_failed"
    );
    let mut nested_drift = admission_request("nested-message", "nested-task", "nested-conv");
    let mut continuation: serde_json::Value =
        serde_json::from_slice(&nested_drift.continuation_json).expect("continuation");
    continuation["owner_scope"] = serde_json::Value::String("other-owner".to_owned());
    nested_drift.continuation_json = canonical(continuation);
    nested_drift.continuation_sha256 = domain_digest(
        b"maf.submission.continuation.v1\0",
        &[&nested_drift.continuation_json],
    );
    assert_eq!(
        finalized
            .admit_submission(nested_drift)
            .expect_err("nested owner binding rejected")
            .code,
        "runtime_store_write_failed"
    );
}

fn canonical(value: serde_json::Value) -> Vec<u8> {
    serde_json::to_vec(&value).expect("canonical JSON")
}

fn admission_request(
    message_id: &str,
    task_id: &str,
    conversation_id: &str,
) -> AdmitSubmissionRequest {
    let fingerprint = "a".repeat(64);
    let content = "hello";
    let conversation = canonical(serde_json::json!({
        "conversation_id": conversation_id,
        "create_if_missing": true,
        "created_at": "2026-08-26T00:00:00Z",
        "current_task_id": task_id,
        "schema": "maf.submission.conversation_projection.v1",
        "status": "active",
        "updated_at": "2026-08-26T00:00:00Z",
        "username": "owner",
    }));
    let message = canonical(serde_json::json!({
        "content": content,
        "conversation_id": conversation_id,
        "message_created_at": "2026-08-26T00:00:00Z",
        "message_id": message_id,
        "message_type": "text",
        "metadata": {},
        "role": "user",
        "schema": "maf.submission.message_projection.v1",
        "stream_status": "complete",
        "task_id": task_id,
        "updated_at": "2026-08-26T00:00:00Z",
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
        "request_fingerprint": fingerprint,
        "requested_capability_id": null,
        "routing_mode": "auto",
        "schema": "maf.submission.continuation.v1",
        "sheet_selections": {},
        "task_id": task_id,
        "upload_refs": [],
    }));
    AdmitSubmissionRequest {
        message_id: message_id.to_owned(),
        task_id: task_id.to_owned(),
        conversation_id: conversation_id.to_owned(),
        username: "owner".to_owned(),
        request_fingerprint: "a".repeat(64),
        projection_sha256: domain_digest(
            b"maf.submission.projection.v1\0",
            &[&conversation, b"\0", &message],
        ),
        continuation_sha256: domain_digest(b"maf.submission.continuation.v1\0", &[&continuation]),
        conversation_projection_json: conversation,
        message_projection_json: message,
        continuation_json: continuation,
        message_created_at_ms: 10,
        workflow_owner: "worker-a".to_owned(),
        now_ms: 10,
        claim_ttl_ms: 100,
        task: TaskRecord {
            task_id: task_id.to_owned(),
            conversation_id: conversation_id.to_owned(),
            root_message_id: message_id.to_owned(),
            status: "accepted".to_owned(),
            routing_mode: "auto".to_owned(),
            requested_capability_id: None,
            summary: None,
            cancel_requested_at: None,
            created_at: Some("2026-08-26T00:00:00Z".to_owned()),
            updated_at: None,
            assignment: None,
        },
        idempotency_key: format!("submission:{message_id}"),
    }
}

fn prepared_execution(task_id: &str, conversation_id: &str, message_id: &str) -> Vec<u8> {
    canonical(serde_json::json!({
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
        "execution_text_sha256": "c".repeat(64),
        "execution_text_source": "root_message",
        "initial_required_tool_name": null,
        "mcp_assignment": null,
        "mcp_binding": null,
        "message_id": message_id,
        "model_options": {"model_edition": null, "reasoning_effort": "medium", "thinking_enabled": false},
        "owner_scope": "owner",
        "pending_context": null,
        "planned_handoff_kind": "agent_run",
        "preparation_receipt": {
            "task_id": task_id,
            "receipt_sha256": "d".repeat(64),
            "route_decision_sha256": "e".repeat(64),
            "memory_context_sha256": "f".repeat(64),
            "selector_decision_sha256": "0".repeat(64)
        },
        "prepared_kind": "agent_run",
        "requested_capability_id": null,
        "schema": "maf.submission.prepared_execution.v1",
        "sheet_selections": {},
        "task_id": task_id,
        "upload_refs": [],
    }))
}

#[derive(Deserialize)]
struct CanonicalVector {
    value: serde_json::Value,
    canonical_json: String,
    size_bytes: usize,
    sha256: String,
}

#[test]
fn shared_python_rust_agent_payload_vectors_match() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/agent_payload_vectors.json");
    let vectors: Vec<CanonicalVector> = serde_json::from_str(
        &std::fs::read_to_string(path).expect("read shared Agent payload vectors"),
    )
    .expect("decode vectors");
    for vector in vectors {
        let mut canonical = serde_json::to_vec(&vector.value).expect("canonicalize JSON");
        canonical.push(b'\n');
        assert_eq!(canonical, vector.canonical_json.as_bytes());
        assert_eq!(canonical.len(), vector.size_bytes);
        assert_eq!(format!("{:x}", Sha256::digest(&canonical)), vector.sha256);
    }
}

fn agent_run(revision: u64) -> AgentRunRecord {
    AgentRunRecord {
        run_id: "run-agent".to_owned(),
        task_id: "task-agent".to_owned(),
        conversation_id: "conv-agent".to_owned(),
        status: "running".to_owned(),
        model_edition: "edition".to_owned(),
        reasoning_effort: "minimal".to_owned(),
        thinking_enabled: false,
        binding_option_digests_json: b"{}".to_vec(),
        next_item_sequence: revision + 1,
        compacted_through_sequence: 0,
        active_sample_item_id: None,
        waiting_call_item_ids: Vec::new(),
        next_batch_call_ordinal: 0,
        claim_owner: None,
        claim_token: None,
        lease_expires_at_ms: None,
        revision,
        terminal_reason_code: None,
        created_at_ms: 1,
        updated_at_ms: revision as i64 + 1,
        terminal_at_ms: None,
    }
}

fn agent_item() -> AgentItemRecord {
    let payload = b"{\"text\":\"ok\"}\n".to_vec();
    AgentItemRecord {
        item_id: "item-agent-1".to_owned(),
        run_id: "run-agent".to_owned(),
        task_id: "task-agent".to_owned(),
        sequence: 1,
        kind: "assistant_message".to_owned(),
        state: "committed".to_owned(),
        payload_size_bytes: payload.len() as u64,
        payload_sha256: format!("{:x}", Sha256::digest(&payload)),
        payload_json: payload,
        parent_item_id: None,
        source_call_item_id: None,
        provider_sample_id: Some("sample".to_owned()),
        call_ordinal: None,
        created_at_ms: 1,
        committed_at_ms: Some(1),
    }
}

fn agent_tool_item(
    item_id: &str,
    sequence: u64,
    kind: &str,
    source_call_item_id: Option<&str>,
) -> AgentItemRecord {
    let payload = format!("{{\"id\":\"{item_id}\"}}\n").into_bytes();
    AgentItemRecord {
        item_id: item_id.to_owned(),
        run_id: "run-agent".to_owned(),
        task_id: "task-agent".to_owned(),
        sequence,
        kind: kind.to_owned(),
        state: if kind == "tool_result" {
            "reserved".to_owned()
        } else {
            "committed".to_owned()
        },
        payload_size_bytes: payload.len() as u64,
        payload_sha256: format!("{:x}", Sha256::digest(&payload)),
        payload_json: payload,
        parent_item_id: None,
        source_call_item_id: source_call_item_id.map(str::to_owned),
        provider_sample_id: None,
        call_ordinal: Some(0),
        created_at_ms: 1,
        committed_at_ms: if kind == "tool_result" { None } else { Some(1) },
    }
}

fn agent_final_projection() -> Vec<u8> {
    br#"{"event":{"event_id":"agent-event","event_type":"agent.final_output","message_id":"agent-message"},"message":{"content":"ok","conversation_id":"conv-agent","message_id":"agent-message","role":"assistant","task_id":"task-agent"},"receipt":{"assistant_item_id":"item-agent-1","artifact_id":"agent-artifact","event_id":"agent-event","message_id":"agent-message","node_id":"agent-node","receipt_id":"agent-receipt","run_id":"run-agent","task_id":"task-agent","text_sha256":"digest"}}"#.to_vec()
}

#[test]
fn agent_state_commit_is_atomic_cas_idempotent_and_ordered() {
    let mut kernel = RuntimeSidecarKernel::new();
    let create = CommitAgentStateRequest {
        operation: "create_run".to_owned(),
        run: Some(agent_run(0)),
        items: Vec::new(),
        expected_revision: 0,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: "agent-create".to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: Vec::new(),
        artifacts: Vec::new(),
        final_projection_json: None,
        task: None,
    };
    let (_, _, duplicate) = kernel
        .commit_agent_state(create.clone())
        .expect("create AgentRun");
    assert!(!duplicate);
    assert!(kernel.commit_agent_state(create).expect("exact retry").2);
    let orphan = CommitAgentStateRequest {
        operation: "commit_sample".to_owned(),
        run: Some(agent_run(1)),
        items: vec![agent_tool_item(
            "result-orphan",
            1,
            "tool_result",
            Some("missing"),
        )],
        expected_revision: 0,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: "agent-orphan".to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: Vec::new(),
        artifacts: Vec::new(),
        final_projection_json: None,
        task: None,
    };
    assert_eq!(
        kernel.commit_agent_state(orphan).unwrap_err().code,
        "runtime_store_write_failed"
    );
    let duplicate_results = CommitAgentStateRequest {
        operation: "commit_sample".to_owned(),
        run: Some(agent_run(1)),
        items: vec![
            agent_tool_item("call-one", 1, "tool_call", None),
            agent_tool_item("result-one", 2, "tool_result", Some("call-one")),
            agent_tool_item("result-two", 3, "tool_result", Some("call-one")),
        ],
        expected_revision: 0,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: "agent-duplicate-result".to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: Vec::new(),
        artifacts: Vec::new(),
        final_projection_json: None,
        task: None,
    };
    assert_eq!(
        kernel
            .commit_agent_state(duplicate_results)
            .unwrap_err()
            .code,
        "runtime_store_write_failed"
    );
    assert_eq!(kernel.get_agent_run("run-agent").unwrap().revision, 0);
    let mut agent_node = task_node("completed");
    agent_node.node_id = "agent-node".to_owned();
    agent_node.task_id = "task-agent".to_owned();
    agent_node.capability_id = "agent.final_output".to_owned();
    let agent_artifact = maf_runtime_sidecar::ArtifactRecord {
        artifact_id: "agent-artifact".to_owned(),
        task_id: "task-agent".to_owned(),
        producer_node_id: "agent-node".to_owned(),
        artifact_type: "json".to_owned(),
        storage_ref: "opaque://agent".to_owned(),
        summary: "safe".to_owned(),
        is_complete: true,
        created_at: "1".to_owned(),
    };
    let mut agent_task = task_record("completed");
    agent_task.task_id = "task-agent".to_owned();
    agent_task.conversation_id = "conv-agent".to_owned();
    let mut final_run = agent_run(1);
    final_run.status = "completed".to_owned();
    final_run.terminal_at_ms = Some(2);
    let commit = CommitAgentStateRequest {
        operation: "commit_final".to_owned(),
        run: Some(final_run),
        items: vec![agent_item()],
        expected_revision: 0,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: "agent-sample".to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: vec![agent_node.clone()],
        artifacts: vec![agent_artifact.clone()],
        final_projection_json: Some(agent_final_projection()),
        task: Some(agent_task.clone()),
    };
    kernel.commit_agent_state(commit).expect("commit sample");
    assert_eq!(kernel.get_agent_run("run-agent").unwrap().revision, 1);
    assert_eq!(kernel.list_agent_items("run-agent"), vec![agent_item()]);
    assert_eq!(kernel.get_task_node("agent-node"), Some(agent_node));
    assert_eq!(kernel.get_artifact("agent-artifact"), Some(agent_artifact));
    assert_eq!(kernel.get_task("task-agent"), Some(agent_task));
    assert_eq!(
        kernel.get_agent_final_projection("run-agent"),
        Some(agent_final_projection())
    );
    let stale = CommitAgentStateRequest {
        operation: "commit_outcome".to_owned(),
        run: Some(agent_run(2)),
        items: Vec::new(),
        expected_revision: 0,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: "agent-stale".to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: Vec::new(),
        artifacts: Vec::new(),
        final_projection_json: None,
        task: None,
    };
    assert_eq!(
        kernel.commit_agent_state(stale).unwrap_err().code,
        "runtime_store_write_failed"
    );
}

fn task_node(status: &str) -> TaskNodeRecord {
    TaskNodeRecord {
        node_id: "node-authority".to_owned(),
        task_id: "task-authority".to_owned(),
        capability_id: "main_agent.respond".to_owned(),
        assigned_instance_id: None,
        status: status.to_owned(),
        input_refs: Vec::new(),
        output_refs: Vec::new(),
        started_at: None,
        finished_at: None,
    }
}

fn task_record(status: &str) -> TaskRecord {
    TaskRecord {
        task_id: "task-authority".to_owned(),
        conversation_id: "conv".to_owned(),
        root_message_id: "message".to_owned(),
        status: status.to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: None,
        cancel_requested_at: None,
        created_at: Some("2026-08-12T00:00:00Z".to_owned()),
        updated_at: None,
        assignment: Some(TaskRouteAssignment {
            route_mode: "shadow".to_owned(),
            real_path: "legacy".to_owned(),
            shadow_path: "user_scoped".to_owned(),
            config_version: "config-v1".to_owned(),
            reason_code: "shadow_sample".to_owned(),
            cohort_id: Some("internal".to_owned()),
            assignment_key_hash: Some("sha256:assignment".to_owned()),
            assigned_at: Some("2026-08-12T00:00:00Z".to_owned()),
        }),
    }
}

#[test]
fn task_record_is_authoritative_idempotent_and_assignment_is_write_once() {
    let mut kernel = RuntimeSidecarKernel::new();
    let accepted = task_record("accepted");
    let (stored, duplicate) = kernel
        .submit_task_record(accepted.clone(), "task-key-1", None)
        .expect("submit complete TaskRecord");
    assert_eq!(stored, accepted);
    assert!(!duplicate);
    assert_eq!(kernel.get_task("task-authority"), Some(accepted.clone()));

    let (_, duplicate) = kernel
        .submit_task_record(accepted.clone(), "task-key-1", Some("accepted"))
        .expect("same key and payload retries");
    assert!(duplicate);
    let conflict = kernel
        .submit_task_record(task_record("running"), "task-key-1", Some("accepted"))
        .expect_err("same key with changed payload conflicts");
    assert_eq!(conflict.code, "runtime_store_idempotency_conflict");
    assert!(!conflict.retriable);

    let mut running = task_record("running");
    let (_, duplicate) = kernel
        .submit_task_record(running.clone(), "task-key-2", Some("accepted"))
        .expect("legal status advance");
    assert!(!duplicate);
    running.assignment.as_mut().unwrap().config_version = "changed".to_owned();
    let assignment_conflict = kernel
        .submit_task_record(running, "task-key-3", Some("running"))
        .expect_err("assignment mutation conflicts");
    assert_eq!(
        assignment_conflict.code,
        "runtime_store_idempotency_conflict"
    );
}

#[test]
fn legacy_null_assignment_requires_an_explicit_migration() {
    let mut kernel = RuntimeSidecarKernel::new();
    let mut legacy = task_record("completed");
    legacy.assignment = None;
    kernel
        .submit_task_record(legacy.clone(), "legacy-null", None)
        .expect("store legacy null assignment");
    assert_eq!(kernel.get_task("task-authority"), Some(legacy));

    let error = kernel
        .submit_task_record(task_record("running"), "legacy-upgrade", Some("completed"))
        .expect_err("legacy assignment upgrade must fail closed");
    assert_eq!(error.code, "runtime_store_migration_blocked");
}

#[test]
fn task_node_identity_and_terminal_status_are_immutable() {
    let mut kernel = RuntimeSidecarKernel::new();
    kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "completed",
            "",
            "node-complete",
            Some(task_node("completed")),
        )
        .expect("store completed node");

    for (key, mut replacement) in [
        ("node-task-change", task_node("completed")),
        ("node-capability-change", task_node("completed")),
        ("node-reopen", task_node("running")),
    ] {
        if key == "node-task-change" {
            replacement.task_id = "other-task".to_owned();
        } else if key == "node-capability-change" {
            replacement.capability_id = "skill.other".to_owned();
        }
        let task_id = replacement.task_id.clone();
        let status = replacement.status.clone();
        let error = kernel
            .transition_node(
                &task_id,
                "node-authority",
                &status,
                "completed",
                key,
                Some(replacement),
            )
            .expect_err("invalid TaskNode replacement must fail");
        assert_eq!(error.code, "runtime_store_write_failed");
    }
}

#[test]
fn node_transition_exact_retry_returns_the_original_result_before_cas_validation() {
    let mut kernel = RuntimeSidecarKernel::new();
    let request_node = task_node("running");
    let first = kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "running",
            "",
            "node-response-lost",
            Some(request_node.clone()),
        )
        .expect("first transition succeeds before its response is lost");

    let retried = kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "running",
            "",
            "node-response-lost",
            Some(request_node.clone()),
        )
        .expect("exact retry returns the durable idempotency receipt");

    assert_eq!(retried, first);
    assert_eq!(kernel.get_task_node("node-authority"), Some(request_node));
}

#[test]
fn node_transition_idempotency_key_rejects_request_or_node_drift() {
    let mut kernel = RuntimeSidecarKernel::new();
    kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "running",
            "",
            "node-shared-key",
            Some(task_node("running")),
        )
        .expect("store original transition receipt");

    let expected_status_drift = kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "running",
            "ready",
            "node-shared-key",
            Some(task_node("running")),
        )
        .expect_err("same key cannot be reused with a changed request");
    assert_eq!(
        expected_status_drift.code,
        "runtime_store_idempotency_conflict"
    );

    let mut other_node = task_node("running");
    other_node.node_id = "node-other".to_owned();
    let node_drift = kernel
        .transition_node(
            "task-authority",
            "node-other",
            "running",
            "",
            "node-shared-key",
            Some(other_node),
        )
        .expect_err("same key cannot borrow a receipt from another node");
    assert_eq!(node_drift.code, "runtime_store_idempotency_conflict");
    assert_eq!(kernel.get_task_node("node-other"), None);
}

#[test]
fn rollout_assignment_accepts_enforce_fallback_and_unavailable_paths() {
    for (index, real_path) in ["legacy", "user_scoped", "unavailable"]
        .into_iter()
        .enumerate()
    {
        let mut task = task_record("accepted");
        task.task_id = format!("task-enforce-{index}");
        let assignment = task.assignment.as_mut().expect("assignment");
        assignment.route_mode = "enforce".to_owned();
        assignment.real_path = real_path.to_owned();
        assignment.shadow_path = "none".to_owned();

        RuntimeSidecarKernel::new()
            .submit_task_record(task, format!("key-{index}"), None)
            .expect("enforce assignment path is valid");
    }
}

#[test]
fn task_record_status_updates_do_not_requeue_the_same_task() {
    let mut kernel = RuntimeSidecarKernel::new();
    kernel
        .submit_task_record(task_record("accepted"), "task-status-accepted", None)
        .expect("initial task record");
    kernel
        .submit_task_record(
            task_record("running"),
            "task-status-running",
            Some("accepted"),
        )
        .expect("status update must not requeue");

    for index in 0..1023 {
        kernel
            .submit_task(
                format!("filler-{index}"),
                "conv",
                format!("filler-key-{index}"),
            )
            .expect("only one queue slot is consumed by the updated task");
    }
}

#[test]
fn stale_task_and_node_expected_statuses_do_not_mutate_authoritative_state() {
    let mut kernel = RuntimeSidecarKernel::new();
    kernel
        .submit_task_record(task_record("running"), "task-running", None)
        .expect("store running task");
    kernel
        .transition_node(
            "task-authority",
            "node-authority",
            "running",
            "",
            "node-running",
            Some(task_node("running")),
        )
        .expect("store running node");

    assert_eq!(
        kernel
            .submit_task_record(task_record("completed"), "task-stale", Some("planning"),)
            .expect_err("stale task CAS must fail")
            .code,
        "runtime_store_idempotency_conflict"
    );
    assert_eq!(
        kernel
            .transition_node(
                "task-authority",
                "node-authority",
                "completed",
                "ready",
                "node-stale",
                Some(task_node("completed")),
            )
            .expect_err("stale node CAS must fail")
            .code,
        "runtime_store_idempotency_conflict"
    );
    assert_eq!(
        kernel.get_task("task-authority"),
        Some(task_record("running"))
    );
    assert_eq!(
        kernel.get_task_node("node-authority"),
        Some(task_node("running"))
    );
}
use maf_runtime_store::{
    ERROR_CODE_TABLE_HASH, FEATURE_EVENT_LOG, FEATURE_RUNTIME_STORE, FEATURE_TASK_DISPATCHER,
    SCHEMA_HASH,
};

fn valid_compatibility_check() -> CompatibilityCheck {
    CompatibilityCheck {
        expected_component: COMPONENT_ID.to_owned(),
        expected_protocol_version: PROTOCOL_VERSION.to_owned(),
        expected_schema_hash: SCHEMA_HASH.to_owned(),
        expected_error_code_table_hash: ERROR_CODE_TABLE_HASH.to_owned(),
        required_features: vec![
            FEATURE_RUNTIME_STORE.to_owned(),
            FEATURE_EVENT_LOG.to_owned(),
            FEATURE_TASK_DISPATCHER.to_owned(),
        ],
    }
}

#[test]
fn version_and_compatibility_are_owned_by_rust_kernel() {
    let kernel = RuntimeSidecarKernel::new();
    let version = kernel.version();
    assert_eq!(version.component, COMPONENT_ID);
    assert_eq!(version.protocol_version, PROTOCOL_VERSION);
    assert_eq!(version.schema_hash, SCHEMA_HASH);
    assert_eq!(version.error_code_table_hash, ERROR_CODE_TABLE_HASH);
    assert!(
        version
            .supported_features
            .contains(&FEATURE_RUNTIME_STORE.to_owned())
    );

    let compatible = kernel
        .check_compatibility(valid_compatibility_check())
        .expect("valid compatibility handshake");
    assert!(compatible.compatible);
    assert!(compatible.missing_features.is_empty());

    let mut invalid = valid_compatibility_check();
    invalid.required_features.push("unknown_feature".to_owned());
    let error = kernel
        .check_compatibility(invalid)
        .expect_err("missing feature must fail closed");
    assert_eq!(error.code, "runtime_store_protocol_incompatible");
    assert_eq!(
        error.safe_metadata.get("missing_features"),
        Some(&"unknown_feature".to_owned())
    );
}

#[test]
fn append_event_is_idempotent_and_replay_uses_single_cursor_semantics() {
    let mut kernel = RuntimeSidecarKernel::new();
    let first = kernel
        .append_event(
            "conv",
            "task",
            "task.accepted",
            b"{\"ok\":true}".to_vec(),
            10,
            "event-1",
        )
        .expect("first append");
    let duplicate = kernel
        .append_event(
            "conv",
            "task",
            "task.accepted",
            b"{\"changed\":true}".to_vec(),
            11,
            "event-1",
        )
        .expect("duplicate append");
    let second = kernel
        .append_event(
            "conv",
            "task",
            "task.running",
            b"{}".to_vec(),
            12,
            "event-2",
        )
        .expect("second append");

    assert_eq!(first.sequence, 1);
    assert_eq!(duplicate, first);
    assert_eq!(second.sequence, 2);

    let replayed = kernel
        .replay_events("conv", "task", first.sequence, 1_000, 1024)
        .expect("replay");
    assert_eq!(replayed, vec![second]);
}

#[test]
fn write_paths_require_idempotency_key_and_do_not_mutate_state_on_reject() {
    let mut kernel = RuntimeSidecarKernel::new();
    let error = kernel
        .append_event("conv", "task", "missing-idem", b"{}".to_vec(), 1, "")
        .expect_err("missing idempotency key must fail closed");
    assert_eq!(error.code, "runtime_store_write_failed");
    assert!(
        kernel
            .replay_events("conv", "task", 0, 1_000, 1024)
            .unwrap()
            .is_empty()
    );
}

#[test]
fn lease_cancellation_and_bundle_state_are_held_by_rust_kernel() {
    let mut kernel = RuntimeSidecarKernel::new();
    let lease = kernel
        .acquire_lease("task", "owner", 100, 50, "lease-1")
        .expect("lease acquire");
    let duplicate_lease = kernel
        .acquire_lease("task", "owner", 120, 50, "lease-1")
        .expect("idempotent lease acquire");
    assert_eq!(duplicate_lease, lease);

    let renewed = kernel
        .renew_lease("task", &lease.renew_token, 130, 50)
        .expect("lease renew");
    assert_eq!(renewed.revision, 2);
    assert!(
        kernel
            .release_lease("task", &renewed.renew_token)
            .expect("release")
    );

    assert!(
        kernel
            .write_cancellation_token("task", 200, "user_requested", "terminal_noop", "cancel-1")
            .expect("cancel")
    );
    let token = kernel.cancellation_token("task").expect("stored token");
    assert_eq!(token.reason, "user_requested");
    assert_eq!(token.terminal_policy, "terminal_noop");

    let pin = kernel
        .pin_bundle_revision("task", "skill", "rev-1", "pin-1")
        .expect("pin");
    assert_eq!(
        pin,
        BundleRevisionResult {
            task_id: "task".to_owned(),
            bundle_kind: "skill".to_owned(),
            revision: "rev-1".to_owned(),
            released: false,
        }
    );
    let released = kernel
        .release_bundle_revision("task", "skill", "rev-1", 300, "release-1")
        .expect("release");
    assert!(released.released);
}

#[test]
fn readiness_requires_compatibility_and_shutdown_drain_rejects_new_writes() {
    let mut kernel = RuntimeSidecarKernel::new();
    let initial_health = kernel.health();
    assert_eq!(initial_health.state, HealthState::Serving);
    assert_eq!(initial_health.version.component, COMPONENT_ID);

    let initial_readiness = kernel.readiness();
    assert_eq!(initial_readiness.state, ReadinessState::NotReady);
    assert!(!initial_readiness.compatibility_handshake_passed);

    kernel
        .accept_compatibility_handshake(valid_compatibility_check())
        .expect("compatibility handshake");
    let ready = kernel.readiness();
    assert_eq!(ready.state, ReadinessState::Ready);
    assert!(ready.compatibility_handshake_passed);

    kernel.begin_shutdown_drain(1_000);
    let draining_health = kernel.health();
    assert_eq!(draining_health.state, HealthState::Degraded);
    assert_eq!(kernel.shutdown_drain_deadline_ms(), Some(31_000));
    let draining_readiness = kernel.readiness();
    assert_eq!(draining_readiness.state, ReadinessState::NotReady);
    assert!(draining_readiness.compatibility_handshake_passed);

    let error = kernel
        .append_event(
            "conv",
            "task",
            "during-drain",
            b"{}".to_vec(),
            1_001,
            "event-drain",
        )
        .expect_err("new write must fail while sidecar is draining");
    assert_eq!(error.code, "runtime_store_unavailable");
    assert!(
        kernel
            .replay_events("conv", "task", 0, 1_000, 1024)
            .unwrap()
            .is_empty()
    );
}

#[test]
fn service_adapter_maps_proto_shaped_requests_and_typed_error_envelopes() {
    let mut service = RuntimeSidecarService::new();
    let readiness = service.readiness();
    assert_eq!(readiness.state, ReadinessState::NotReady);
    assert!(!readiness.compatibility_handshake_passed);
    assert_eq!(readiness.version.component, COMPONENT_ID);

    let accepted = service.check_compatibility(CompatibilityCheckRequest {
        client_version: "python-client-1".to_owned(),
        check: valid_compatibility_check(),
    });
    assert!(accepted.compatible);
    assert!(accepted.error.is_none());
    assert_eq!(service.readiness().state, ReadinessState::Ready);

    let append = service.append_event(AppendEventRequest {
        conversation_id: "conv".to_owned(),
        task_id: "task".to_owned(),
        event_type: "task.accepted".to_owned(),
        payload_json: b"{}".to_vec(),
        idempotency: Some(Idempotency {
            key: "event-1".to_owned(),
            owner: "python-runtime".to_owned(),
            deadline_ms: 2_000,
        }),
        created_at_ms: 10,
    });
    assert!(append.error.is_none());
    assert_eq!(append.cursor.expect("cursor").sequence, 1);

    let replay = service.replay_events(ReplayEventsRequest {
        conversation_id: "conv".to_owned(),
        task_id: "task".to_owned(),
        after_sequence: 0,
        page_limit: 1_000,
        byte_limit: 1024,
    });
    assert!(replay.error.is_none());
    assert_eq!(replay.cursors.len(), 1);
    assert!(!replay.truncated);

    service.begin_shutdown_drain(100);
    let rejected = service.append_event(AppendEventRequest {
        conversation_id: "conv".to_owned(),
        task_id: "task".to_owned(),
        event_type: "during-drain".to_owned(),
        payload_json: b"{}".to_vec(),
        idempotency: Some(Idempotency {
            key: "event-2".to_owned(),
            owner: "python-runtime".to_owned(),
            deadline_ms: 2_000,
        }),
        created_at_ms: 101,
    });
    let error = rejected.error.expect("typed error");
    assert_eq!(error.code, "runtime_store_unavailable");
    assert_eq!(error.category, "internal");
    assert!(error.retriable);
    assert!(rejected.cursor.is_none());
}

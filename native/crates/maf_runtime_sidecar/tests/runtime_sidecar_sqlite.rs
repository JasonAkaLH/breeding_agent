use maf_runtime_sidecar::{
    AcknowledgeSubmissionHandoffRequest, AcknowledgeSubmissionProjectionRequest,
    AdmitSubmissionRequest, AgentItemRecord, AgentRunRecord, ArtifactRecord,
    ClaimPendingSubmissionRequest, CloseConversationAdmissionRequest, CommitAgentStateRequest,
    ConversationAdmissionCloseDisposition, GetSubmissionPreparationRequest, Idempotency,
    MessageIdentityKind, MessageIdentityRecord, PrepareSubmissionHandoffRequest,
    RenewSubmissionClaimRequest, ReserveMessageIdentityRequest, RuntimeSidecarSqliteAdapter,
    SubmissionAdmissionDisposition, SubmissionAuthorityImportRequest,
    SubmissionConversationImportRecord, SubmissionHandoffState, SubmissionPreparationState,
    SubmissionProjectionState, TaskNodeRecord, TaskRecord, TaskRouteAssignment,
};
use rusqlite::Connection;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static TEMP_PATH_COUNTER: AtomicU64 = AtomicU64::new(0);

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

fn agent_node() -> TaskNodeRecord {
    TaskNodeRecord {
        node_id: "agent-node".to_owned(),
        task_id: "task-agent".to_owned(),
        capability_id: "agent.final_output".to_owned(),
        assigned_instance_id: None,
        status: "completed".to_owned(),
        input_refs: vec!["item-agent-1".to_owned()],
        output_refs: vec!["agent-artifact".to_owned()],
        started_at: Some("1".to_owned()),
        finished_at: Some("1".to_owned()),
    }
}

fn agent_artifact() -> ArtifactRecord {
    ArtifactRecord {
        artifact_id: "agent-artifact".to_owned(),
        task_id: "task-agent".to_owned(),
        producer_node_id: "agent-node".to_owned(),
        artifact_type: "text".to_owned(),
        storage_ref: "opaque://agent-final".to_owned(),
        summary: "safe".to_owned(),
        is_complete: true,
        created_at: "1".to_owned(),
    }
}

fn agent_task() -> TaskRecord {
    TaskRecord {
        task_id: "task-agent".to_owned(),
        conversation_id: "conv-agent".to_owned(),
        root_message_id: "message-agent".to_owned(),
        status: "completed".to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: None,
        cancel_requested_at: None,
        created_at: Some("1".to_owned()),
        updated_at: Some("1".to_owned()),
        assignment: None,
    }
}

fn agent_final_projection() -> Vec<u8> {
    br#"{"event":{"event_id":"agent-event","event_type":"agent.final_output","message_id":"agent-message"},"message":{"content":"ok","conversation_id":"conv-agent","message_id":"agent-message","role":"assistant","task_id":"task-agent"},"receipt":{"assistant_item_id":"item-agent-1","artifact_id":"agent-artifact","event_id":"agent-event","message_id":"agent-message","node_id":"agent-node","receipt_id":"agent-receipt","run_id":"run-agent","task_id":"task-agent","text_sha256":"digest"}}"#.to_vec()
}

fn completed_agent_run() -> AgentRunRecord {
    let mut run = agent_run(1);
    run.status = "completed".to_owned();
    run.terminal_at_ms = Some(2);
    run
}

fn agent_request(
    operation: &str,
    run: AgentRunRecord,
    items: Vec<AgentItemRecord>,
    expected_revision: u64,
    key: &str,
) -> CommitAgentStateRequest {
    CommitAgentStateRequest {
        operation: operation.to_owned(),
        run: Some(run),
        items,
        expected_revision,
        expected_claim_token: None,
        idempotency: Some(Idempotency {
            key: key.to_owned(),
            owner: "test".to_owned(),
            deadline_ms: 0,
        }),
        task_nodes: Vec::new(),
        artifacts: Vec::new(),
        final_projection_json: None,
        task: None,
    }
}

#[test]
fn sqlite_adapter_persists_agent_state_atomically_across_reopen() {
    let db_path = temp_db_path("agent-state");
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open adapter");
        adapter
            .commit_agent_state(agent_request(
                "create_run",
                agent_run(0),
                Vec::new(),
                0,
                "agent-create",
            ))
            .expect("create AgentRun");
        let mut sample = agent_request(
            "commit_final",
            completed_agent_run(),
            vec![agent_item()],
            0,
            "agent-sample",
        );
        sample.task_nodes = vec![agent_node()];
        sample.artifacts = vec![agent_artifact()];
        sample.final_projection_json = Some(agent_final_projection());
        sample.task = Some(agent_task());
        adapter
            .commit_agent_state(sample)
            .expect("commit Agent sample");
    }
    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen adapter");
    assert_eq!(
        reopened
            .get_agent_run("run-agent")
            .unwrap()
            .unwrap()
            .revision,
        1
    );
    assert_eq!(
        reopened.list_agent_items("run-agent").unwrap(),
        vec![agent_item()]
    );
    assert_eq!(
        reopened.get_task_node("agent-node").unwrap(),
        Some(agent_node())
    );
    assert_eq!(
        reopened.get_artifact("agent-artifact").unwrap(),
        Some(agent_artifact())
    );
    assert_eq!(
        reopened.get_agent_final_projection("run-agent").unwrap(),
        Some(agent_final_projection())
    );
    assert_eq!(reopened.get_task("task-agent").unwrap(), Some(agent_task()));
    let stale = reopened
        .commit_agent_state(agent_request(
            "commit_outcome",
            agent_run(2),
            Vec::new(),
            0,
            "agent-stale",
        ))
        .expect_err("stale CAS rejected");
    assert_eq!(stale.code, "runtime_store_write_failed");
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_agent_state_projection_failure_rolls_back_run_item_and_node() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    adapter
        .commit_agent_state(agent_request(
            "create_run",
            agent_run(0),
            Vec::new(),
            0,
            "agent-create-fault",
        ))
        .expect("create AgentRun");
    let mut wrong_artifact = agent_artifact();
    wrong_artifact.task_id = "wrong-task".to_owned();
    let mut fault = agent_request(
        "commit_sample",
        agent_run(1),
        vec![agent_item()],
        0,
        "agent-sample-fault",
    );
    fault.task_nodes = vec![agent_node()];
    fault.artifacts = vec![wrong_artifact];
    fault.task = Some(agent_task());
    let error = adapter
        .commit_agent_state(fault)
        .expect_err("invalid projection rolls back transaction");
    assert_eq!(error.code, "runtime_store_write_failed");
    assert_eq!(
        adapter
            .get_agent_run("run-agent")
            .unwrap()
            .unwrap()
            .revision,
        0
    );
    assert!(adapter.list_agent_items("run-agent").unwrap().is_empty());
    assert_eq!(adapter.get_task_node("agent-node").unwrap(), None);
    assert_eq!(adapter.get_task("task-agent").unwrap(), None);
}

fn authority_task(status: &str) -> TaskRecord {
    TaskRecord {
        task_id: "task-authority".to_owned(),
        conversation_id: "conv".to_owned(),
        root_message_id: "message".to_owned(),
        status: status.to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        summary: None,
        cancel_requested_at: None,
        created_at: Some("created".to_owned()),
        updated_at: None,
        assignment: Some(TaskRouteAssignment {
            route_mode: "enforce".to_owned(),
            real_path: "user_scoped".to_owned(),
            shadow_path: "none".to_owned(),
            config_version: "v1".to_owned(),
            reason_code: "cohort".to_owned(),
            cohort_id: None,
            assignment_key_hash: None,
            assigned_at: Some("assigned".to_owned()),
        }),
    }
}

fn authority_node(node_id: &str, status: &str) -> TaskNodeRecord {
    TaskNodeRecord {
        node_id: node_id.to_owned(),
        task_id: "task".to_owned(),
        capability_id: "main_agent.respond".to_owned(),
        assigned_instance_id: Some("instance".to_owned()),
        status: status.to_owned(),
        input_refs: vec!["input".to_owned()],
        output_refs: vec!["output".to_owned()],
        started_at: Some("2026-08-13T10:00:00Z".to_owned()),
        finished_at: None,
    }
}

#[test]
fn sqlite_adapter_persists_complete_task_record_and_does_not_invent_legacy_rows() {
    let db_path = temp_db_path("task-authority");
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        adapter
            .submit_task("legacy", "conv", "legacy-key")
            .expect("legacy submit");
        assert_eq!(adapter.get_task("legacy").expect("legacy read"), None);
        let mut upgraded = authority_task("accepted");
        upgraded.task_id = "legacy".to_owned();
        let migration_error = adapter
            .submit_task_record(upgraded, "legacy-authority-key", None)
            .expect_err("legacy row upgrade requires an audited migration");
        assert_eq!(migration_error.code, "runtime_store_migration_blocked");
        assert_eq!(adapter.get_task("legacy").expect("legacy read"), None);
        let task = authority_task("accepted");
        let (stored, result) = adapter
            .submit_task_record(task.clone(), "key-1", None)
            .expect("submit record");
        assert_eq!(stored, Some(task.clone()));
        assert!(!result.duplicate);
        let (_, duplicate) = adapter
            .submit_task_record(task, "key-1", Some("accepted"))
            .expect("retry record");
        assert!(duplicate.duplicate);
        let conflict = adapter
            .submit_task_record(authority_task("running"), "key-1", Some("accepted"))
            .expect_err("payload conflict");
        assert_eq!(conflict.code, "runtime_store_idempotency_conflict");
    }
    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let stored = reopened
        .get_task("task-authority")
        .expect("read record")
        .expect("found");
    assert_eq!(stored.assignment.unwrap().config_version, "v1");
    assert_eq!(reopened.get_task("missing").expect("missing read"), None);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_queries_authoritative_tasks_after_reopen_with_python_ordering() {
    let db_path = temp_db_path("task-query-authority");
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        for (task_id, conversation_id, status, created_at) in [
            ("task-accepted", "conv", "accepted", "2026-08-13T10:00:00Z"),
            ("task-running", "conv", "running", "2026-08-13T11:00:00Z"),
            (
                "task-completed",
                "conv",
                "completed",
                "2026-08-13T12:00:00Z",
            ),
            ("task-other", "other", "running", "2026-08-13T13:00:00Z"),
        ] {
            let mut task = authority_task(status);
            task.task_id = task_id.to_owned();
            task.conversation_id = conversation_id.to_owned();
            task.created_at = Some(created_at.to_owned());
            adapter
                .submit_task_record(task, &format!("key-{task_id}"), None)
                .expect("submit authoritative task");
        }
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let listed = reopened
        .list_tasks_for_conversation("conv", &[])
        .expect("list tasks after reopen");
    assert_eq!(
        listed
            .iter()
            .map(|task| task.task_id.as_str())
            .collect::<Vec<_>>(),
        vec!["task-completed", "task-running", "task-accepted"]
    );
    let filtered = reopened
        .list_tasks_for_conversation("conv", &["running".to_owned(), "accepted".to_owned()])
        .expect("filter active tasks");
    assert_eq!(
        filtered
            .iter()
            .map(|task| task.task_id.as_str())
            .collect::<Vec<_>>(),
        vec!["task-running", "task-accepted"]
    );
    assert_eq!(
        reopened
            .get_active_task_for_conversation("conv")
            .expect("get active task")
            .expect("active task exists")
            .task_id,
        "task-running"
    );
    assert!(
        reopened
            .get_active_task_for_conversation("missing")
            .expect("missing active query")
            .is_none()
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_rejects_task_and_node_identity_or_terminal_rewrites() {
    let db_path = temp_db_path("authority-invariants");
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        adapter
            .submit_task_record(authority_task("completed"), "task-completed", None)
            .expect("store completed task");
        let mut reopened_task = authority_task("running");
        reopened_task.conversation_id = "other-conversation".to_owned();
        assert_eq!(
            adapter
                .submit_task_record(reopened_task, "task-rewrite", Some("completed"))
                .expect_err("Task identity and terminal status are immutable")
                .code,
            "runtime_store_write_failed"
        );

        adapter
            .transition_node(
                "task",
                "node-terminal",
                "completed",
                "",
                "node-completed",
                Some(authority_node("node-terminal", "completed")),
            )
            .expect("store completed node");
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let mut moved = authority_node("node-terminal", "completed");
    moved.task_id = "other-task".to_owned();
    assert_eq!(
        reopened
            .transition_node(
                "other-task",
                "node-terminal",
                "completed",
                "completed",
                "node-moved",
                Some(moved),
            )
            .expect_err("TaskNode cannot move between tasks")
            .code,
        "runtime_store_write_failed"
    );
    assert_eq!(
        reopened
            .transition_node(
                "task",
                "node-terminal",
                "running",
                "completed",
                "node-reopened",
                None,
            )
            .expect_err("terminal TaskNode cannot reopen without a snapshot")
            .code,
        "runtime_store_write_failed"
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_stale_expected_status_does_not_mutate_after_reopen() {
    let db_path = temp_db_path("expected-status-cas");
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        adapter
            .submit_task_record(authority_task("running"), "task-running", None)
            .expect("store running task");
        adapter
            .transition_node(
                "task",
                "node-cas",
                "running",
                "",
                "node-running",
                Some(authority_node("node-cas", "running")),
            )
            .expect("store running node");
    }
    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    assert_eq!(
        reopened
            .submit_task_record(authority_task("completed"), "task-stale", Some("planning"),)
            .expect_err("stale task CAS")
            .code,
        "runtime_store_idempotency_conflict"
    );
    assert_eq!(
        reopened
            .transition_node(
                "task",
                "node-cas",
                "completed",
                "ready",
                "node-stale",
                Some(authority_node("node-cas", "completed")),
            )
            .expect_err("stale node CAS")
            .code,
        "runtime_store_idempotency_conflict"
    );
    assert_eq!(
        reopened.get_task("task-authority").expect("task read"),
        Some(authority_task("running"))
    );
    assert_eq!(
        reopened.get_task_node("node-cas").expect("node read"),
        Some(authority_node("node-cas", "running"))
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_persists_event_replay_across_reopen() {
    let db_path = temp_db_path("event-replay");

    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        let (first, first_duplicate) = adapter
            .append_event_exact(
                "conv",
                "task",
                "task.accepted",
                b"{}".to_vec(),
                10,
                "event-1",
            )
            .expect("append event");
        let (duplicate, replay_duplicate) = adapter
            .append_event_exact(
                "conv",
                "task",
                "task.accepted",
                b"{}".to_vec(),
                10,
                "event-1",
            )
            .expect("idempotent append");
        assert_eq!(first, duplicate);
        assert!(!first_duplicate);
        assert!(replay_duplicate);
        assert_eq!(first.sequence, 1);
        for (conversation_id, task_id, event_type, payload_json, created_at_ms) in [
            ("other-conv", "task", "task.accepted", b"{}".to_vec(), 10),
            ("conv", "other-task", "task.accepted", b"{}".to_vec(), 10),
            ("conv", "task", "changed", b"{}".to_vec(), 10),
            (
                "conv",
                "task",
                "task.accepted",
                b"{\"changed\":true}".to_vec(),
                10,
            ),
            ("conv", "task", "task.accepted", b"{}".to_vec(), 11),
        ] {
            let error = adapter
                .append_event(
                    conversation_id,
                    task_id,
                    event_type,
                    payload_json,
                    created_at_ms,
                    "event-1",
                )
                .expect_err("event identity drift must conflict");
            assert_eq!(error.code, "runtime_store_idempotency_conflict");
        }
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let (restarted, duplicate) = reopened
        .append_event_exact(
            "conv",
            "task",
            "task.accepted",
            b"{}".to_vec(),
            10,
            "event-1",
        )
        .expect("exact append after reopen");
    assert_eq!(restarted.sequence, 1);
    assert!(duplicate);
    let replayed = reopened
        .replay_events("conv", "task", 0, 1_000, 1024)
        .expect("replay after reopen");
    assert_eq!(replayed.len(), 1);
    assert_eq!(replayed[0].sequence, 1);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_persists_lease_across_reopen() {
    let db_path = temp_db_path("lease");

    let lease = {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        adapter
            .acquire_lease("task", "owner", 100, 50, "lease-1")
            .expect("acquire lease")
    };
    assert_eq!(lease.revision, 1);

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let renewed = reopened
        .renew_lease("task", &lease.renew_token, 120, 50)
        .expect("renew persisted lease");
    assert_eq!(renewed.revision, 2);
    assert_eq!(renewed.expires_at_ms, 170);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_replays_original_lease_for_duplicate_acquire_key() {
    let db_path = temp_db_path("lease-idempotency");
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");

    let lease = adapter
        .acquire_lease("task", "owner", 100, 50, "lease-1")
        .expect("acquire lease");
    let renewed = adapter
        .renew_lease("task", &lease.renew_token, 120, 50)
        .expect("renew lease");
    let duplicate = adapter
        .acquire_lease("task", "owner", 125, 50, "lease-1")
        .expect("duplicate acquire");

    assert_eq!(renewed.revision, 2);
    assert_eq!(duplicate.revision, 1);
    assert_eq!(duplicate.renew_token, lease.renew_token);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_persists_task_node_cancellation_and_bundle_across_reopen() {
    let db_path = temp_db_path("task-node-cancel-bundle");

    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        let submitted = adapter
            .submit_task("task", "conv", "submit-1")
            .expect("submit task");
        assert_eq!(submitted.task_id, "task");
        assert!(!submitted.duplicate);

        let duplicate_submit = adapter
            .submit_task("changed-task", "conv", "submit-1")
            .expect("idempotent submit");
        assert_eq!(duplicate_submit.task_id, "task");
        assert!(duplicate_submit.duplicate);

        let transitioned = adapter
            .transition_node(
                "task",
                "node",
                "running",
                "",
                "node-1",
                Some(authority_node("node", "running")),
            )
            .expect("transition node");
        assert_eq!(transitioned.status, "running");

        assert!(
            adapter
                .write_cancellation_token("task", 200, "user", "terminal-noop", "cancel-1")
                .expect("write cancellation token")
        );

        let pinned = adapter
            .pin_bundle_revision("task", "skill", "rev-1", "pin-1")
            .expect("pin bundle revision");
        assert!(!pinned.released);
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let duplicate_transition = reopened
        .transition_node(
            "task",
            "node",
            "running",
            "running",
            "node-1",
            Some(authority_node("node", "running")),
        )
        .expect("idempotent node transition after reopen");
    assert_eq!(duplicate_transition.status, "running");
    assert_eq!(
        reopened
            .get_task_node("node")
            .expect("get persisted TaskNode")
            .expect("TaskNode exists"),
        authority_node("node", "running")
    );
    assert_eq!(
        reopened
            .list_task_nodes_for_task("task")
            .expect("list persisted TaskNodes"),
        vec![authority_node("node", "running")]
    );

    let cancellation = reopened
        .cancellation_token("task")
        .expect("read cancellation token")
        .expect("cancellation token persisted");
    assert_eq!(cancellation.reason, "user");
    assert_eq!(cancellation.terminal_policy, "terminal-noop");

    let released = reopened
        .release_bundle_revision("task", "skill", "rev-1", 250, "release-1")
        .expect("release persisted bundle pin");
    assert!(released.released);

    let duplicate_release = reopened
        .release_bundle_revision("task", "skill", "changed", 251, "release-1")
        .expect("idempotent bundle release after reopen");
    assert_eq!(duplicate_release.revision, "rev-1");
    assert!(duplicate_release.released);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_adapter_persists_artifacts_across_reopen() {
    let db_path = temp_db_path("artifact");

    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        let artifact = adapter
            .save_artifact(
                ArtifactRecord {
                    artifact_id: "artifact".to_owned(),
                    task_id: "task".to_owned(),
                    producer_node_id: "node-b".to_owned(),
                    artifact_type: "json".to_owned(),
                    storage_ref: "opaque://artifact".to_owned(),
                    summary: "summary".to_owned(),
                    is_complete: true,
                    created_at: "2026-05-15T00:00:00".to_owned(),
                },
                "artifact-1",
            )
            .expect("save artifact");
        assert_eq!(artifact.artifact_id, "artifact");

        let duplicate_artifact = adapter
            .save_artifact(
                ArtifactRecord {
                    artifact_id: "changed".to_owned(),
                    task_id: "task".to_owned(),
                    producer_node_id: "node-b".to_owned(),
                    artifact_type: "text".to_owned(),
                    storage_ref: "opaque://changed".to_owned(),
                    summary: "changed".to_owned(),
                    is_complete: false,
                    created_at: "".to_owned(),
                },
                "artifact-1",
            )
            .expect("idempotent artifact");
        assert_eq!(duplicate_artifact, artifact);
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
    let artifact = reopened
        .get_artifact("artifact")
        .expect("get artifact")
        .expect("artifact persisted");
    assert_eq!(artifact.storage_ref, "opaque://artifact");
    let artifacts = reopened
        .list_artifacts_for_task("task")
        .expect("list artifacts");
    assert_eq!(artifacts, vec![artifact]);
    let _ = std::fs::remove_file(db_path);
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

fn canonical(value: serde_json::Value) -> Vec<u8> {
    serde_json::to_vec(&value).expect("canonical JSON")
}

fn submission_digest(prefix: &[u8], parts: &[&[u8]]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(prefix);
    for part in parts {
        hasher.update(part);
    }
    format!("{:x}", hasher.finalize())
}

#[derive(Clone, Serialize)]
struct TestInventoryEvidence {
    count: u32,
    pk_sha256: String,
    canonical_sha256: String,
    finalize_empty: bool,
}

fn test_inventory(
    kind: &str,
    primary_keys: Vec<String>,
    records: Vec<serde_json::Value>,
) -> TestInventoryEvidence {
    let digest = |domain: &str, bytes: &[u8]| {
        let mut hasher = Sha256::new();
        hasher.update(domain.as_bytes());
        hasher.update(b"\0");
        hasher.update(bytes);
        format!("{:x}", hasher.finalize())
    };
    TestInventoryEvidence {
        count: records.len() as u32,
        pk_sha256: digest(
            &format!("maf.submission_authority.inventory.{kind}.pk.v1"),
            &serde_json::to_vec(&primary_keys).unwrap(),
        ),
        canonical_sha256: digest(
            &format!("maf.submission_authority.inventory.{kind}.records.v1"),
            &serde_json::to_vec(&records).unwrap(),
        ),
        finalize_empty: records.is_empty(),
    }
}

fn finalization_bundle(
    finalized_at_ms: i64,
    conversation_inventory: TestInventoryEvidence,
    message_inventory: TestInventoryEvidence,
    active_inventory: TestInventoryEvidence,
) -> (String, Vec<u8>, Vec<u8>) {
    let features = serde_json::to_vec(&maf_runtime_sidecar::supported_features()).unwrap();
    let subject = canonical(serde_json::json!({
        "active_task_inventory": active_inventory,
        "conversation_inventory": conversation_inventory,
        "message_identity_inventory": message_inventory,
        "proto_hash": maf_runtime_store::PROTO_HASH,
        "report_sha256": "1".repeat(64),
        "schema": "maf.submission_authority.finalization_subject.v1",
        "schema_hash": maf_runtime_store::SCHEMA_HASH,
        "snapshot_boundary_sha256": "2".repeat(64),
        "source_backend": "sqlite",
        "source_identity_sha256": "3".repeat(64),
        "supported_features_sha256": format!("{:x}", Sha256::digest(features)),
        "writer_fence_sha256": "4".repeat(64),
    }));
    let digest = submission_digest(b"maf.submission_authority.finalization.v1\0", &[&subject]);
    let subject_value: serde_json::Value = serde_json::from_slice(&subject).unwrap();
    let receipt = canonical(serde_json::json!({
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
    }));
    (digest, subject, receipt)
}

fn empty_finalization_bundle(finalized_at_ms: i64) -> (String, Vec<u8>, Vec<u8>) {
    finalization_bundle(
        finalized_at_ms,
        test_inventory("conversations", Vec::new(), Vec::new()),
        test_inventory("message_identities", Vec::new(), Vec::new()),
        test_inventory("active_tasks", Vec::new(), Vec::new()),
    )
}

fn different_empty_finalization_bundle(finalized_at_ms: i64) -> (String, Vec<u8>, Vec<u8>) {
    let (_, subject, receipt) = empty_finalization_bundle(finalized_at_ms);
    let mut subject_value: serde_json::Value = serde_json::from_slice(&subject).unwrap();
    subject_value["report_sha256"] = serde_json::json!("9".repeat(64));
    let subject = canonical(subject_value);
    let digest = submission_digest(b"maf.submission_authority.finalization.v1\0", &[&subject]);
    let mut receipt_value: serde_json::Value = serde_json::from_slice(&receipt).unwrap();
    receipt_value["finalization_receipt_sha256"] = serde_json::json!(digest);
    (digest, subject, canonical(receipt_value))
}

fn sqlite_admission_request(
    message_id: &str,
    task_id: &str,
    conversation_id: &str,
) -> AdmitSubmissionRequest {
    let content = "hello";
    let fingerprint = "a".repeat(64);
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
        request_fingerprint: fingerprint,
        projection_sha256: submission_digest(
            b"maf.submission.projection.v1\0",
            &[&conversation, b"\0", &message],
        ),
        continuation_sha256: submission_digest(
            b"maf.submission.continuation.v1\0",
            &[&continuation],
        ),
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

fn sqlite_prepared_execution(task_id: &str, conversation_id: &str, message_id: &str) -> Vec<u8> {
    canonical(serde_json::json!({
        "available_mcp_servers": [],
        "bundle_revisions": {"mcp_bundle_revision": null, "skill_bundle_revision": null},
        "conversation_id": conversation_id,
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
        "message_id": message_id,
        "model_options": {"model_edition": null, "reasoning_effort": "medium", "thinking_enabled": false},
        "owner_scope": "owner",
        "pending_context": null,
        "planned_handoff_kind": "agent_run",
        "preparation_receipt": {
            "memory_context_sha256": "f".repeat(64),
            "receipt_sha256": "d".repeat(64),
            "route_decision_sha256": "e".repeat(64),
            "selector_decision_sha256": "0".repeat(64),
            "task_id": task_id
        },
        "prepared_kind": "agent_run",
        "requested_capability_id": null,
        "schema": "maf.submission.prepared_execution.v1",
        "sheet_selections": {},
        "task_id": task_id,
        "upload_refs": [],
    }))
}

#[test]
fn sqlite_submission_authority_requires_offline_finalization_and_replays_first_receipt() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let blocked = adapter
        .admit_submission(sqlite_admission_request("message", "task", "conversation"))
        .expect_err("uninitialized authority blocks online admission");
    assert_eq!(blocked.code, "runtime_store_migration_blocked");

    let (digest, subject, receipt) = empty_finalization_bundle(7);
    let finalized = adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 7)
        .expect("finalize empty authority");
    assert!(!finalized.exact_replay);
    let (_, replay_subject, replay_receipt) = empty_finalization_bundle(99);
    let replay = adapter
        .finalize_empty_submission_authority(&digest, &replay_subject, &replay_receipt, 99)
        .expect("same digest returns the first stored receipt and time");
    assert!(replay.exact_replay);
    assert_eq!(replay.finalized_at_ms, 7);
    assert_eq!(replay.finalization_receipt_json, receipt);
    assert_eq!(
        adapter
            .finalize_empty_submission_authority(&digest, b"{}", &receipt, 7)
            .expect_err("same digest cannot bypass strict subject validation")
            .code,
        "runtime_store_write_failed"
    );
    let mut garbage_receipt = receipt.clone();
    garbage_receipt.push(b' ');
    assert_eq!(
        adapter
            .finalize_empty_submission_authority(&digest, &subject, &garbage_receipt, 7)
            .expect_err("same digest cannot bypass strict receipt validation")
            .code,
        "runtime_store_write_failed"
    );
    let exact = adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 7)
        .expect("exact finalization replay");
    assert!(exact.exact_replay);
    assert_eq!(exact.finalized_at_ms, 7);
    assert_eq!(exact.finalization_receipt_json, receipt);
    let (other_digest, other_subject, other_receipt) = different_empty_finalization_bundle(8);
    assert_eq!(
        adapter
            .finalize_empty_submission_authority(&other_digest, &other_subject, &other_receipt, 8,)
            .expect_err("different receipt conflicts")
            .code,
        "runtime_store_idempotency_conflict"
    );
}

#[test]
fn submission_finalization_shared_vector_locks_subject_digest_and_receipt() {
    let (digest, subject, receipt) = empty_finalization_bundle(7);
    assert_eq!(
        (
            digest,
            format!("{:x}", Sha256::digest(&subject)),
            format!("{:x}", Sha256::digest(&receipt)),
        ),
        (
            "48fd5ffba62e2e24fac246d40725f2cf836ce66beb33491488918db04c0c8c90".to_owned(),
            "aa53b907b800f2c0e8ea86f5a62c3000e224b13b51b34a2aa0f31ceb5d5156e5".to_owned(),
            "1d6d7be12c40d42887f5eb9095a770afc96e0da719105b40e67c90bfa43fa770".to_owned(),
        )
    );
}

#[test]
fn sqlite_submission_schema_is_additive_and_has_exact_authority_columns() {
    let db_path = temp_db_path("submission-schema");
    {
        let connection = Connection::open(&db_path).expect("open legacy database");
        connection
            .execute_batch(
                "CREATE TABLE submitted_tasks(task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL); INSERT INTO submitted_tasks(task_id, conversation_id) VALUES ('legacy', 'conversation');",
            )
            .expect("seed legacy schema");
    }
    RuntimeSidecarSqliteAdapter::open(&db_path).expect("upgrade schema");
    let connection = Connection::open(&db_path).expect("inspect schema");
    let columns = |table: &str| {
        let mut statement = connection
            .prepare(&format!("PRAGMA table_info({table})"))
            .expect("prepare table info");
        statement
            .query_map([], |row| row.get::<_, String>(1))
            .expect("query table info")
            .collect::<Result<Vec<_>, _>>()
            .expect("read table info")
    };
    assert_eq!(
        columns("submission_authority_meta"),
        vec![
            "singleton_key",
            "state",
            "finalization_receipt_sha256",
            "finalization_receipt_json",
            "finalized_at_ms",
        ]
    );
    assert_eq!(
        columns("submission_conversations"),
        vec![
            "conversation_id",
            "username",
            "status",
            "revision",
            "active_task_id",
            "close_operation_id",
            "updated_at_ms",
        ]
    );
    assert_eq!(
        columns("submission_message_identities"),
        vec![
            "message_id",
            "conversation_id",
            "username",
            "identity_kind",
            "role",
            "message_type",
            "message_created_at_ms",
            "task_id",
            "request_fingerprint",
            "reserved_at_ms",
        ]
    );
    assert_eq!(
        columns("submission_admissions"),
        vec![
            "message_id",
            "task_id",
            "conversation_id",
            "username",
            "idempotency_key",
            "request_fingerprint",
            "conversation_projection_json",
            "message_projection_json",
            "projection_sha256",
            "continuation_json",
            "continuation_sha256",
            "admission_state",
            "projection_state",
            "preparation_state",
            "prepared_execution_json",
            "prepared_execution_sha256",
            "handoff_state",
            "handoff_kind",
            "handoff_identity",
            "claim_owner",
            "claim_token",
            "claim_expires_at_ms",
            "created_at_ms",
            "updated_at_ms",
        ]
    );
    assert_eq!(
        connection
            .query_row(
                "SELECT conversation_id FROM submitted_tasks WHERE task_id='legacy'",
                [],
                |row| row.get::<_, String>(0),
            )
            .expect("legacy Task survives upgrade"),
        "conversation"
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_startup_rejects_malformed_same_name_authority_table() {
    let db_path = temp_db_path("submission-malformed-schema");
    Connection::open(&db_path)
        .unwrap()
        .execute_batch(
            "CREATE TABLE submission_authority_meta(singleton_key INTEGER PRIMARY KEY, state TEXT NOT NULL, finalization_receipt_sha256 TEXT, finalization_receipt_json BLOB, finalized_at_ms INTEGER);",
        )
        .unwrap();
    let error = RuntimeSidecarSqliteAdapter::open(&db_path)
        .expect_err("same-name table without approved CHECK manifest fails closed");
    assert_eq!(error.code, "runtime_store_migration_blocked");
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_startup_rejects_extra_check_index_and_trigger() {
    let check_path = temp_db_path("submission-extra-check");
    drop(RuntimeSidecarSqliteAdapter::open(&check_path).unwrap());
    let connection = Connection::open(&check_path).unwrap();
    connection.execute_batch("PRAGMA writable_schema=ON; UPDATE sqlite_master SET sql=substr(sql,1,length(sql)-1) || ', CHECK(1=1))' WHERE type='table' AND name='submission_conversations'; PRAGMA writable_schema=OFF;").unwrap();
    drop(connection);
    assert_eq!(
        RuntimeSidecarSqliteAdapter::open(&check_path)
            .expect_err("extra CHECK is not canonical")
            .code,
        "runtime_store_migration_blocked"
    );

    let index_path = temp_db_path("submission-extra-index");
    drop(RuntimeSidecarSqliteAdapter::open(&index_path).unwrap());
    Connection::open(&index_path)
        .unwrap()
        .execute_batch("CREATE INDEX extra_submission_index ON submission_conversations(username)")
        .unwrap();
    assert_eq!(
        RuntimeSidecarSqliteAdapter::open(&index_path)
            .expect_err("extra index is not canonical")
            .code,
        "runtime_store_migration_blocked"
    );

    let trigger_path = temp_db_path("submission-extra-trigger");
    drop(RuntimeSidecarSqliteAdapter::open(&trigger_path).unwrap());
    Connection::open(&trigger_path)
        .unwrap()
        .execute_batch("CREATE TRIGGER extra_submission_trigger AFTER UPDATE ON submission_conversations BEGIN SELECT 1; END")
        .unwrap();
    assert_eq!(
        RuntimeSidecarSqliteAdapter::open(&trigger_path)
            .expect_err("extra trigger is not canonical")
            .code,
        "runtime_store_migration_blocked"
    );
    for path in [check_path, index_path, trigger_path] {
        let _ = std::fs::remove_file(path);
    }
}

#[test]
fn sqlite_submission_admission_persists_replays_before_busy_and_releases_on_close() {
    let db_path = temp_db_path("submission-admission");
    let (digest, subject, receipt) = empty_finalization_bundle(7);
    let canonical_admission;
    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open adapter");
        adapter
            .finalize_empty_submission_authority(&digest, &subject, &receipt, 7)
            .expect("finalize");
        let created = adapter
            .admit_submission(sqlite_admission_request(
                "message-1",
                "task-1",
                "conversation",
            ))
            .expect("created");
        assert_eq!(created.disposition, SubmissionAdmissionDisposition::Created);
        canonical_admission = created.admission.expect("admission");
        let mut replay = sqlite_admission_request("message-1", "candidate-task", "conversation");
        replay.message_created_at_ms = 99;
        let replayed = adapter.admit_submission(replay).expect("replay");
        assert_eq!(
            replayed.disposition,
            SubmissionAdmissionDisposition::IdempotentReplay
        );
        assert_eq!(replayed.admission, Some(canonical_admission.clone()));
        assert_eq!(
            adapter
                .admit_submission(sqlite_admission_request(
                    "message-2",
                    "task-2",
                    "conversation"
                ))
                .expect("busy")
                .disposition,
            SubmissionAdmissionDisposition::ConversationBusy
        );
    }
    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen");
    let claim = reopened
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "worker-b".to_owned(),
            now_ms: 111,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .expect("claim after restart");
    assert!(claim.found);
    assert_eq!(claim.admission, Some(canonical_admission.clone()));
    let claim = claim.claim.expect("claim token");
    let projected = reopened
        .acknowledge_submission_projection(AcknowledgeSubmissionProjectionRequest {
            message_id: "message-1".to_owned(),
            workflow_owner: claim.owner,
            claim_token: claim.token,
            projection_sha256: canonical_admission.projection_sha256,
            expected_state: SubmissionProjectionState::Pending,
            now_ms: 112,
        })
        .expect("ack projection");
    assert_eq!(
        projected.0.projection_state,
        SubmissionProjectionState::Projected
    );
    let closed = reopened
        .close_conversation_admission(CloseConversationAdmissionRequest {
            username: "owner".to_owned(),
            conversation_id: "conversation".to_owned(),
            operation_id: "delete:conversation".to_owned(),
            now_ms: 113,
        })
        .expect("close");
    assert_eq!(
        closed.disposition,
        ConversationAdmissionCloseDisposition::Closed
    );
    assert_eq!(
        reopened.get_task("task-1").unwrap().unwrap().status,
        "cancelled"
    );
    assert!(
        !reopened
            .claim_pending_submission(ClaimPendingSubmissionRequest {
                workflow_owner: "worker-c".to_owned(),
                now_ms: 200,
                claim_ttl_ms: 100,
                after_created_at_ms: None,
                after_message_id: None,
            })
            .expect("closed scan")
            .found
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_claim_prepare_get_and_handoff_are_durable_and_cas_closed() {
    let db_path = temp_db_path("submission-handoff");
    let (digest, subject, receipt) = empty_finalization_bundle(8);
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 8)
        .unwrap();
    let created = adapter
        .admit_submission(sqlite_admission_request(
            "flow-message",
            "flow-task",
            "flow-conversation",
        ))
        .unwrap();
    let initial_claim = created.claim.unwrap();
    let claim = adapter
        .renew_submission_claim(RenewSubmissionClaimRequest {
            message_id: "flow-message".to_owned(),
            workflow_owner: initial_claim.owner,
            claim_token: initial_claim.token,
            now_ms: 11,
            claim_ttl_ms: 100,
        })
        .expect("renew claim");
    let projection_digest = created.admission.unwrap().projection_sha256;
    adapter
        .acknowledge_submission_projection(AcknowledgeSubmissionProjectionRequest {
            message_id: "flow-message".to_owned(),
            workflow_owner: claim.owner.clone(),
            claim_token: claim.token.clone(),
            projection_sha256: projection_digest,
            expected_state: SubmissionProjectionState::Pending,
            now_ms: 12,
        })
        .expect("project");
    let prepared = sqlite_prepared_execution("flow-task", "flow-conversation", "flow-message");
    let prepared_digest =
        submission_digest(b"maf.submission.prepared_execution.v1\0", &[&prepared]);
    let first = adapter
        .prepare_submission_handoff(PrepareSubmissionHandoffRequest {
            message_id: "flow-message".to_owned(),
            workflow_owner: claim.owner.clone(),
            claim_token: claim.token.clone(),
            prepared_execution_json: prepared.clone(),
            prepared_execution_sha256: prepared_digest.clone(),
            expected_state: SubmissionPreparationState::Pending,
            now_ms: 13,
        })
        .expect("prepare");
    assert!(!first.1);
    drop(adapter);
    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    let stored = reopened
        .get_submission_preparation(&GetSubmissionPreparationRequest {
            username: "owner".to_owned(),
            conversation_id: "flow-conversation".to_owned(),
            task_id: "flow-task".to_owned(),
        })
        .unwrap()
        .expect("prepared snapshot survives restart");
    assert_eq!(stored.prepared_execution_json, Some(prepared));
    let handed_off = reopened
        .acknowledge_submission_handoff(AcknowledgeSubmissionHandoffRequest {
            message_id: "flow-message".to_owned(),
            workflow_owner: claim.owner,
            claim_token: claim.token,
            prepared_execution_sha256: prepared_digest,
            handoff_kind: "agent_run".to_owned(),
            handoff_identity: "run-flow".to_owned(),
            expected_state: SubmissionHandoffState::Pending,
            now_ms: 14,
        })
        .expect("handoff");
    assert_eq!(
        handed_off.0.handoff_state,
        SubmissionHandoffState::HandedOff
    );
    assert!(
        !reopened
            .claim_pending_submission(ClaimPendingSubmissionRequest {
                workflow_owner: "other".to_owned(),
                now_ms: 200,
                claim_ttl_ms: 100,
                after_created_at_ms: None,
                after_message_id: None,
            })
            .unwrap()
            .found
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_claim_observability_is_cursor_scoped_and_head_ordered() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let (digest, subject, receipt) = empty_finalization_bundle(1);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
        .unwrap();
    let head = adapter
        .admit_submission(sqlite_admission_request(
            "head-message",
            "head-task",
            "head-conversation",
        ))
        .unwrap();
    let head_claim = head.claim.unwrap();
    let mut tail = sqlite_admission_request("tail-message", "tail-task", "tail-conversation");
    tail.now_ms = 20;
    tail.message_created_at_ms = 20;
    tail.claim_ttl_ms = 20;
    adapter.admit_submission(tail).unwrap();

    let blocked = adapter
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "recovery".to_owned(),
            now_ms: 50,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .unwrap();
    assert!(!blocked.found);
    assert_eq!(blocked.pending_count, 2);
    assert_eq!(blocked.earliest_claim_expires_at_ms, Some(110));

    let tail = adapter
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "recovery".to_owned(),
            now_ms: 50,
            claim_ttl_ms: 100,
            after_created_at_ms: Some(10),
            after_message_id: Some("head-message".to_owned()),
        })
        .unwrap();
    assert!(tail.found);
    assert_eq!(tail.pending_count, 1);
    assert_eq!(tail.admission.unwrap().message_id, "tail-message");

    let renewed = adapter
        .renew_submission_claim(RenewSubmissionClaimRequest {
            message_id: "head-message".to_owned(),
            workflow_owner: head_claim.owner,
            claim_token: head_claim.token,
            now_ms: 60,
            claim_ttl_ms: 200,
        })
        .unwrap();
    assert_eq!(renewed.expires_at_ms, 260);
    let heartbeat_blocked = adapter
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "other".to_owned(),
            now_ms: 259,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .unwrap();
    assert_eq!(heartbeat_blocked.pending_count, 2);
    assert_eq!(heartbeat_blocked.earliest_claim_expires_at_ms, Some(260));

    let takeover = adapter
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "other".to_owned(),
            now_ms: 260,
            claim_ttl_ms: 100,
            after_created_at_ms: None,
            after_message_id: None,
        })
        .unwrap();
    assert!(takeover.found);
    assert_eq!(takeover.pending_count, 2);
    assert_eq!(takeover.admission.unwrap().message_id, "head-message");

    let empty = adapter
        .claim_pending_submission(ClaimPendingSubmissionRequest {
            workflow_owner: "other".to_owned(),
            now_ms: 260,
            claim_ttl_ms: 100,
            after_created_at_ms: Some(20),
            after_message_id: Some("tail-message".to_owned()),
        })
        .unwrap();
    assert!(!empty.found);
    assert_eq!(empty.pending_count, 0);
    assert_eq!(empty.earliest_claim_expires_at_ms, None);
}

#[test]
fn sqlite_message_identity_reservation_creates_guard_and_close_fences_new_messages() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().unwrap();
    let (digest, subject, receipt) = empty_finalization_bundle(9);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 9)
        .unwrap();
    let identity = MessageIdentityRecord {
        message_id: "file-message".to_owned(),
        conversation_id: "file-conversation".to_owned(),
        username: "owner".to_owned(),
        identity_kind: MessageIdentityKind::FileVisible,
        role: Some("user".to_owned()),
        message_type: Some("file".to_owned()),
        message_created_at_ms: Some(10),
        task_id: None,
        request_fingerprint: None,
        reserved_at_ms: 10,
    };
    assert_eq!(
        adapter
            .reserve_message_identity(ReserveMessageIdentityRequest {
                identity: identity.clone(),
            })
            .unwrap()
            .disposition,
        maf_runtime_sidecar::MessageIdentityDisposition::Created
    );
    assert_eq!(
        adapter
            .reserve_message_identity(ReserveMessageIdentityRequest { identity })
            .unwrap()
            .disposition,
        maf_runtime_sidecar::MessageIdentityDisposition::ExactReplay
    );
    adapter
        .close_conversation_admission(CloseConversationAdmissionRequest {
            username: "owner".to_owned(),
            conversation_id: "file-conversation".to_owned(),
            operation_id: "delete:file-conversation".to_owned(),
            now_ms: 11,
        })
        .unwrap();
    let blocked = adapter
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: MessageIdentityRecord {
                message_id: "server-message".to_owned(),
                conversation_id: "file-conversation".to_owned(),
                username: "owner".to_owned(),
                identity_kind: MessageIdentityKind::ServerInternal,
                role: Some("assistant".to_owned()),
                message_type: Some("text".to_owned()),
                message_created_at_ms: Some(12),
                task_id: Some("task".to_owned()),
                request_fingerprint: None,
                reserved_at_ms: 12,
            },
        })
        .unwrap();
    assert_eq!(
        blocked.disposition,
        maf_runtime_sidecar::MessageIdentityDisposition::ConversationNotAvailable
    );
}

#[test]
fn sqlite_submission_offline_import_validates_active_tasks_and_legacy_identity() {
    let db_path = temp_db_path("submission-import");
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open adapter");
    let task = TaskRecord {
        task_id: "legacy-task".to_owned(),
        conversation_id: "legacy-conversation".to_owned(),
        root_message_id: "legacy-root".to_owned(),
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
        .submit_task_record(task.clone(), "legacy-task-key", None)
        .expect("seed pre-finalization Task");
    let conversation = SubmissionConversationImportRecord {
        conversation_id: "legacy-conversation".to_owned(),
        username: "owner".to_owned(),
        status: "active".to_owned(),
        active_task_id: Some("legacy-task".to_owned()),
        updated_at_ms: 8,
    };
    let identity = MessageIdentityRecord {
        message_id: "legacy-message".to_owned(),
        conversation_id: "legacy-conversation".to_owned(),
        username: "owner".to_owned(),
        identity_kind: MessageIdentityKind::LegacyConflictOnly,
        role: None,
        message_type: None,
        message_created_at_ms: None,
        task_id: None,
        request_fingerprint: None,
        reserved_at_ms: 8,
    };
    let (digest, subject, receipt) = finalization_bundle(
        9,
        test_inventory(
            "conversations",
            vec![conversation.conversation_id.clone()],
            vec![serde_json::json!({
                "active_task_id": conversation.active_task_id,
                "conversation_id": conversation.conversation_id,
                "status": conversation.status,
                "updated_at_ms": conversation.updated_at_ms,
                "username": conversation.username,
            })],
        ),
        test_inventory(
            "message_identities",
            vec![identity.message_id.clone()],
            vec![serde_json::json!({
                "conversation_id": identity.conversation_id,
                "identity_kind": "legacy_conflict_only",
                "message_created_at_ms": identity.message_created_at_ms,
                "message_id": identity.message_id,
                "message_type": identity.message_type,
                "request_fingerprint": identity.request_fingerprint,
                "reserved_at_ms": identity.reserved_at_ms,
                "role": identity.role,
                "task_id": identity.task_id,
                "username": identity.username,
            })],
        ),
        test_inventory(
            "active_tasks",
            vec![task.task_id.clone()],
            vec![serde_json::to_value(&task).unwrap()],
        ),
    );
    let request = SubmissionAuthorityImportRequest {
        conversations: vec![conversation],
        message_identities: vec![identity],
        active_task_ids: vec!["legacy-task".to_owned()],
        finalization_subject_json: subject,
        finalization_receipt_sha256: digest.clone(),
        finalization_receipt_json: receipt.clone(),
        finalized_at_ms: 9,
    };
    let imported = adapter
        .import_and_finalize_submission_authority(request.clone())
        .expect("import and finalize");
    assert!(!imported.exact_replay);
    let mut conversation_drift = request.clone();
    conversation_drift.conversations[0].username = "different".to_owned();
    assert_eq!(
        adapter
            .import_and_finalize_submission_authority(conversation_drift)
            .expect_err("same digest rejects Conversation array drift")
            .code,
        "runtime_store_write_failed"
    );
    let mut message_drift = request.clone();
    message_drift.message_identities[0].message_id = "different".to_owned();
    assert_eq!(
        adapter
            .import_and_finalize_submission_authority(message_drift)
            .expect_err("same digest rejects Message array drift")
            .code,
        "runtime_store_write_failed"
    );
    let mut active_id_drift = request.clone();
    active_id_drift.active_task_ids[0] = "different".to_owned();
    assert_eq!(
        adapter
            .import_and_finalize_submission_authority(active_id_drift)
            .expect_err("same digest rejects active Task ID drift")
            .code,
        "runtime_store_write_failed"
    );
    let mut completed = task;
    completed.status = "completed".to_owned();
    completed.updated_at = Some("10".to_owned());
    adapter
        .submit_task_record(completed, "legacy-task-terminal", Some("running"))
        .expect("active Task can evolve after finalization");
    assert!(
        adapter
            .import_and_finalize_submission_authority(request)
            .expect("exact import replay ignores current Task state evolution")
            .exact_replay
    );
    let conflict = adapter
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: MessageIdentityRecord {
                message_id: "legacy-message".to_owned(),
                conversation_id: "legacy-conversation".to_owned(),
                username: "owner".to_owned(),
                identity_kind: MessageIdentityKind::FileVisible,
                role: Some("user".to_owned()),
                message_type: Some("file".to_owned()),
                message_created_at_ms: Some(10),
                task_id: None,
                request_fingerprint: None,
                reserved_at_ms: 10,
            },
        })
        .expect("legacy identity is conflict-only");
    assert_eq!(
        conflict.disposition,
        maf_runtime_sidecar::MessageIdentityDisposition::Conflict
    );
    assert_eq!(
        adapter
            .submit_task_record(
                TaskRecord {
                    task_id: "unowned-task".to_owned(),
                    conversation_id: "other".to_owned(),
                    root_message_id: "other-message".to_owned(),
                    status: "accepted".to_owned(),
                    routing_mode: "auto".to_owned(),
                    requested_capability_id: None,
                    summary: None,
                    cancel_requested_at: None,
                    created_at: Some("2".to_owned()),
                    updated_at: None,
                    assignment: None,
                },
                "unowned-task-key",
                None,
            )
            .expect_err("finalized authority rejects unowned accepted Task")
            .code,
        "runtime_store_migration_blocked"
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_two_connections_serialize_created_replay_and_busy() {
    let db_path = temp_db_path("submission-concurrency");
    let (digest, subject, receipt) = empty_finalization_bundle(4);
    RuntimeSidecarSqliteAdapter::open(&db_path)
        .unwrap()
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 4)
        .unwrap();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut handles = Vec::new();
    for _ in 0..2 {
        let path = db_path.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            let adapter = RuntimeSidecarSqliteAdapter::open(path).unwrap();
            barrier.wait();
            adapter
                .admit_submission(sqlite_admission_request(
                    "same-message",
                    "same-task",
                    "same-conversation",
                ))
                .unwrap()
                .disposition
        }));
    }
    let mut dispositions = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect::<Vec<_>>();
    dispositions.sort_by_key(|value| match value {
        SubmissionAdmissionDisposition::Created => 0,
        SubmissionAdmissionDisposition::IdempotentReplay => 1,
        _ => 2,
    });
    assert_eq!(
        dispositions,
        vec![
            SubmissionAdmissionDisposition::Created,
            SubmissionAdmissionDisposition::IdempotentReplay,
        ]
    );
    let busy = RuntimeSidecarSqliteAdapter::open(&db_path)
        .unwrap()
        .admit_submission(sqlite_admission_request(
            "other-message",
            "other-task",
            "same-conversation",
        ))
        .unwrap();
    assert_eq!(
        busy.disposition,
        SubmissionAdmissionDisposition::ConversationBusy
    );
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_submission_write_failure_rolls_back_guard_identity_task_and_admission() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    adapter
        .submit_task("seed-task", "seed-conversation", "submission:fault-message")
        .expect("seed colliding Task idempotency key before cutover");
    let (digest, subject, receipt) = empty_finalization_bundle(5);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 5)
        .expect("finalize");
    assert_eq!(
        adapter
            .admit_submission(sqlite_admission_request(
                "fault-message",
                "fault-task",
                "fault-conversation",
            ))
            .expect_err("Task receipt insertion fault rolls back all admission state")
            .code,
        "runtime_store_write_failed"
    );
    assert_eq!(adapter.get_task("fault-task").unwrap(), None);
    let reserved = adapter
        .reserve_message_identity(ReserveMessageIdentityRequest {
            identity: MessageIdentityRecord {
                message_id: "fault-message".to_owned(),
                conversation_id: "fault-conversation".to_owned(),
                username: "owner".to_owned(),
                identity_kind: MessageIdentityKind::FileVisible,
                role: Some("user".to_owned()),
                message_type: Some("file".to_owned()),
                message_created_at_ms: Some(6),
                task_id: None,
                request_fingerprint: None,
                reserved_at_ms: 6,
            },
        })
        .expect("rolled back Message identity can be reserved");
    assert_eq!(
        reserved.disposition,
        maf_runtime_sidecar::MessageIdentityDisposition::Created
    );
}

#[test]
fn sqlite_submission_each_admission_write_point_rolls_back_atomically() {
    let cases = [
        (
            "guard",
            "CREATE TRIGGER fail_guard BEFORE INSERT ON submission_conversations BEGIN SELECT RAISE(ABORT, 'fault'); END;",
            "DROP TRIGGER fail_guard",
        ),
        (
            "identity",
            "CREATE TRIGGER fail_identity BEFORE INSERT ON submission_message_identities BEGIN SELECT RAISE(ABORT, 'fault'); END;",
            "DROP TRIGGER fail_identity",
        ),
        (
            "task",
            "CREATE TRIGGER fail_task BEFORE INSERT ON submitted_tasks BEGIN SELECT RAISE(ABORT, 'fault'); END;",
            "DROP TRIGGER fail_task",
        ),
        (
            "admission",
            "CREATE TRIGGER fail_admission BEFORE INSERT ON submission_admissions BEGIN SELECT RAISE(ABORT, 'fault'); END;",
            "DROP TRIGGER fail_admission",
        ),
        (
            "active-pointer",
            "CREATE TRIGGER fail_pointer BEFORE UPDATE OF active_task_id ON submission_conversations WHEN NEW.active_task_id IS NOT NULL BEGIN SELECT RAISE(ABORT, 'fault'); END;",
            "DROP TRIGGER fail_pointer",
        ),
    ];
    for (name, trigger, drop_trigger) in cases {
        let db_path = temp_db_path(&format!("submission-fault-{name}"));
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
        let (digest, subject, receipt) = empty_finalization_bundle(1);
        adapter
            .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
            .unwrap();
        let connection = Connection::open(&db_path).unwrap();
        connection.execute_batch(trigger).unwrap();
        let request = sqlite_admission_request(
            "fault-matrix-message",
            "fault-matrix-task",
            "fault-matrix-conversation",
        );
        assert_eq!(
            adapter
                .admit_submission(request.clone())
                .expect_err("injected write fault")
                .code,
            "runtime_store_write_failed",
            "write point: {name}"
        );
        assert_eq!(adapter.get_task("fault-matrix-task").unwrap(), None);
        connection.execute_batch(drop_trigger).unwrap();
        assert_eq!(
            adapter.admit_submission(request).unwrap().disposition,
            SubmissionAdmissionDisposition::Created,
            "all authority rows rolled back at write point: {name}"
        );
        drop(connection);
        drop(adapter);
        let _ = std::fs::remove_file(db_path);
    }

    let db_path = temp_db_path("submission-fault-claim");
    let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).unwrap();
    let (digest, subject, receipt) = empty_finalization_bundle(1);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
        .unwrap();
    adapter
        .admit_submission(sqlite_admission_request(
            "claim-fault-message",
            "claim-fault-task",
            "claim-fault-conversation",
        ))
        .unwrap();
    let connection = Connection::open(&db_path).unwrap();
    connection
        .execute_batch("CREATE TRIGGER fail_claim BEFORE UPDATE OF claim_token ON submission_admissions WHEN NEW.claim_owner = 'recovery' BEGIN SELECT RAISE(ABORT, 'fault'); END;")
        .unwrap();
    let claim_request = ClaimPendingSubmissionRequest {
        workflow_owner: "recovery".to_owned(),
        now_ms: 1_001,
        claim_ttl_ms: 100,
        after_created_at_ms: None,
        after_message_id: None,
    };
    assert_eq!(
        adapter
            .claim_pending_submission(claim_request.clone())
            .expect_err("claim write fault")
            .code,
        "runtime_store_write_failed"
    );
    connection.execute_batch("DROP TRIGGER fail_claim").unwrap();
    assert!(
        adapter
            .claim_pending_submission(claim_request)
            .unwrap()
            .found
    );
    drop(connection);
    drop(adapter);
    let _ = std::fs::remove_file(db_path);
}

#[test]
fn sqlite_terminal_task_writers_release_only_their_current_conversation_guard() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().expect("open adapter");
    let (digest, subject, receipt) = empty_finalization_bundle(6);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 6)
        .unwrap();
    let first = adapter
        .admit_submission(sqlite_admission_request(
            "message-1",
            "task-1",
            "conversation",
        ))
        .unwrap()
        .admission
        .unwrap();
    let mut completed = first.task;
    completed.status = "completed".to_owned();
    completed.updated_at = Some("11".to_owned());
    adapter
        .submit_task_record(completed.clone(), "terminal-1", Some("accepted"))
        .expect("terminal SubmitTask releases guard");
    let second = adapter
        .admit_submission(sqlite_admission_request(
            "message-2",
            "task-2",
            "conversation",
        ))
        .expect("second admission after terminal release");
    assert_eq!(second.disposition, SubmissionAdmissionDisposition::Created);
    adapter
        .submit_task_record(completed, "terminal-1-replay", Some("completed"))
        .expect("late old terminal exact update");
    assert_eq!(
        adapter
            .admit_submission(sqlite_admission_request(
                "message-3",
                "task-3",
                "conversation"
            ))
            .expect("new active guard remains")
            .disposition,
        SubmissionAdmissionDisposition::ConversationBusy
    );

    let second_adapter = RuntimeSidecarSqliteAdapter::open_in_memory().unwrap();
    let (digest, subject, receipt) = empty_finalization_bundle(7);
    second_adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 7)
        .unwrap();
    let admitted = second_adapter
        .admit_submission(sqlite_admission_request(
            "message-agent",
            "task-agent",
            "conv-agent",
        ))
        .unwrap()
        .admission
        .unwrap();
    second_adapter
        .commit_agent_state(agent_request(
            "create_run",
            agent_run(0),
            Vec::new(),
            0,
            "agent-create-terminal-guard",
        ))
        .unwrap();
    let mut terminal_task = admitted.task;
    terminal_task.status = "completed".to_owned();
    terminal_task.updated_at = Some("2".to_owned());
    let mut final_request = agent_request(
        "commit_final",
        completed_agent_run(),
        vec![agent_item()],
        0,
        "agent-final-terminal-guard",
    );
    final_request.task_nodes = vec![agent_node()];
    final_request.artifacts = vec![agent_artifact()];
    final_request.final_projection_json = Some(agent_final_projection());
    final_request.task = Some(terminal_task);
    second_adapter
        .commit_agent_state(final_request)
        .expect("terminal CommitAgentState releases guard");
    assert_eq!(
        second_adapter
            .admit_submission(sqlite_admission_request(
                "message-after-agent",
                "task-after-agent",
                "conv-agent",
            ))
            .unwrap()
            .disposition,
        SubmissionAdmissionDisposition::Created
    );
}

#[test]
fn finalized_sqlite_commit_agent_state_rejects_unowned_new_accepted_task() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().unwrap();
    let (digest, subject, receipt) = empty_finalization_bundle(1);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 1)
        .unwrap();
    adapter
        .commit_agent_state(agent_request(
            "create_run",
            agent_run(0),
            Vec::new(),
            0,
            "sqlite-finalized-agent-create",
        ))
        .unwrap();
    let mut accepted = agent_task();
    accepted.status = "accepted".to_owned();
    let mut request = agent_request(
        "commit_outcome",
        agent_run(1),
        Vec::new(),
        0,
        "sqlite-finalized-agent-outcome",
    );
    request.task = Some(accepted);
    let error = adapter
        .commit_agent_state(request)
        .expect_err("finalized authority rejects Task without admission/import evidence");
    assert_eq!(error.code, "runtime_store_migration_blocked");
    assert_eq!(
        adapter
            .get_agent_run("run-agent")
            .unwrap()
            .unwrap()
            .revision,
        0
    );
    assert_eq!(adapter.get_task("task-agent").unwrap(), None);
}

#[test]
fn sqlite_initial_no_server_assignment_and_terminal_update_is_narrowly_authorized() {
    let adapter = RuntimeSidecarSqliteAdapter::open_in_memory().unwrap();
    let (digest, subject, receipt) = empty_finalization_bundle(10);
    adapter
        .finalize_empty_submission_authority(&digest, &subject, &receipt, 10)
        .unwrap();
    let mut request = sqlite_admission_request(
        "no-server-message",
        "no-server-task",
        "no-server-conversation",
    );
    let mut continuation: serde_json::Value =
        serde_json::from_slice(&request.continuation_json).unwrap();
    assert_eq!(continuation["initial_no_server_eligible"], false);
    continuation["initial_no_server_eligible"] = serde_json::json!(true);
    request.continuation_json = canonical(continuation);
    request.continuation_sha256 = submission_digest(
        b"maf.submission.continuation.v1\0",
        &[&request.continuation_json],
    );
    request.task.assignment = Some(TaskRouteAssignment {
        route_mode: "enforce".to_owned(),
        real_path: "user_scoped".to_owned(),
        shadow_path: "none".to_owned(),
        config_version: "config-1".to_owned(),
        reason_code: "user_scoped_selected".to_owned(),
        cohort_id: None,
        assignment_key_hash: Some("b".repeat(64)),
        assigned_at: Some("1".to_owned()),
    });
    let created = adapter.admit_submission(request).unwrap();
    let claim = created.claim.unwrap();
    let mut record = created.admission.unwrap();
    adapter
        .acknowledge_submission_projection(AcknowledgeSubmissionProjectionRequest {
            message_id: record.message_id.clone(),
            workflow_owner: claim.owner.clone(),
            claim_token: claim.token.clone(),
            projection_sha256: record.projection_sha256.clone(),
            expected_state: SubmissionProjectionState::Pending,
            now_ms: 11,
        })
        .unwrap();
    let mut prepared_value: serde_json::Value = serde_json::from_slice(&sqlite_prepared_execution(
        &record.task_id,
        &record.conversation_id,
        &record.message_id,
    ))
    .unwrap();
    prepared_value["prepared_kind"] = serde_json::json!("no_server_intent");
    prepared_value["planned_handoff_kind"] = serde_json::json!("no_server_intent");
    let prepared = canonical(prepared_value);
    let prepared_digest =
        submission_digest(b"maf.submission.prepared_execution.v1\0", &[&prepared]);
    adapter
        .prepare_submission_handoff(PrepareSubmissionHandoffRequest {
            message_id: record.message_id.clone(),
            workflow_owner: claim.owner,
            claim_token: claim.token,
            prepared_execution_json: prepared,
            prepared_execution_sha256: prepared_digest,
            expected_state: SubmissionPreparationState::Pending,
            now_ms: 12,
        })
        .expect("store no-server preparation evidence");
    record.task.status = "failed".to_owned();
    record.task.updated_at = Some("13".to_owned());
    let replacement = record.task.assignment.as_mut().unwrap();
    replacement.real_path = "unavailable".to_owned();
    replacement.reason_code = "no_user_scoped_server".to_owned();
    adapter
        .submit_task_record(record.task.clone(), "no-server-terminal", Some("accepted"))
        .expect("narrow no-server assignment+terminal update");
    let mut drift = record.task;
    drift.status = "accepted".to_owned();
    drift.assignment.as_mut().unwrap().reason_code = "different".to_owned();
    assert_eq!(
        adapter
            .submit_task_record(drift, "no-server-drift", Some("failed"))
            .expect_err("all other assignment drift remains rejected")
            .code,
        "runtime_store_idempotency_conflict"
    );
}

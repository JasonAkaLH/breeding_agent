use maf_runtime_sidecar::{
    ArtifactRecord, RuntimeSidecarSqliteAdapter, TaskEdgeRecord, TaskNodeRecord, TaskRecord,
    TaskRouteAssignment,
};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static TEMP_PATH_COUNTER: AtomicU64 = AtomicU64::new(0);

fn authority_task(status: &str) -> TaskRecord {
    TaskRecord {
        task_id: "task-authority".to_owned(),
        conversation_id: "conv".to_owned(),
        root_message_id: "message".to_owned(),
        status: status.to_owned(),
        routing_mode: "auto".to_owned(),
        requested_capability_id: None,
        root_node_id: None,
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
        criticality: "required".to_owned(),
        dependency_type: "hard".to_owned(),
        retry_policy_json: b"{\"max_attempts\":2}".to_vec(),
        timeout_policy_json: b"{\"seconds\":30}".to_vec(),
        resource_class: Some("default".to_owned()),
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
        let first = adapter
            .append_event(
                "conv",
                "task",
                "task.accepted",
                b"{}".to_vec(),
                10,
                "event-1",
            )
            .expect("append event");
        let duplicate = adapter
            .append_event(
                "conv",
                "task",
                "changed",
                b"{\"changed\":true}".to_vec(),
                11,
                "event-1",
            )
            .expect("idempotent append");
        assert_eq!(first, duplicate);
        assert_eq!(first.sequence, 1);
    }

    let reopened = RuntimeSidecarSqliteAdapter::open(&db_path).expect("reopen sqlite adapter");
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
fn sqlite_adapter_persists_task_edges_and_artifacts_across_reopen() {
    let db_path = temp_db_path("edge-artifact");

    {
        let adapter = RuntimeSidecarSqliteAdapter::open(&db_path).expect("open sqlite adapter");
        let edge = adapter
            .save_task_edge(
                TaskEdgeRecord {
                    task_id: "task".to_owned(),
                    from_node_id: "node-a".to_owned(),
                    to_node_id: "node-b".to_owned(),
                    edge_type: "data".to_owned(),
                    condition: "ok".to_owned(),
                },
                "edge-1",
            )
            .expect("save task edge");
        assert_eq!(edge.from_node_id, "node-a");

        let duplicate_edge = adapter
            .save_task_edge(
                TaskEdgeRecord {
                    task_id: "task".to_owned(),
                    from_node_id: "changed".to_owned(),
                    to_node_id: "node-b".to_owned(),
                    edge_type: "control".to_owned(),
                    condition: "changed".to_owned(),
                },
                "edge-1",
            )
            .expect("idempotent task edge");
        assert_eq!(duplicate_edge, edge);

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
    let edges = reopened.list_task_edges("task").expect("list task edges");
    assert_eq!(edges.len(), 1);
    assert_eq!(edges[0].to_node_id, "node-b");

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

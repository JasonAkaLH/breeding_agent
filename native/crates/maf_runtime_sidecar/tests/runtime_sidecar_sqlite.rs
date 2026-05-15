use maf_runtime_sidecar::RuntimeSidecarSqliteAdapter;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

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
            .transition_node("task", "node", "running", "node-1")
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
        .transition_node("task", "node", "changed", "node-1")
        .expect("idempotent node transition after reopen");
    assert_eq!(duplicate_transition.status, "running");

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

fn temp_db_path(test_name: &str) -> PathBuf {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock")
        .as_nanos();
    let mut path = std::env::temp_dir();
    path.push(format!(
        "maf-runtime-sidecar-{test_name}-{}-{timestamp}.sqlite",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    path
}

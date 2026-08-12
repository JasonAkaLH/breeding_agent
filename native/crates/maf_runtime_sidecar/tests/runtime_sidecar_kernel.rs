use maf_runtime_sidecar::{
    AppendEventRequest, BundleRevisionResult, COMPONENT_ID, CompatibilityCheck,
    CompatibilityCheckRequest, HealthState, Idempotency, PROTOCOL_VERSION, ReadinessState,
    ReplayEventsRequest, RuntimeSidecarKernel, RuntimeSidecarService, TaskNodeRecord, TaskRecord,
    TaskRouteAssignment,
};

fn task_node(status: &str) -> TaskNodeRecord {
    TaskNodeRecord {
        node_id: "node-authority".to_owned(),
        task_id: "task-authority".to_owned(),
        capability_id: "main_agent.respond".to_owned(),
        assigned_instance_id: None,
        status: status.to_owned(),
        criticality: "required".to_owned(),
        dependency_type: "hard".to_owned(),
        retry_policy_json: b"{}".to_vec(),
        timeout_policy_json: b"{}".to_vec(),
        resource_class: None,
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
        root_node_id: None,
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

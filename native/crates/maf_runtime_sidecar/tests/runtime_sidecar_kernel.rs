use maf_runtime_sidecar::{
    AppendEventRequest, BundleRevisionResult, COMPONENT_ID, CompatibilityCheck,
    CompatibilityCheckRequest, HealthState, Idempotency, PROTOCOL_VERSION, ReadinessState,
    ReplayEventsRequest, RuntimeSidecarKernel, RuntimeSidecarService,
};
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

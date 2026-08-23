use maf_runtime_sidecar::pb::common::v1 as common_pb;
use maf_runtime_sidecar::pb::runtime::v1 as runtime_pb;
use maf_runtime_sidecar::{
    COMPONENT_ID, RuntimeSidecarGrpcService, RuntimeSidecarServeConfig,
    RuntimeSidecarSqliteAdapter, runtime_sidecar_service_from_config,
    serve_runtime_sidecar_with_incoming,
};
#[cfg(unix)]
use maf_runtime_sidecar::{
    semantic_probe_runtime_sidecar_unix_socket, serve_runtime_sidecar_unix_socket,
};
use runtime_pb::runtime_sidecar_server::RuntimeSidecar;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tonic::Request;

static TEMP_PATH_COUNTER: AtomicU64 = AtomicU64::new(0);

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

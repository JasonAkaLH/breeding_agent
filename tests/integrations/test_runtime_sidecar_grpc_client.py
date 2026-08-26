from __future__ import annotations

import os
import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch
from pathlib import Path

from src.storage.rust_contract import artifact_policy, load_runtime_sidecar_contract
from src.storage.runtime_sidecar_grpc_client import (
    RuntimeSidecarGrpcClient,
    _decode_closed_message,
    _decode_message,
    _field_bytes,
    _field_string,
    _field_varint,
    _frame,
    _grpc_data_frames,
    _initialize_http2_connection,
    _read_grpc_response,
    _send_grpc_payload,
    _task_record,
)


class RuntimeSidecarGrpcClientIntegrationTest(unittest.TestCase):
    def test_submission_admission_methods_use_the_nine_approved_rpc_shapes(self) -> None:
        task = _submission_task_record()
        conversation, message, continuation = _submission_projection_bytes()
        projection_sha = hashlib.sha256(
            b"maf.submission.projection.v1\0" + conversation + b"\0" + message
        ).hexdigest()
        continuation_sha = hashlib.sha256(
            b"maf.submission.continuation.v1\0" + continuation
        ).hexdigest()
        admission = _submission_admission_wire(
            task=task,
            conversation=conversation,
            message=message,
            continuation=continuation,
            projection_sha=projection_sha,
            continuation_sha=continuation_sha,
        )
        prepared_execution = _prepared_execution_bytes()
        prepared_sha = hashlib.sha256(
            b"maf.submission.prepared_execution.v1\0" + prepared_execution
        ).hexdigest()
        prepared_admission = _submission_admission_wire(
            task=task,
            conversation=conversation,
            message=message,
            continuation=continuation,
            projection_sha=projection_sha,
            continuation_sha=continuation_sha,
            projection_state=2,
            preparation_state=2,
            prepared_execution=prepared_execution,
            prepared_sha=prepared_sha,
        )
        handed_off_admission = _submission_admission_wire(
            task=task,
            conversation=conversation,
            message=message,
            continuation=continuation,
            projection_sha=projection_sha,
            continuation_sha=continuation_sha,
            projection_state=2,
            preparation_state=2,
            prepared_execution=prepared_execution,
            prepared_sha=prepared_sha,
            handoff_state=2,
            handoff_kind="agent_run",
            handoff_identity="agent-run:task",
        )
        claim = (
            _field_string(1, "worker")
            + _field_string(2, "secret")
            + _field_varint(3, 2_000)
        )
        responses = {
            "AdmitSubmission": _field_varint(1, 1)
            + _field_bytes(2, admission)
            + _field_bytes(3, claim),
            "ClaimPendingSubmission": _field_varint(1, 1)
            + _field_bytes(2, admission)
            + _field_bytes(3, claim)
            + _field_string(4, "finalized")
            + _field_string(5, "f" * 64),
            "RenewSubmissionClaim": _field_bytes(1, claim),
            "AcknowledgeSubmissionProjection": _field_bytes(
                1,
                _submission_admission_wire(
                    task=task,
                    conversation=conversation,
                    message=message,
                    continuation=continuation,
                    projection_sha=projection_sha,
                    continuation_sha=continuation_sha,
                    projection_state=2,
                ),
            ),
            "PrepareSubmissionHandoff": _field_bytes(1, prepared_admission),
            "GetSubmissionPreparation": _field_varint(1, 1)
            + _field_bytes(2, prepared_admission),
            "AcknowledgeSubmissionHandoff": _field_bytes(
                1, handed_off_admission
            ),
            "CloseConversationAdmission": _field_varint(1, 1)
            + _field_varint(2, 2),
            "ReserveMessageIdentity": _field_varint(1, 1)
            + _field_bytes(2, _message_identity_wire()),
        }
        client = object.__new__(RuntimeSidecarGrpcClient)
        client._ensure_compatible = Mock()  # type: ignore[method-assign]
        calls: list[tuple[str, bytes]] = []

        def unary(method: str, payload: bytes, *, timeout_seconds: float) -> bytes:
            self.assertEqual(timeout_seconds, 5)
            calls.append((method, payload))
            return responses[method]

        client._unary = unary  # type: ignore[method-assign]

        admitted = client.admit_submission(
            message_id="message",
            task_id="task",
            conversation_id="conversation",
            username="owner",
            request_fingerprint="a" * 64,
            conversation_projection_json=conversation,
            message_projection_json=message,
            projection_sha256=projection_sha,
            continuation_json=continuation,
            continuation_sha256=continuation_sha,
            message_created_at_ms=1_000,
            workflow_owner="worker",
            now_ms=1_000,
            claim_ttl_ms=1_000,
            task=task,
            idempotency_key="submission:message",
        )
        claimed = client.claim_pending_submission(
            workflow_owner="worker", now_ms=1_000, claim_ttl_ms=1_000
        )
        renewed = client.renew_submission_claim(
            message_id="message",
            workflow_owner="worker",
            claim_token="secret",
            now_ms=1_000,
            claim_ttl_ms=1_000,
        )
        projected = client.acknowledge_submission_projection(
            message_id="message",
            workflow_owner="worker",
            claim_token="secret",
            projection_sha256=projection_sha,
            now_ms=1_000,
        )
        prepared = client.prepare_submission_handoff(
            message_id="message",
            workflow_owner="worker",
            claim_token="secret",
            prepared_execution_json=prepared_execution,
            prepared_execution_sha256=prepared_sha,
            now_ms=1_000,
        )
        fetched = client.get_submission_preparation(
            username="owner", conversation_id="conversation", task_id="task"
        )
        handed_off = client.acknowledge_submission_handoff(
            message_id="message",
            workflow_owner="worker",
            claim_token="secret",
            prepared_execution_sha256=prepared_sha,
            handoff_kind="agent_run",
            handoff_identity="agent-run:task",
            now_ms=1_000,
        )
        closed = client.close_conversation_admission(
            username="owner",
            conversation_id="conversation",
            operation_id="close:conversation",
            now_ms=1_000,
        )
        reserved = client.reserve_message_identity(
            identity={
                "message_id": "server-message",
                "conversation_id": "conversation",
                "username": "owner",
                "identity_kind": "server_internal",
                "role": "assistant",
                "message_type": "text",
                "message_created_at_ms": 1_000,
                "task_id": "task",
                "request_fingerprint": None,
                "reserved_at_ms": 1_000,
            }
        )

        self.assertEqual(admitted["disposition"], "created")
        self.assertTrue(claimed["found"])
        self.assertEqual(renewed["claim"]["token"], "secret")
        self.assertEqual(projected["admission"]["message_id"], "message")
        self.assertEqual(prepared["admission"]["task_id"], "task")
        self.assertTrue(fetched["found"])
        self.assertEqual(handed_off["admission"]["conversation_id"], "conversation")
        self.assertEqual(closed["disposition"], "closed")
        self.assertEqual(reserved["identity"]["message_id"], "server-message")
        self.assertEqual([method for method, _ in calls], list(responses))
        self.assertEqual(_decode_message(calls[0][1])[16], [b"submission:message"])
        self.assertEqual(
            _decode_message(_decode_message(calls[-1][1])[1][0]),
            _decode_message(_message_identity_wire()),
        )
        with self.assertRaisesRegex(RuntimeError, "differs from request"):
            client.acknowledge_submission_projection(
                message_id="other",
                workflow_owner="worker",
                claim_token="secret",
                projection_sha256=projection_sha,
                now_ms=1_000,
            )
        with self.assertRaisesRegex(RuntimeError, "differs from request"):
            client.prepare_submission_handoff(
                message_id="message",
                workflow_owner="worker",
                claim_token="secret",
                prepared_execution_json=prepared_execution,
                prepared_execution_sha256="0" * 64,
                now_ms=1_000,
            )
        with self.assertRaisesRegex(RuntimeError, "differs from request"):
            client.get_submission_preparation(
                username="owner", conversation_id="conversation", task_id="other"
            )
        with self.assertRaisesRegex(RuntimeError, "differs from request"):
            client.acknowledge_submission_handoff(
                message_id="message",
                workflow_owner="worker",
                claim_token="secret",
                prepared_execution_sha256=prepared_sha,
                handoff_kind="agent_run",
                handoff_identity="agent-run:other",
                now_ms=1_000,
            )
        with self.assertRaisesRegex(RuntimeError, "differs from request"):
            client.reserve_message_identity(
                identity={
                    "message_id": "server-message",
                    "conversation_id": "conversation",
                    "username": "owner",
                    "identity_kind": "server_internal",
                    "role": "assistant",
                    "message_type": "text",
                    "message_created_at_ms": 1_000,
                    "task_id": "other",
                    "request_fingerprint": None,
                    "reserved_at_ms": 1_000,
                }
            )
        invalid_phase_admissions = (
            _submission_admission_wire(
                task=task,
                conversation=conversation,
                message=message,
                continuation=continuation,
                projection_sha=projection_sha,
                continuation_sha=continuation_sha,
                projection_state=2,
                handoff_state=2,
                handoff_kind="agent_run",
                handoff_identity="agent-run:task",
            ),
            _submission_admission_wire(
                task=task,
                conversation=conversation,
                message=message,
                continuation=continuation,
                projection_sha=projection_sha,
                continuation_sha=continuation_sha,
                projection_state=2,
                preparation_state=2,
                prepared_execution=prepared_execution,
                prepared_sha=prepared_sha,
                handoff_kind="agent_run",
                handoff_identity="agent-run:task",
            ),
        )
        for invalid in invalid_phase_admissions:
            responses["AdmitSubmission"] = _field_varint(1, 1) + _field_bytes(
                2, invalid
            )
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                RuntimeError, "runtime_store_response_invalid"
            ):
                client.admit_submission(
                    message_id="message",
                    task_id="task",
                    conversation_id="conversation",
                    username="owner",
                    request_fingerprint="a" * 64,
                    conversation_projection_json=conversation,
                    message_projection_json=message,
                    projection_sha256=projection_sha,
                    continuation_json=continuation,
                    continuation_sha256=continuation_sha,
                    message_created_at_ms=1_000,
                    workflow_owner="worker",
                    now_ms=1_000,
                    claim_ttl_ms=1_000,
                    task=task,
                    idempotency_key="submission:message",
                )

    def test_submission_decoder_rejects_unknown_disposition(self) -> None:
        client = object.__new__(RuntimeSidecarGrpcClient)
        client._ensure_compatible = Mock()  # type: ignore[method-assign]
        client._unary = Mock(return_value=_field_varint(1, 99))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "unknown enum value"):
            client.close_conversation_admission(
                username="owner",
                conversation_id="conversation",
                operation_id="close:conversation",
                now_ms=1_000,
            )

    def test_large_grpc_request_is_split_into_legal_http2_data_frames(self) -> None:
        frames = _grpc_data_frames(b"x" * 50_000)

        self.assertEqual(len(frames), 4)
        self.assertTrue(all(int.from_bytes(frame[:3], "big") <= 16_384 for frame in frames))
        self.assertTrue(all(frame[4] == 0 for frame in frames[:-1]))
        self.assertEqual(frames[-1][4], 1)

    def test_submission_wire_decoder_rejects_unknown_duplicate_and_truncated_fields(self) -> None:
        invalid = (
            _field_string(2, "first") + _field_string(2, "second"),
            _field_string(99, "unknown"),
            b"\x12\x05ab",
            b"\x80",
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                RuntimeError, "malformed protobuf"
            ):
                _decode_closed_message(payload, 1, 2)

    def test_grpc_response_requires_exact_declared_message_length(self) -> None:
        for data in (b"\x00\x00\x00\x00\x05abc", b"\x00\x00\x00\x00\x01ab"):
            sock = _BytesSocket(_frame(0, 1, 1, data))
            with self.subTest(data=data), self.assertRaisesRegex(
                RuntimeError, "message length is inconsistent"
            ):
                _read_grpc_response(sock)

    def test_grpc_response_rejects_configured_message_cap_without_large_allocation(self) -> None:
        data = b"\x00" + (9).to_bytes(4, "big") + b"x" * 9
        sock = _BytesSocket(_frame(0, 1, 1, data))
        with patch(
            "src.storage.runtime_sidecar_grpc_client.resource_limit",
            return_value=8,
        ), self.assertRaisesRegex(RuntimeError, "exceeds the configured limit"):
            _read_grpc_response(sock)

    def test_http2_flow_control_negotiates_and_consumes_window_updates(self) -> None:
        peer_settings = (
            (4).to_bytes(2, "big")
            + (70_000).to_bytes(4, "big")
            + (5).to_bytes(2, "big")
            + (32_768).to_bytes(4, "big")
        )
        negotiation = _BytesSocket(_frame(4, 0, 0, peer_settings))
        max_frame, connection_window, stream_window = _initialize_http2_connection(
            negotiation
        )
        self.assertEqual((max_frame, connection_window, stream_window), (32_768, 65_535, 70_000))
        self.assertEqual(len(negotiation.sent), 3)

        window_updates = _frame(8, 0, 0, (40_000).to_bytes(4, "big")) + _frame(
            8, 0, 1, (40_000).to_bytes(4, "big")
        )
        transport = _BytesSocket(window_updates)
        _send_grpc_payload(
            transport,
            b"x" * 100_000,
            max_frame_size=max_frame,
            connection_window=connection_window,
            stream_window=stream_window,
        )
        self.assertEqual(sum(int.from_bytes(frame[:3], "big") for frame in transport.sent), 100_000)
        self.assertEqual(transport.sent[-1][4], 1)

    def test_client_rejects_public_endpoint_before_connecting(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "runtime_store_unavailable"):
            RuntimeSidecarGrpcClient("http://example.com:50051")

    def test_client_requires_complete_mtls_material_for_https_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "cross-host endpoints must use https mTLS"):
            RuntimeSidecarGrpcClient("http://10.0.0.5:50051", mtls_enabled=True)
        with self.assertRaisesRegex(ValueError, "requires mTLS"):
            RuntimeSidecarGrpcClient("https://127.0.0.1:50051", mtls_enabled=False)
        with self.assertRaisesRegex(ValueError, "requires CA, client certificate, and client key"):
            RuntimeSidecarGrpcClient("https://127.0.0.1:50051", mtls_enabled=True)

    def test_client_rejects_unallowlisted_artifact_provenance_before_connecting(self) -> None:
        metadata = _runtime_sidecar_artifact_metadata()
        with self.assertRaisesRegex(RuntimeError, "runtime_store_artifact_untrusted"):
            RuntimeSidecarGrpcClient(
                "http://127.0.0.1:65535",
                artifact_provenance={**metadata, "checksum_sha256": "sha256:tampered"},
                allowed_artifact_checksums=("sha256:runtime-sidecar",),
                allowed_cargo_lock_digests=("sha256:cargo-lock",),
            )

        client = RuntimeSidecarGrpcClient(
            "http://127.0.0.1:65535",
            artifact_provenance=metadata,
            allowed_artifact_checksums=("sha256:runtime-sidecar",),
            allowed_cargo_lock_digests=("sha256:cargo-lock",),
        )
        self.assertIsNotNone(client)

    def test_python_client_appends_and_replays_against_rust_sidecar_binary(self) -> None:
        binary = _ensure_runtime_sidecar_binary()
        last_startup_error: Exception | None = None
        for _ in range(5):
            port = _free_loopback_port()
            endpoint = f"http://127.0.0.1:{port}"

            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "runtime-sidecar.sqlite"
                process = subprocess.Popen(
                    [
                        str(binary),
                        "--serve",
                        f"127.0.0.1:{port}",
                        "--sqlite",
                        str(db_path),
                    ],
                    cwd=_repo_root(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    try:
                        client = _connect_with_retry(endpoint, process=process)
                    except AssertionError as exc:
                        last_startup_error = exc
                        continue

                    version = client.version()
                    self.assertEqual(version["component"], "maf_runtime_sidecar")

                    client.check_compatibility()
                    cursor = client.append_event(
                        conversation_id="conv",
                        task_id="task",
                        event_type="task.accepted",
                        payload_json=b"{}",
                        idempotency_key="event-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(cursor["sequence"], 1)

                    replayed = client.replay_events(
                        conversation_id="conv",
                        task_id="task",
                        after_sequence=0,
                        page_limit=10,
                        byte_limit=1024,
                    )
                    self.assertEqual(len(replayed["cursors"]), 1)
                    self.assertEqual(replayed["cursors"][0]["sequence"], 1)

                    submitted = client.submit_task(
                        task_id="task",
                        conversation_id="conv",
                        idempotency_key="submit-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(submitted["task_id"], "task")
                    self.assertFalse(submitted["duplicate"])
                    duplicate_submit = client.submit_task(
                        task_id="changed",
                        conversation_id="conv",
                        idempotency_key="submit-1",
                        owner="python-runtime",
                    )
                    self.assertTrue(duplicate_submit["duplicate"])
                    self.assertEqual(duplicate_submit["task_id"], "task")

                    task_record = {
                        "task_id": "task-authority",
                        "conversation_id": "conv",
                        "root_message_id": "message",
                        "status": "accepted",
                        "routing_mode": "auto",
                        "requested_capability_id": None,
                        "summary": None,
                        "cancel_requested_at": None,
                        "created_at": "2026-08-12T00:00:00Z",
                        "updated_at": None,
                        "assignment": {
                            "route_mode": "shadow",
                            "real_path": "legacy",
                            "shadow_path": "user_scoped",
                            "config_version": "config-v1",
                            "reason_code": "shadow_enabled",
                            "cohort_id": None,
                            "assignment_key_hash": "sha256:assignment",
                            "assigned_at": "2026-08-12T00:00:00Z",
                        },
                    }
                    authoritative = client.submit_task(
                        task_id="task-authority",
                        conversation_id="conv",
                        task=task_record,
                        idempotency_key="submit-authority-1",
                    )
                    self.assertEqual(authoritative["task"], task_record)
                    self.assertEqual(client.get_task(task_id="task-authority")["task"], task_record)
                    self.assertFalse(client.get_task(task_id="missing")["found"])
                    listed_tasks = client.list_tasks_for_conversation(conversation_id="conv")
                    self.assertEqual(
                        [task["task_id"] for task in listed_tasks["tasks"]],
                        ["task-authority"],
                    )
                    filtered_tasks = client.list_tasks_for_conversation(
                        conversation_id="conv",
                        statuses=("running",),
                    )
                    self.assertEqual(filtered_tasks["tasks"], [])
                    active_task = client.get_active_task_for_conversation(conversation_id="conv")
                    self.assertTrue(active_task["found"])
                    agent_run = {
                        "run_id": "run-agent",
                        "task_id": "task-agent",
                        "conversation_id": "conv",
                        "status": "running",
                        "model_edition": "edition",
                        "reasoning_effort": "minimal",
                        "thinking_enabled": False,
                        "binding_option_digests_json": {},
                        "next_item_sequence": 1,
                        "compacted_through_sequence": 0,
                        "active_sample_item_id": None,
                        "waiting_call_item_ids": [],
                        "next_batch_call_ordinal": 0,
                        "claim_owner": None,
                        "claim_token": None,
                        "lease_expires_at_ms": None,
                        "revision": 0,
                        "terminal_reason_code": None,
                        "created_at_ms": 1,
                        "updated_at_ms": 1,
                        "terminal_at_ms": None,
                    }
                    created_agent = client.commit_agent_state(
                        operation="create_run",
                        run=agent_run,
                        items=(),
                        expected_revision=0,
                        expected_claim_token=None,
                        idempotency_key="agent-create",
                    )
                    self.assertFalse(created_agent["duplicate"])
                    payload_json = b'{"text":"ok"}\n'
                    agent_item = {
                        "item_id": "item-agent",
                        "run_id": "run-agent",
                        "task_id": "task-agent",
                        "sequence": 1,
                        "kind": "assistant_message",
                        "state": "committed",
                        "payload_json": payload_json,
                        "payload_size_bytes": len(payload_json),
                        "payload_sha256": hashlib.sha256(payload_json).hexdigest(),
                        "parent_item_id": None,
                        "source_call_item_id": None,
                        "provider_sample_id": "sample-agent",
                        "call_ordinal": None,
                        "created_at_ms": 1,
                        "committed_at_ms": 1,
                    }
                    committed_run = {**agent_run, "revision": 1, "next_item_sequence": 2, "updated_at_ms": 2}
                    committed_agent = client.commit_agent_state(
                        operation="commit_sample",
                        run=committed_run,
                        items=(agent_item,),
                        expected_revision=0,
                        expected_claim_token=None,
                        idempotency_key="agent-sample",
                    )
                    self.assertEqual(committed_agent["run"]["revision"], 1)
                    self.assertEqual(client.get_agent_run(run_id="run-agent")["run"], committed_run | {"binding_option_digests_json": b"{}"})
                    self.assertEqual(client.list_agent_items(run_id="run-agent")["items"], [agent_item])
                    self.assertEqual(active_task["task"], task_record)
                    self.assertFalse(
                        client.get_active_task_for_conversation(conversation_id="missing")["found"]
                    )
                    conflicting = {**task_record, "status": "running"}
                    with self.assertRaisesRegex(RuntimeError, "runtime_store_idempotency_conflict"):
                        client.submit_task(
                            task_id="task-authority",
                            conversation_id="conv",
                            task=conflicting,
                            idempotency_key="submit-authority-1",
                        )

                    node_record = {
                        "node_id": "node",
                        "task_id": "task",
                        "capability_id": "main_agent.respond",
                        "assigned_instance_id": "instance",
                        "status": "running",
                        "input_refs": ["input"],
                        "output_refs": ["output"],
                        "started_at": "2026-08-13T10:00:00Z",
                        "finished_at": None,
                    }
                    transitioned = client.transition_node(
                        task_id="task",
                        node_id="node",
                        to_status="running",
                        expected_from_status="",
                        idempotency_key="node-1",
                        owner="python-runtime",
                        node=node_record,
                    )
                    self.assertEqual(transitioned["status"], "running")
                    self.assertEqual(transitioned["node"], node_record)
                    self.assertEqual(client.get_task_node(node_id="node")["node"], node_record)
                    self.assertEqual(client.list_task_nodes_for_task(task_id="task")["nodes"], [node_record])

                    artifact = client.save_artifact(
                        artifact_id="artifact",
                        task_id="task",
                        producer_node_id="node",
                        artifact_type="json",
                        storage_ref="opaque://artifact",
                        summary="summary",
                        is_complete=True,
                        created_at="",
                        idempotency_key="artifact-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(artifact["artifact_id"], "artifact")
                    self.assertEqual(client.get_artifact(artifact_id="artifact")["artifact"], artifact)
                    self.assertEqual(client.list_artifacts_for_task(task_id="task")["artifacts"], [artifact])

                    lease = client.acquire_lease(
                        task_id="task",
                        owner_id="worker",
                        now_ms=100,
                        ttl_ms=50,
                        idempotency_key="lease-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(lease["revision"], 1)
                    renewed = client.renew_lease(
                        task_id="task",
                        renew_token=lease["renew_token"],
                        now_ms=120,
                        ttl_ms=50,
                    )
                    self.assertEqual(renewed["revision"], 2)
                    released = client.release_lease(task_id="task", renew_token=renewed["renew_token"])
                    self.assertTrue(released["released"])

                    cancellation = client.write_cancellation_token(
                        task_id="task",
                        requested_at_ms=200,
                        reason="user",
                        terminal_policy="terminal-noop",
                        idempotency_key="cancel-1",
                        owner="python-runtime",
                    )
                    self.assertTrue(cancellation["written"])

                    pinned = client.pin_bundle_revision(
                        task_id="task",
                        bundle_kind="skill",
                        revision="rev-1",
                        idempotency_key="pin-1",
                        owner="python-runtime",
                    )
                    self.assertFalse(pinned["released"])
                    bundle_release = client.release_bundle_revision(
                        task_id="task",
                        bundle_kind="skill",
                        revision="rev-1",
                        released_at_ms=250,
                        idempotency_key="release-1",
                        owner="python-runtime",
                    )
                    self.assertTrue(bundle_release["released"])
                    return
                finally:
                    _terminate_process(process)
        self.fail(f"Rust runtime sidecar did not become ready on a loopback port: {last_startup_error}")

    def test_python_client_round_trips_near_50_mib_submission_against_sqlite_binary(self) -> None:
        binary = _ensure_runtime_sidecar_binary()
        port = _free_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "runtime-sidecar-large.sqlite"
            process = subprocess.Popen(
                [
                    str(binary),
                    "--serve",
                    f"127.0.0.1:{port}",
                    "--sqlite",
                    str(db_path),
                ],
                cwd=_repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                client = _connect_with_retry(endpoint, process=process)
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        "UPDATE submission_authority_meta "
                        "SET state='finalized', finalization_receipt_sha256=?, "
                        "finalization_receipt_json=?, finalized_at_ms=1 "
                        "WHERE singleton_key=1",
                        ("f" * 64, b"{}"),
                    )
                    connection.commit()
                content = "x" * (49 * 1024 * 1024)
                conversation, message, continuation = _submission_projection_bytes(
                    content
                )
                projection_sha = hashlib.sha256(
                    b"maf.submission.projection.v1\0"
                    + conversation
                    + b"\0"
                    + message
                ).hexdigest()
                continuation_sha = hashlib.sha256(
                    b"maf.submission.continuation.v1\0" + continuation
                ).hexdigest()
                response = client.admit_submission(
                    message_id="message",
                    task_id="task",
                    conversation_id="conversation",
                    username="owner",
                    request_fingerprint="a" * 64,
                    conversation_projection_json=conversation,
                    message_projection_json=message,
                    projection_sha256=projection_sha,
                    continuation_json=continuation,
                    continuation_sha256=continuation_sha,
                    message_created_at_ms=1_000,
                    workflow_owner="worker",
                    now_ms=1_000,
                    claim_ttl_ms=60_000,
                    task=_submission_task_record(),
                    idempotency_key="submission:message",
                    timeout_seconds=60,
                )
                self.assertEqual(response["disposition"], "created")
                self.assertEqual(
                    len(response["admission"]["message_projection_json"]),
                    len(message),
                )
                self.assertEqual(
                    response["admission"]["projection_sha256"],
                    projection_sha,
                )
            finally:
                _terminate_process(process)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix domain sockets are not available on this platform")
    def test_python_client_connects_to_rust_sidecar_binary_over_unix_socket(self) -> None:
        binary = _ensure_runtime_sidecar_binary()

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "runtime-sidecar.sock"
            db_path = Path(temp_dir) / "runtime-sidecar.sqlite"
            endpoint = f"unix://{socket_path}"
            process = subprocess.Popen(
                [
                    str(binary),
                    "--serve",
                    endpoint,
                    "--sqlite",
                    str(db_path),
                ],
                cwd=_repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                client = _connect_with_retry(endpoint, process=process)
                version = client.version()
                self.assertEqual(version["component"], "maf_runtime_sidecar")
                cursor = client.append_event(
                    conversation_id="conv",
                    task_id="task",
                    event_type="task.accepted",
                    payload_json=b"{}",
                    idempotency_key="unix-event-1",
                    owner="python-runtime",
                )
                self.assertEqual(cursor["sequence"], 1)
            finally:
                _terminate_process(process)
                if socket_path.exists():
                    os.unlink(socket_path)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required to generate local mTLS fixtures")
    def test_python_client_connects_to_rust_sidecar_binary_over_mtls(self) -> None:
        binary = _ensure_runtime_sidecar_binary()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            certs = _generate_mtls_certs(root)
            db_path = root / "runtime-sidecar.sqlite"
            last_startup_error: Exception | None = None
            for _ in range(5):
                port = _free_loopback_port()
                endpoint = f"https://127.0.0.1:{port}"
                process = subprocess.Popen(
                    [
                        str(binary),
                        "--serve",
                        f"127.0.0.1:{port}",
                        "--sqlite",
                        str(db_path),
                        "--tls-cert",
                        str(certs["server_cert"]),
                        "--tls-key",
                        str(certs["server_key"]),
                        "--client-ca",
                        str(certs["ca_cert"]),
                    ],
                    cwd=_repo_root(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    try:
                        client = _connect_with_retry(
                            endpoint,
                            process=process,
                            mtls_enabled=True,
                            tls_ca_path=str(certs["ca_cert"]),
                            tls_cert_path=str(certs["client_cert"]),
                            tls_key_path=str(certs["client_key"]),
                            tls_server_name="localhost",
                        )
                    except AssertionError as exc:
                        last_startup_error = exc
                        continue

                    version = client.version()
                    self.assertEqual(version["component"], "maf_runtime_sidecar")
                    cursor = client.append_event(
                        conversation_id="conv",
                        task_id="task",
                        event_type="task.accepted",
                        payload_json=b"{}",
                        idempotency_key="mtls-event-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(cursor["sequence"], 1)
                    return
                finally:
                    _terminate_process(process)
            self.fail(f"Rust runtime sidecar did not become ready over mTLS: {last_startup_error}")


def _connect_with_retry(
    endpoint: str,
    *,
    process: subprocess.Popen[str] | None = None,
    mtls_enabled: bool = False,
    tls_ca_path: str | None = None,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
    tls_server_name: str | None = None,
) -> RuntimeSidecarGrpcClient:
    last_error: Exception | None = None
    for _ in range(100):
        try:
            client = RuntimeSidecarGrpcClient(
                endpoint,
                mtls_enabled=mtls_enabled,
                tls_ca_path=tls_ca_path,
                tls_cert_path=tls_cert_path,
                tls_key_path=tls_key_path,
                tls_server_name=tls_server_name,
            )
            client.version(timeout_seconds=1)
            return client
        except Exception as exc:  # noqa: BLE001 - retry startup race against Rust binary.
            last_error = exc
            if process is not None and process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    "Rust runtime sidecar exited before becoming ready: "
                    f"code={process.returncode}, stdout={stdout[-1000:]!r}, stderr={stderr[-1000:]!r}"
                ) from exc
            time.sleep(0.05)
    raise AssertionError(f"Rust runtime sidecar did not become ready: {last_error}")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    try:
        process.communicate(timeout=1)
    except (subprocess.TimeoutExpired, ValueError):
        pass


def _generate_mtls_certs(root: Path) -> dict[str, Path]:
    ca_cert = root / "ca.crt"
    ca_key = root / "ca.key"
    ca_conf = root / "ca.cnf"
    server_cert = root / "server.crt"
    server_key = root / "server.key"
    server_csr = root / "server.csr"
    server_conf = root / "server.cnf"
    client_cert = root / "client.crt"
    client_key = root / "client.key"
    client_csr = root / "client.csr"
    client_conf = root / "client.cnf"

    ca_conf.write_text(
        "\n".join(
            [
                "[req]",
                "prompt = no",
                "distinguished_name = dn",
                "x509_extensions = v3_ca",
                "[dn]",
                "CN = MAF Runtime Test CA",
                "[v3_ca]",
                "basicConstraints = critical,CA:true",
                "keyUsage = critical,keyCertSign,cRLSign",
                "subjectKeyIdentifier = hash",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run_openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_cert),
        "-days",
        "1",
        "-config",
        str(ca_conf),
    )
    server_conf.write_text(
        "\n".join(
            [
                "[req]",
                "prompt = no",
                "distinguished_name = dn",
                "req_extensions = v3_req",
                "[dn]",
                "CN = localhost",
                "[v3_req]",
                "subjectAltName = @alt_names",
                "keyUsage = critical,digitalSignature,keyEncipherment",
                "extendedKeyUsage = serverAuth",
                "[alt_names]",
                "DNS.1 = localhost",
                "IP.1 = 127.0.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run_openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(server_key),
        "-out",
        str(server_csr),
        "-config",
        str(server_conf),
    )
    _run_openssl(
        "x509",
        "-req",
        "-in",
        str(server_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(server_cert),
        "-days",
        "1",
        "-sha256",
        "-extensions",
        "v3_req",
        "-extfile",
        str(server_conf),
    )
    client_conf.write_text(
        "\n".join(
            [
                "[req]",
                "prompt = no",
                "distinguished_name = dn",
                "req_extensions = v3_req",
                "[dn]",
                "CN = maf-python-runtime-client",
                "[v3_req]",
                "keyUsage = critical,digitalSignature",
                "extendedKeyUsage = clientAuth",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run_openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(client_key),
        "-out",
        str(client_csr),
        "-config",
        str(client_conf),
    )
    _run_openssl(
        "x509",
        "-req",
        "-in",
        str(client_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(client_cert),
        "-days",
        "1",
        "-sha256",
        "-extensions",
        "v3_req",
        "-extfile",
        str(client_conf),
    )
    return {
        "ca_cert": ca_cert,
        "server_cert": server_cert,
        "server_key": server_key,
        "client_cert": client_cert,
        "client_key": client_key,
    }


def _run_openssl(*args: str) -> None:
    subprocess.run(
        ["openssl", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _submission_task_record() -> dict[str, object]:
    return {
        "task_id": "task",
        "conversation_id": "conversation",
        "root_message_id": "message",
        "status": "accepted",
        "routing_mode": "auto",
        "requested_capability_id": None,
        "summary": None,
        "cancel_requested_at": None,
        "created_at": "2026-08-26T00:00:00Z",
        "updated_at": None,
        "assignment": None,
    }


def _submission_projection_bytes(
    content: str = "hello",
) -> tuple[bytes, bytes, bytes]:
    def canonical(value: object) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    conversation = canonical(
        {
            "conversation_id": "conversation",
            "create_if_missing": True,
            "created_at": "2026-08-26T00:00:00Z",
            "current_task_id": "task",
            "schema": "maf.submission.conversation_projection.v1",
            "status": "active",
            "updated_at": "2026-08-26T00:00:00Z",
            "username": "owner",
        }
    )
    message = canonical(
        {
            "content": content,
            "conversation_id": "conversation",
            "message_created_at": "2026-08-26T00:00:00Z",
            "message_id": "message",
            "message_type": "text",
            "metadata": {},
            "role": "user",
            "schema": "maf.submission.message_projection.v1",
            "stream_status": "complete",
            "task_id": "task",
            "updated_at": "2026-08-26T00:00:00Z",
        }
    )
    continuation = canonical(
        {
            "schema": "maf.submission.continuation.v1",
            "conversation_id": "conversation",
            "message_id": "message",
            "task_id": "task",
            "request_fingerprint": "a" * 64,
            "owner_scope": "owner",
            "message_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "routing_mode": "auto",
            "requested_capability_id": None,
            "model_options": {
                "model_edition": None,
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            "bundle_revisions": {
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            "execution_metadata": {
                "requested_capability_alias": None,
                "canonical_capability_id": None,
                "mcp_dispatch_server_id": None,
                "mcp_binding_mode": None,
                "mcp_command": None,
                "mcp_execution_mode": None,
                "mcp_rollout_config_version": None,
                "mcp_route_reason_code": None,
                "mcp_rollout_mode": None,
                "defer_task_completed_until_pending_skill_context_processed": None,
                "forced_by_mcp_command": None,
                "mcp_shadow_enabled": None,
            },
            "upload_refs": [],
            "sheet_selections": {},
            "mcp_binding": None,
            "mcp_assignment": None,
            "available_mcp_servers": [],
            "pending_context": None,
            "initial_no_server_eligible": False,
        }
    )
    return conversation, message, continuation


def _submission_admission_wire(
    *,
    task: dict[str, object],
    conversation: bytes,
    message: bytes,
    continuation: bytes,
    projection_sha: str,
    continuation_sha: str,
    projection_state: int = 1,
    preparation_state: int = 1,
    prepared_execution: bytes | None = None,
    prepared_sha: str | None = None,
    handoff_state: int = 1,
    handoff_kind: str | None = None,
    handoff_identity: str | None = None,
) -> bytes:
    payload = b"".join(
        [
            _field_string(1, "message"),
            _field_string(2, "task"),
            _field_string(3, "conversation"),
            _field_string(4, "owner"),
            _field_string(5, "a" * 64),
            _field_bytes(6, conversation),
            _field_bytes(7, message),
            _field_string(8, projection_sha),
            _field_bytes(9, continuation),
            _field_string(10, continuation_sha),
            _field_varint(11, projection_state),
            _field_varint(12, preparation_state),
            _field_varint(15, handoff_state),
            _field_varint(18, 1_000),
            _field_varint(19, 1_000),
            _field_varint(20, 0),
            _field_bytes(21, _task_record(task)),
            _field_string(22, "submission:message"),
        ]
    )
    if prepared_execution is not None:
        payload += _field_bytes(13, prepared_execution)
    if prepared_sha is not None:
        payload += _field_string(14, prepared_sha)
    if handoff_kind is not None:
        payload += _field_string(16, handoff_kind)
    if handoff_identity is not None:
        payload += _field_string(17, handoff_identity)
    return payload


def _prepared_execution_bytes() -> bytes:
    value = {
        "schema": "maf.submission.prepared_execution.v1",
        "task_id": "task",
        "conversation_id": "conversation",
        "message_id": "message",
        "prepared_kind": "agent_run",
        "owner_scope": "owner",
        "execution_text_source": "root_message",
        "execution_text_sha256": hashlib.sha256(b"hello").hexdigest(),
        "requested_capability_id": None,
        "initial_required_tool_name": None,
        "model_options": {
            "model_edition": None,
            "reasoning_effort": "medium",
            "thinking_enabled": False,
        },
        "bundle_revisions": {
            "skill_bundle_revision": None,
            "mcp_bundle_revision": None,
        },
        "execution_metadata": {
            "requested_capability_alias": None,
            "canonical_capability_id": None,
            "mcp_dispatch_server_id": None,
            "mcp_binding_mode": None,
            "mcp_command": None,
            "mcp_execution_mode": None,
            "mcp_rollout_config_version": None,
            "mcp_route_reason_code": None,
            "mcp_rollout_mode": None,
            "defer_task_completed_until_pending_skill_context_processed": None,
            "forced_by_mcp_command": None,
            "mcp_shadow_enabled": None,
        },
        "preparation_receipt": {
            "task_id": "task",
            "receipt_sha256": "1" * 64,
            "route_decision_sha256": "2" * 64,
            "memory_context_sha256": "3" * 64,
            "selector_decision_sha256": "4" * 64,
        },
        "upload_refs": [],
        "sheet_selections": {},
        "mcp_binding": None,
        "mcp_assignment": None,
        "available_mcp_servers": [],
        "pending_context": None,
        "planned_handoff_kind": "agent_run",
    }
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _message_identity_wire() -> bytes:
    return b"".join(
        [
            _field_string(1, "server-message"),
            _field_string(2, "conversation"),
            _field_string(3, "owner"),
            _field_varint(4, 3),
            _field_string(5, "assistant"),
            _field_string(6, "text"),
            _field_varint(7, 1_000),
            _field_string(8, "task"),
            _field_varint(10, 1_000),
        ]
    )


class _BytesSocket:
    def __init__(self, payload: bytes) -> None:
        self._payload = bytearray(payload)
        self.sent: list[bytes] = []

    def recv(self, size: int) -> bytes:
        chunk = bytes(self._payload[:size])
        del self._payload[:size]
        return chunk

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)


def _ensure_runtime_sidecar_binary() -> Path:
    binary = _repo_root() / "native" / "target" / "debug" / "maf-runtime-sidecar"
    subprocess.run(
        ["cargo", "build", "-p", "maf_runtime_sidecar", "--bin", "maf-runtime-sidecar"],
        cwd=_repo_root() / "native",
        check=True,
    )
    return binary


def _runtime_sidecar_artifact_metadata() -> dict[str, str]:
    contract = load_runtime_sidecar_contract()
    return {
        "source": "ci_pipeline",
        "artifact_kind": "sidecar_binary",
        "checksum_sha256": "sha256:runtime-sidecar",
        "sbom_digest": "sha256:sbom",
        "cargo_lock_digest": "sha256:cargo-lock",
        "proto_hash": artifact_policy()["expected_proto_hash"],
        "schema_hash": contract["schema_hash"],
        "provenance_attestation": "slsa-provenance",
    }


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]

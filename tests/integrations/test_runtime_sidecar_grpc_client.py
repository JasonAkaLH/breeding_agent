from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from src.storage.rust_contract import artifact_policy, load_runtime_sidecar_contract
from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient


class RuntimeSidecarGrpcClientIntegrationTest(unittest.TestCase):
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
                        "root_node_id": None,
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
                    self.assertEqual(active_task["task"], task_record)
                    self.assertFalse(
                        client.get_active_task_for_conversation(conversation_id="missing")["found"]
                    )
                    first_claim = client.claim_planner_replan(
                        task_id="task-authority",
                        decision_digest="a" * 64,
                        now="2026-08-18T10:00:00Z",
                    )["claim"]
                    retry_claim = client.claim_planner_replan(
                        task_id="task-authority",
                        decision_digest="a" * 64,
                        now="2026-08-18T10:01:00Z",
                    )["claim"]
                    second_claim = client.claim_planner_replan(
                        task_id="task-authority",
                        decision_digest="b" * 64,
                        now="2026-08-18T10:02:00Z",
                    )["claim"]
                    self.assertEqual(first_claim, retry_claim)
                    self.assertEqual(first_claim["planning_epoch"], "r1")
                    self.assertEqual(second_claim["planning_epoch"], "r2")
                    applied_claim = client.mark_planner_replan_claim(
                        task_id="task-authority",
                        decision_digest="a" * 64,
                        status="applied",
                        now="2026-08-18T10:03:00Z",
                    )["claim"]
                    self.assertEqual(applied_claim["status"], "applied")
                    self.assertEqual(
                        client.get_planner_replan_claim(
                            task_id="task-authority",
                            decision_digest="a" * 64,
                        )["claim"],
                        applied_claim,
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
                        "criticality": "required",
                        "dependency_type": "hard",
                        "retry_policy": {"max_attempts": 2},
                        "timeout_policy": {"seconds": 30},
                        "resource_class": "default",
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

                    edge = client.save_task_edge(
                        task_id="task",
                        from_node_id="node",
                        to_node_id="node-next",
                        edge_type="data",
                        condition="",
                        idempotency_key="edge-1",
                        owner="python-runtime",
                    )
                    self.assertEqual(edge["from_node_id"], "node")
                    self.assertEqual(client.list_task_edges(task_id="task")["edges"], [edge])

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

from __future__ import annotations

import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient


class RuntimeSidecarGrpcClientIntegrationTest(unittest.TestCase):
    def test_client_rejects_public_endpoint_before_connecting(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "runtime_store_unavailable"):
            RuntimeSidecarGrpcClient("http://example.com:50051")

    def test_python_client_appends_and_replays_against_rust_sidecar_binary(self) -> None:
        binary = _ensure_runtime_sidecar_binary()
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
                client = _connect_with_retry(endpoint)
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

                transitioned = client.transition_node(
                    task_id="task",
                    node_id="node",
                    to_status="running",
                    idempotency_key="node-1",
                    owner="python-runtime",
                )
                self.assertEqual(transitioned["status"], "running")

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
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _connect_with_retry(endpoint: str) -> RuntimeSidecarGrpcClient:
    last_error: Exception | None = None
    for _ in range(40):
        try:
            client = RuntimeSidecarGrpcClient(endpoint)
            client.version(timeout_seconds=1)
            return client
        except Exception as exc:  # noqa: BLE001 - retry startup race against Rust binary.
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"Rust runtime sidecar did not become ready: {last_error}")


def _ensure_runtime_sidecar_binary() -> Path:
    binary = _repo_root() / "native" / "target" / "debug" / "maf-runtime-sidecar"
    if binary.exists():
        return binary
    subprocess.run(
        ["cargo", "build", "-p", "maf_runtime_sidecar", "--bin", "maf-runtime-sidecar"],
        cwd=_repo_root() / "native",
        check=True,
    )
    return binary


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]

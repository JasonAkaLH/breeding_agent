from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any

from src.integrations.codex_skills import SkillScriptError, SkillScriptRunner, parse_skill_file
from src.integrations.codex_skills import skill_sandbox_client as sandbox_module
from src.integrations.codex_skills.skill_sandbox_client import SkillSandboxGrpcClient


class _FakeRustSandboxClient:
    def __init__(self, *, sandbox_root: Path | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sandbox_root = sandbox_root

    def execute_sandboxed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        if self.sandbox_root is not None:
            outputs_dir = self.sandbox_root / kwargs["cwd_under_public_root"] / "outputs"
            outputs_dir.mkdir(parents=True, exist_ok=True)
            (outputs_dir / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")
        return {
            "exit_code": 0,
            "stdout_prefix": json.dumps(
                {
                    "answer": "from rust sandbox",
                    "output_files": [{"path": "outputs/report.html", "mime_type": "text/html"}],
                }
            ).encode("utf-8"),
            "stderr_prefix": b"",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "error": None,
        }


class SkillSandboxClientIntegrationTest(unittest.TestCase):
    def test_enforce_mode_fails_closed_without_rust_sandbox_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = _write_python_skill(Path(tmpdir))
            runner = SkillScriptRunner(
                rust_sandbox_mode="enforce",
                rust_sandbox_root=Path(tmpdir),
            )

            with self.assertRaises(SkillScriptError) as context:
                asyncio.run(runner.run(manifest, manifest.scripts[0], {"query": "hello"}))

        self.assertEqual(context.exception.code, "skill_runtime_sandbox_unavailable")

    def test_enforce_mode_routes_script_execution_to_rust_sandbox_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = _write_python_skill(Path(tmpdir), body="raise SystemExit('legacy runner must not execute')")
            client = _FakeRustSandboxClient()
            runner = SkillScriptRunner(
                rust_sandbox_client=client,
                rust_sandbox_mode="enforce",
                rust_sandbox_root=Path(tmpdir),
            )

            result = asyncio.run(runner.run(manifest, manifest.scripts[0], {"query": "hello"}))

        self.assertEqual(result["answer"], "from rust sandbox")
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["skill_name"], "scripted")
        self.assertEqual(call["execution_mode"], "python_subprocess")
        self.assertEqual(call["stdin_payload"], b'{"query": "hello"}')
        self.assertEqual(call["argv"], ("./run-skill-python.sh",))

    def test_enforce_mode_preserves_output_processor_for_rust_sandbox_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = _write_python_skill(root, body="raise SystemExit('legacy runner must not execute')")
            seen: dict[str, Any] = {}

            async def processor(*, output, outputs_dir, manifest, script, context):
                seen["file_text"] = (outputs_dir / "report.html").read_text(encoding="utf-8")
                seen["context"] = dict(context)
                return {**output, "managed": True}

            runner = SkillScriptRunner(
                output_processor=processor,
                rust_sandbox_client=_FakeRustSandboxClient(sandbox_root=root),
                rust_sandbox_mode="enforce",
                rust_sandbox_root=root,
            )

            result = asyncio.run(
                runner.run(
                    manifest,
                    manifest.scripts[0],
                    {"query": "hello"},
                    output_context={"task_id": "task", "node_id": "node"},
                )
            )

        self.assertEqual(seen["file_text"], "<h1>ok</h1>")
        self.assertEqual(seen["context"], {"task_id": "task", "node_id": "node"})
        self.assertTrue(result["managed"])

    def test_h2c_client_rejects_oversized_grpc_response_payload(self) -> None:
        original_limit = sandbox_module._MAX_GRPC_RESPONSE_BYTES
        sandbox_module._MAX_GRPC_RESPONSE_BYTES = 8
        try:
            grpc_payload = b"\x00" + (9).to_bytes(4, "big") + b"x" * 9
            sock = _FakeSocket(
                sandbox_module._frame(
                    sandbox_module._FRAME_DATA,
                    sandbox_module._FLAG_END_STREAM,
                    1,
                    grpc_payload,
                )
            )
            with self.assertRaisesRegex(RuntimeError, "exceeded configured limit"):
                sandbox_module._read_grpc_response(sock)  # noqa: SLF001 - contract-level regression.
        finally:
            sandbox_module._MAX_GRPC_RESPONSE_BYTES = original_limit

    def test_h2c_client_rejects_truncated_grpc_message(self) -> None:
        grpc_payload = b"\x00" + (9).to_bytes(4, "big") + b"x" * 8
        sock = _FakeSocket(
            sandbox_module._frame(
                sandbox_module._FRAME_DATA,
                sandbox_module._FLAG_END_STREAM,
                1,
                grpc_payload,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "incomplete gRPC payload"):
            sandbox_module._read_grpc_response(sock)  # noqa: SLF001 - contract-level regression.

    def test_h2c_client_rejects_missing_or_short_grpc_message_header(self) -> None:
        for payload_size in range(5):
            with self.subTest(payload_size=payload_size):
                sock = _FakeSocket(
                    sandbox_module._frame(
                        sandbox_module._FRAME_DATA,
                        sandbox_module._FLAG_END_STREAM,
                        1,
                        b"x" * payload_size,
                    )
                )

                with self.assertRaisesRegex(RuntimeError, "complete gRPC message"):
                    sandbox_module._read_grpc_response(sock)  # noqa: SLF001 - contract-level regression.

    def test_execute_sandboxed_rejects_empty_success_shaped_response(self) -> None:
        class EmptyExecuteResponseClient(SkillSandboxGrpcClient):
            def __init__(self) -> None:
                pass

            def check_compatibility(self, *, timeout_seconds: float = 5) -> dict[str, Any]:
                return {"compatible": True}

            def _unary(self, method: str, protobuf_payload: bytes, *, timeout_seconds: float) -> bytes:
                return b""

        client = EmptyExecuteResponseClient()

        with self.assertRaisesRegex(RuntimeError, "empty ExecuteSandboxed response"):
            client.execute_sandboxed(
                skill_name="scripted",
                execution_mode="python_subprocess",
                cwd_under_public_root=".",
                argv=("./echo.sh",),
                timeout_ms=1_000,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                stdin_payload=b"{}",
            )

    def test_h2c_client_rejects_extra_grpc_message_bytes(self) -> None:
        first_message = b"\x00" + (1).to_bytes(4, "big") + b"x"
        unexpected_second_message = b"\x00" + (1).to_bytes(4, "big") + b"y"
        sock = _FakeSocket(
            sandbox_module._frame(
                sandbox_module._FRAME_DATA,
                sandbox_module._FLAG_END_STREAM,
                1,
                first_message + unexpected_second_message,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected trailing gRPC payload bytes"):
            sandbox_module._read_grpc_response(sock)  # noqa: SLF001 - contract-level regression.

    def test_h2c_client_rejects_server_version_range_without_current_client(self) -> None:
        contract = sandbox_module.load_skill_runtime_contract()
        version = {
            "component": contract["component"],
            "protocol_version": contract["protocol_version"],
            "schema_hash": contract["schema_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "supported_features": contract["supported_features"],
            "min_client_version": "9.0.0",
            "max_client_version": "9.1.x",
        }

        with self.assertRaisesRegex(RuntimeError, "client version"):
            sandbox_module._validate_handshake(version)  # noqa: SLF001 - contract-level regression.


    def test_client_constructor_validates_configured_artifact_provenance(self) -> None:
        contract = sandbox_module.load_skill_runtime_contract()
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "skill_sandbox_sidecar_binary",
            "checksum_sha256": "sha256:skill-sandbox",
            "cargo_lock_digest": "sha256:cargo-lock",
            "contract_version": contract["contract_version"],
            "bundle_revision": "skill-runtime-20260517.1",
            "schema_hash": contract["schema_hash"],
            "sbom_digest": "sha256:sbom",
            "provenance_attestation": "sha256:provenance",
        }

        client = SkillSandboxGrpcClient(
            "http://127.0.0.1:65535",
            artifact_provenance=metadata,
            allowed_artifact_checksums=("sha256:skill-sandbox",),
            allowed_cargo_lock_digests=("sha256:cargo-lock",),
        )

        self.assertEqual(client._authority, "127.0.0.1:65535")  # noqa: SLF001
        with self.assertRaisesRegex(RuntimeError, "skill_runtime_artifact_untrusted"):
            SkillSandboxGrpcClient(
                "http://127.0.0.1:65535",
                artifact_provenance={**metadata, "checksum_sha256": "sha256:tampered"},
                allowed_artifact_checksums=("sha256:skill-sandbox",),
                allowed_cargo_lock_digests=("sha256:cargo-lock",),
            )

    def test_python_client_executes_against_rust_skill_sandbox_binary(self) -> None:
        binary = _ensure_skill_sandbox_binary()
        port = _free_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            script = root / "echo.sh"
            script.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            script.chmod(0o755)
            process = subprocess.Popen(
                [
                    str(binary),
                    "--serve",
                    f"127.0.0.1:{port}",
                    "--sandbox-root",
                    str(root),
                ],
                cwd=_repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                client = _connect_with_retry(endpoint)
                version = client.version()
                self.assertEqual(version["component"], "maf_skill_runtime")
                client.check_compatibility()
                response = client.execute_sandboxed(
                    skill_name="scripted",
                    execution_mode="python_subprocess",
                    cwd_under_public_root=".",
                    argv=("./echo.sh",),
                    timeout_ms=5_000,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                    stdin_payload=b'{"answer":"from sidecar"}',
                )
                self.assertEqual(response["exit_code"], 0)
                self.assertEqual(response["stdout_prefix"], b'{"answer":"from sidecar"}')
                self.assertIsNone(response["error"])
            finally:
                _terminate_process(process)

    def test_python_client_validates_policy_against_rust_skill_sandbox_binary(self) -> None:
        binary = _ensure_skill_sandbox_binary()
        port = _free_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"

        with tempfile.TemporaryDirectory() as tmpdir:
            process = subprocess.Popen(
                [
                    str(binary),
                    "--serve",
                    f"127.0.0.1:{port}",
                    "--sandbox-root",
                    tmpdir,
                ],
                cwd=_repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                client = _connect_with_retry(endpoint)
                allowed = client.validate_policy(
                    skill_name="platform",
                    capability_id="skill.platform",
                    execution_mode="platform_service",
                    trust_scope="project",
                    handler="demo.handler",
                    manifest_services=("demo.service",),
                    runtime_allowlist_services=("demo.service",),
                    requested_services=("demo.service",),
                    runtime_allowlist_handlers=("demo.handler",),
                    x_runtime_rust={"adapter": "pyo3", "contract_version": "1"},
                )
                denied = client.validate_policy(
                    skill_name="platform",
                    capability_id="skill.platform",
                    execution_mode="platform_service",
                    trust_scope="project",
                    handler="demo.handler",
                    manifest_services=("demo.service",),
                    runtime_allowlist_services=("demo.service",),
                    requested_services=("demo.service",),
                    runtime_allowlist_handlers=(),
                    x_runtime_rust={"adapter": "pyo3", "contract_version": "1"},
                )
            finally:
                _terminate_process(process)

        self.assertTrue(allowed["allowed"])
        self.assertIsNone(allowed["error"])
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["error"]["code"], "skill_runtime_handler_not_allowlisted")

    def test_script_runner_executes_python_script_through_rust_skill_sandbox_binary(self) -> None:
        binary = _ensure_skill_sandbox_binary()
        port = _free_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = _write_python_skill(root)
            process = subprocess.Popen(
                [
                    str(binary),
                    "--serve",
                    f"127.0.0.1:{port}",
                    "--sandbox-root",
                    str(root),
                ],
                cwd=_repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                client = _connect_with_retry(endpoint)
                runner = SkillScriptRunner(
                    rust_sandbox_client=client,
                    rust_sandbox_mode="enforce",
                    rust_sandbox_root=root,
                )
                result = asyncio.run(runner.run(manifest, manifest.scripts[0], {"query": "hello"}))
            finally:
                _terminate_process(process)

        self.assertEqual(result, {"answer": "legacy hello"})


def _write_python_skill(root: Path, *, body: str | None = None):
    skill_dir = root / "scripted"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script_body = body or (
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'answer': 'legacy ' + payload['query']}))\n"
    )
    (scripts_dir / "echo.py").write_text(script_body, encoding="utf-8")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        textwrap.dedent(
            """
            ---
            name: scripted
            scripts:
              - name: echo
                path: scripts/echo.py
                auto_run: true
            outputs:
              required:
                - answer
            ---

            # Scripted
            Run script.
            """
        ).strip(),
        encoding="utf-8",
    )
    return parse_skill_file(skill_file)


def _connect_with_retry(endpoint: str) -> SkillSandboxGrpcClient:
    last_error: Exception | None = None
    for _ in range(40):
        try:
            client = SkillSandboxGrpcClient(endpoint)
            client.version(timeout_seconds=1)
            return client
        except Exception as exc:  # noqa: BLE001 - retry startup race against Rust binary.
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"Rust skill sandbox did not become ready: {last_error}")


def _ensure_skill_sandbox_binary() -> Path:
    binary = _repo_root() / "native" / "target" / "debug" / "maf-skill-sandbox"
    sources = [
        _repo_root() / "native" / "crates" / "maf_skill_runtime" / "src" / "lib.rs",
        _repo_root() / "native" / "crates" / "maf_skill_runtime" / "src" / "main.rs",
        _repo_root() / "native" / "proto" / "maf" / "skill" / "v1" / "skill_runtime.proto",
    ]
    if binary.exists() and binary.stat().st_mtime >= max(source.stat().st_mtime for source in sources):
        return binary
    subprocess.run(
        ["cargo", "build", "-p", "maf_skill_runtime", "--bin", "maf-skill-sandbox"],
        cwd=_repo_root() / "native",
        check=True,
    )
    return binary


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class _FakeSocket:
    def __init__(self, payload: bytes) -> None:
        self._payload = bytearray(payload)
        self.sent: list[bytes] = []

    def recv(self, size: int) -> bytes:
        if not self._payload:
            return b""
        chunk = self._payload[:size]
        del self._payload[:size]
        return bytes(chunk)

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    unittest.main()

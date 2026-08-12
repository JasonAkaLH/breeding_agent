from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from src.core.enums import TaskStatus
from src.core.models import EventRecord, Task
from src.orchestration.models import OrchestrationRequest
from src.storage.rust_contract import artifact_policy, load_runtime_sidecar_contract, mode_for_component
from tests.api.support import APITestCase
from tests.api.test_user_mcp_runtime_wiring import (
    _write_task_authority_migration_evidence,
)


class _RecordingDispatcherSidecarClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def pin_bundle_revision(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "bundle_revision_pin",
                {
                    "bundle_kind": bundle_kind,
                    "idempotency_key": idempotency_key,
                    "revision": revision,
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "bundle_revision_pin",
            "task_id": task_id,
            "bundle_kind": bundle_kind,
            "revision": revision,
            "released": False,
            "error": None,
        }

    def release_bundle_revision(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
        released_at_ms: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "bundle_revision_release",
                {
                    "bundle_kind": bundle_kind,
                    "idempotency_key": idempotency_key,
                    "revision": revision,
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "bundle_revision_release",
            "task_id": task_id,
            "bundle_kind": bundle_kind,
            "revision": revision,
            "released": True,
            "error": None,
        }


class _FailingDispatcherSidecarClient(_RecordingDispatcherSidecarClient):
    def pin_bundle_revision(
        self,
        *,
        task_id: str,
        bundle_kind: str,
        revision: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "bundle_revision_pin",
                {
                    "bundle_kind": bundle_kind,
                    "idempotency_key": idempotency_key,
                    "revision": revision,
                    "task_id": task_id,
                },
            )
        )
        raise RuntimeError("dispatcher_unavailable: simulated shadow sidecar outage")


class _RecordingRuntimeStoreSidecarClient(_RecordingDispatcherSidecarClient):
    def append_event(
        self,
        *,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload_json: bytes,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "event_append",
                {
                    "conversation_id": conversation_id,
                    "event_type": event_type,
                    "idempotency_key": idempotency_key,
                    "payload_bytes": str(len(payload_json)),
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "event_append",
            "cursor": {
                "conversation_id": conversation_id,
                "created_at_ms": 1,
                "sequence": 1,
                "task_id": task_id,
            },
            "error": None,
        }


class RuntimeSidecarContractAPITest(APITestCase):
    def _request(self, task_id: str = "task-bundle-pin") -> OrchestrationRequest:
        return OrchestrationRequest(
            task_id=task_id,
            conversation_id="conv-bundle-pin",
            root_message_id="msg-bundle-pin",
            user_message="bundle pin",
        )

    async def test_dispatcher_enforce_rejects_python_legacy_bundle_revision_pin_without_sidecar(self) -> None:
        request = self._request()

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "enforce"}):
            self.assertEqual(mode_for_component("task_dispatcher"), "enforce")
            with self.assertRaisesRegex(
                RuntimeError,
                "dispatcher_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                self.runtime._retain_task_skill_revision(request)  # noqa: SLF001 - validates runtime sidecar guard

        self.assertNotIn(request.task_id, self.runtime._task_skill_bundle_revisions)  # noqa: SLF001

    async def test_dispatcher_enforce_rejects_python_legacy_bundle_revision_release_without_sidecar(self) -> None:
        request = self._request("task-bundle-release")
        self.runtime._retain_task_skill_revision(request)  # noqa: SLF001 - sets up retained revision
        retained_revision = self.runtime._task_skill_bundle_revisions[request.task_id]  # noqa: SLF001
        await self.runtime.storage.save_task(
            Task(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                root_message_id=request.root_message_id,
                status=TaskStatus.COMPLETED,
            )
        )

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "dispatcher_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                await self.runtime._release_task_skill_revision_if_terminal(request.task_id)  # noqa: SLF001

        self.assertEqual(self.runtime._task_skill_bundle_revisions[request.task_id], retained_revision)  # noqa: SLF001

    async def test_runtime_configures_grpc_sidecar_client_from_deployment_endpoint_env(self) -> None:
        sentinel_client = object()
        with (
            patch.dict(os.environ, {"MAF_RUNTIME_SIDECAR_ENDPOINT": "http://127.0.0.1:65535"}),
            patch("src.api.runtime.RuntimeSidecarGrpcClient", return_value=sentinel_client) as client_factory,
        ):
            await self.reconfigure_runtime(enable_conversation_memory=False)

        self.assertIs(self.runtime.storage._runtime_sidecar_client, sentinel_client)  # noqa: SLF001
        client_factory.assert_called_once_with(
            "http://127.0.0.1:65535",
            config_source="environment_variable",
            allowed_hosts=(),
            mtls_enabled=False,
            tls_ca_path=None,
            tls_cert_path=None,
            tls_key_path=None,
            tls_server_name=None,
            artifact_provenance=None,
            allowed_artifact_checksums=(),
            allowed_cargo_lock_digests=(),
        )

    async def test_runtime_configures_mtls_grpc_sidecar_client_from_deployment_env(self) -> None:
        sentinel_client = object()
        with (
            patch.dict(
                os.environ,
                {
                    "MAF_RUNTIME_SIDECAR_ENDPOINT": "https://runtime.internal:50051",
                    "MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS": "runtime.internal",
                    "MAF_RUNTIME_SIDECAR_MTLS_ENABLED": "true",
                    "MAF_RUNTIME_SIDECAR_TLS_CA_PATH": "/etc/maf/ca.pem",
                    "MAF_RUNTIME_SIDECAR_TLS_CERT_PATH": "/etc/maf/client.pem",
                    "MAF_RUNTIME_SIDECAR_TLS_KEY_PATH": "/etc/maf/client.key",
                    "MAF_RUNTIME_SIDECAR_TLS_SERVER_NAME": "runtime.internal",
                },
            ),
            patch("src.api.runtime.RuntimeSidecarGrpcClient", return_value=sentinel_client) as client_factory,
        ):
            await self.reconfigure_runtime(enable_conversation_memory=False)

        self.assertIs(self.runtime.storage._runtime_sidecar_client, sentinel_client)  # noqa: SLF001
        client_factory.assert_called_once_with(
            "https://runtime.internal:50051",
            config_source="environment_variable",
            allowed_hosts=("runtime.internal",),
            mtls_enabled=True,
            tls_ca_path="/etc/maf/ca.pem",
            tls_cert_path="/etc/maf/client.pem",
            tls_key_path="/etc/maf/client.key",
            tls_server_name="runtime.internal",
            artifact_provenance=None,
            allowed_artifact_checksums=(),
            allowed_cargo_lock_digests=(),
        )

    async def test_runtime_enforce_requires_allowlisted_sidecar_artifact_manifest(self) -> None:
        migration_env = _write_task_authority_migration_evidence(self.workspace)
        with (
            patch.dict(
                os.environ,
                {
                    **migration_env,
                    "MAF_RUNTIME_SIDECAR_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH": "",
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH": "",
                },
            ),
            patch("src.api.runtime.RuntimeSidecarGrpcClient") as client_factory,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_artifact_untrusted: Rust runtime sidecar enforce mode requires",
            ):
                await self.reconfigure_runtime(enable_conversation_memory=False)

        client_factory.assert_not_called()

    async def test_runtime_enforce_validates_sidecar_artifact_allowlist_before_client_use(self) -> None:
        migration_env = _write_task_authority_migration_evidence(self.workspace)
        sentinel_client = object()
        manifest, allowlist, metadata = self._write_runtime_sidecar_artifact_trust_files()
        with (
            patch.dict(
                os.environ,
                {
                    **migration_env,
                    "MAF_RUNTIME_SIDECAR_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH": str(manifest),
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH": str(allowlist),
                },
            ),
            patch("src.api.runtime.RuntimeSidecarGrpcClient", return_value=sentinel_client) as client_factory,
        ):
            await self.reconfigure_runtime(enable_conversation_memory=False)

        self.assertIs(self.runtime.storage._runtime_sidecar_client, sentinel_client)  # noqa: SLF001
        client_factory.assert_called_once_with(
            "http://127.0.0.1:65535",
            config_source="environment_variable",
            allowed_hosts=(),
            mtls_enabled=False,
            tls_ca_path=None,
            tls_cert_path=None,
            tls_key_path=None,
            tls_server_name=None,
            artifact_provenance=metadata,
            allowed_artifact_checksums=("sha256:runtime-sidecar",),
            allowed_cargo_lock_digests=("sha256:cargo-lock",),
        )

    async def test_runtime_enforce_rejects_manifest_not_exactly_present_in_allowlist(self) -> None:
        migration_env = _write_task_authority_migration_evidence(self.workspace)
        manifest, allowlist, _metadata = self._write_runtime_sidecar_artifact_trust_files(
            allowlist_overrides={"git_commit": "different-commit"}
        )
        with (
            patch.dict(
                os.environ,
                {
                    **migration_env,
                    "MAF_RUNTIME_SIDECAR_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH": str(manifest),
                    "MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH": str(allowlist),
                },
            ),
            patch("src.api.runtime.RuntimeSidecarGrpcClient") as client_factory,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_artifact_untrusted: Rust runtime sidecar artifact manifest is not present",
            ):
                await self.reconfigure_runtime(enable_conversation_memory=False)

        client_factory.assert_not_called()

    async def test_dispatcher_enforce_routes_bundle_revision_to_configured_sidecar(self) -> None:
        sidecar = _RecordingDispatcherSidecarClient()
        await self.reconfigure_runtime(runtime_sidecar_client=sidecar, enable_conversation_memory=False)
        request = self._request("task-bundle-sidecar")

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "enforce"}):
            self.runtime._retain_task_skill_revision(request)  # noqa: SLF001 - validates sidecar routing
            await self.runtime.storage.save_task(
                Task(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    root_message_id=request.root_message_id,
                    status=TaskStatus.COMPLETED,
                )
            )
            await self.runtime._release_task_skill_revision_if_terminal(request.task_id)  # noqa: SLF001

        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["bundle_revision_pin", "bundle_revision_release"],
        )

    async def test_runtime_shadow_event_append_records_sanitized_audit(self) -> None:
        sidecar = _RecordingRuntimeStoreSidecarClient()
        await self.reconfigure_runtime(runtime_sidecar_client=sidecar, enable_conversation_memory=False)
        event = EventRecord(
            event_id="evt-api-shadow",
            conversation_id="conv-api-shadow",
            task_id="task-api-shadow",
            event_type="shadow",
            payload={"secret": "do-not-log", "safe": True},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "shadow"}):
            saved = await self.runtime.storage.append_event(event)

        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        shadow_records = [
            record for record in audit_records if record["event_type"] == "runtime.sidecar_shadow_diff"
        ]

        self.assertEqual(saved, event)
        self.assertEqual([call[0] for call in sidecar.calls], ["event_append"])
        self.assertEqual(len(await self.runtime.storage.list_event_page_for_task(event.task_id)), 1)
        self.assertEqual(shadow_records[-1]["payload"]["component"], "event_log")
        self.assertEqual(shadow_records[-1]["payload"]["operation"], "event_append")
        self.assertEqual(shadow_records[-1]["payload"]["legacy_status"], "ok")
        self.assertEqual(shadow_records[-1]["payload"]["rust_status"], "ok")
        self.assertNotIn("do-not-log", json.dumps(shadow_records[-1], ensure_ascii=False))

    async def test_dispatcher_shadow_records_bundle_pin_release_audit_after_legacy_revision(self) -> None:
        sidecar = _RecordingDispatcherSidecarClient()
        await self.reconfigure_runtime(runtime_sidecar_client=sidecar, enable_conversation_memory=False)
        request = self._request("task-bundle-shadow")

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "shadow"}):
            self.runtime._retain_task_skill_revision(request)  # noqa: SLF001 - validates shadow routing
            await self.runtime.storage.save_task(
                Task(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    root_message_id=request.root_message_id,
                    status=TaskStatus.COMPLETED,
                )
            )
            await self.runtime._release_task_skill_revision_if_terminal(request.task_id)  # noqa: SLF001

        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        shadow_records = [
            record for record in audit_records if record["event_type"] == "runtime.sidecar_shadow_diff"
        ]

        self.assertEqual([call[0] for call in sidecar.calls], ["bundle_revision_pin", "bundle_revision_release"])
        self.assertNotIn(request.task_id, self.runtime._task_skill_bundle_revisions)  # noqa: SLF001
        self.assertEqual(
            [record["payload"]["operation"] for record in shadow_records[-2:]],
            ["bundle_revision_pin", "bundle_revision_release"],
        )
        self.assertTrue(all(record["payload"]["component"] == "task_dispatcher" for record in shadow_records[-2:]))
        self.assertTrue(all(record["payload"]["rust_status"] == "ok" for record in shadow_records[-2:]))

    async def test_dispatcher_shadow_sidecar_error_does_not_block_legacy_revision_retain(self) -> None:
        sidecar = _FailingDispatcherSidecarClient()
        await self.reconfigure_runtime(runtime_sidecar_client=sidecar, enable_conversation_memory=False)
        request = self._request("task-bundle-shadow-error")

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "shadow"}):
            self.runtime._retain_task_skill_revision(request)  # noqa: SLF001 - validates non-blocking shadow

        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        shadow_records = [
            record for record in audit_records if record["event_type"] == "runtime.sidecar_shadow_diff"
        ]

        self.assertIn(request.task_id, self.runtime._task_skill_bundle_revisions)  # noqa: SLF001
        self.assertEqual(sidecar.calls[0][0], "bundle_revision_pin")
        self.assertEqual(shadow_records[-1]["payload"]["operation"], "bundle_revision_pin")
        self.assertEqual(shadow_records[-1]["payload"]["rust_status"], "error")
        self.assertEqual(shadow_records[-1]["payload"]["error_code"], "dispatcher_unavailable")

    def _write_runtime_sidecar_artifact_trust_files(
        self,
        *,
        allowlist_overrides: dict[str, object] | None = None,
    ) -> tuple[Path, Path, dict[str, str]]:
        contract = load_runtime_sidecar_contract()
        proto_hash = artifact_policy()["expected_proto_hash"]
        manifest_payload = {
            "schema_version": "maf.rust_artifact_provenance.v1",
            "component": "maf_runtime_sidecar",
            "artifact_id": "maf_runtime_sidecar",
            "artifact_kind": "sidecar_binary",
            "artifact_name": "maf-runtime-sidecar-linux-x86_64",
            "artifact_sha256": "sha256:runtime-sidecar",
            "cargo_lock_sha256": "sha256:cargo-lock",
            "sbom_sha256": "sha256:sbom",
            "provenance_sha256": "sha256:provenance",
            "source": "ci_pipeline",
            "git_commit": "abcdef123456",
            "toolchain": "rustc 1.95.0",
            "target_triple": "x86_64-unknown-linux-gnu",
            "build_profile": "release",
            "cargo_features": ["default"],
            "contract_hashes": {"runtime_sidecar": contract["schema_hash"]},
            "proto_hashes": {"runtime": proto_hash},
        }
        allowlist_entry = dict(manifest_payload)
        if allowlist_overrides:
            allowlist_entry.update(allowlist_overrides)
        allowlist_payload = {
            "schema_version": "maf.rust_artifact_allowlist.v1",
            "allowed_artifacts": [allowlist_entry],
        }
        manifest_path = self.workspace / "runtime-sidecar.manifest.json"
        allowlist_path = self.workspace / "runtime-sidecar.allowlist.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        allowlist_path.write_text(json.dumps(allowlist_payload), encoding="utf-8")
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "sidecar_binary",
            "checksum_sha256": "sha256:runtime-sidecar",
            "sbom_digest": "sha256:sbom",
            "cargo_lock_digest": "sha256:cargo-lock",
            "proto_hash": proto_hash,
            "schema_hash": contract["schema_hash"],
            "provenance_attestation": "sha256:provenance",
        }
        return manifest_path, allowlist_path, metadata

from __future__ import annotations

import os
from unittest.mock import patch

from src.core.enums import TaskStatus
from src.core.models import Task
from src.orchestration.models import OrchestrationRequest
from src.storage.rust_contract import mode_for_component
from tests.api.support import APITestCase


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
        )

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

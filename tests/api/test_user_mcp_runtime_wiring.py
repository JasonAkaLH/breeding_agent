from __future__ import annotations

import os
import gc
import hashlib
import hmac
import inspect
import json
import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import src.api.runtime as api_runtime
from src.api.dto import SubmitMessageRequest
from src.api.runtime import build_api_runtime
from src.core.enums import TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import Task, UserMCPServer
from src.orchestration.models import OrchestrationRequest
from src.integrations.master_key import MasterKeyError
from src.integrations.mcp.credentials import CredentialSecurityError
from tests.api.support import InMemoryTaskRuntimeSidecar
from src.storage.rust_contract import load_runtime_sidecar_contract, migration_policy


def _write_task_authority_migration_evidence(root: Path) -> dict[str, str]:
    contract = load_runtime_sidecar_contract()
    policy = migration_policy()
    digest = "a" * 64
    plan = {
        "target_schema_version": contract["schema_hash"],
        "components": {
            component: {evidence: True for evidence in policy["required_evidence"]}
            for component in policy["required_components"]
        },
        "task_authority_cutover": {
            "backfill_import_complete": True,
            "task_inventory": {
                "legacy_count": 1,
                "sidecar_count": 1,
                "legacy_canonical_digest": digest,
                "sidecar_canonical_digest": digest,
            },
            "task_node_inventory": {
                "legacy_count": 1,
                "sidecar_count": 1,
                "legacy_canonical_digest": digest,
                "sidecar_canonical_digest": digest,
            },
            "legacy_null_assignment_resolution": {
                "resolution_complete": True,
                "active_count": 0,
                "active_canonical_digest": hashlib.sha256(b"[]").hexdigest(),
                "terminal_historical_count": 1,
                "terminal_historical_canonical_digest": digest,
                "terminal_historical_remains_unassigned": True,
            },
        },
    }
    key = b"test-deployment-owned-key-32bytes!"
    unsigned = {
        "schema": policy["task_authority_evidence_schema"],
        "component": contract["component"],
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
        "key_id": "test-key-v1",
        "migration_plan": plan,
    }
    signed = {
        **unsigned,
        "hmac_sha256": hmac.new(
            key,
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest(),
    }
    evidence_path = root / "task-migration-evidence.json"
    key_path = root / "task-migration-evidence.key"
    evidence_path.write_text(json.dumps(signed), encoding="utf-8")
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    return {
        policy["task_authority_evidence_path_env"]: str(evidence_path),
        policy["task_authority_hmac_key_path_env"]: str(key_path),
    }


class _LegacyMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self):
        return [
            {
                "name": "search_customer",
                "description": "legacy CRM lookup",
                "inputSchema": {"type": "object", "additionalProperties": True},
            }
        ]

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        return {"content": [{"type": "text", "text": "legacy result"}]}

    async def close(self) -> None:
        return None


_LEGACY_MCP_CONFIG = {
    "enabled": True,
    "servers": [
        {
            "server_id": "crm",
            "endpoint": "https://legacy.invalid/mcp",
            "tools": [
                {
                    "tool_name": "search_customer",
                    "expose": True,
                    "capability_id": "mcp.crm.search_customer",
                    "public_name": "Legacy CRM",
                    "public_description": "系统 CRM 查询能力",
                    "risk_level": "read_only",
                    "planner_allowed_fields": [],
                }
            ],
        }
    ],
}


class UserMCPRuntimeWiringTest(unittest.IsolatedAsyncioTestCase):
    def test_dev_unix_cp7a_can_skip_sidecar_artifact_attestation(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAF_API_ENV": "dev",
                "MAF_RUNTIME_SIDECAR_ENDPOINT": "unix:///run/maf-runtime-sidecar/runtime.sock",
                "MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH": "",
                "MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH": "",
                "MAF_RUST_RUNTIME_STORE_MODE": "off",
                "MAF_RUST_EVENT_LOG_MODE": "off",
                "MAF_RUST_TASK_DISPATCHER_MODE": "off",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "main-cp7a-user-scoped-v1",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            self.assertEqual(
                api_runtime._resolve_runtime_sidecar_artifact_trust_from_env(
                    require_runtime_store_attestation=True,
                ),
                (None, (), ()),
            )

    def test_sidecar_artifact_attestation_bypass_is_dev_local_only(self) -> None:
        baseline = {
            "MAF_API_ENV": "dev",
            "MAF_RUNTIME_SIDECAR_ENDPOINT": "unix:///run/maf-runtime-sidecar/runtime.sock",
            "MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH": "",
            "MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH": "",
            "MAF_RUST_RUNTIME_STORE_MODE": "off",
            "MAF_RUST_EVENT_LOG_MODE": "off",
            "MAF_RUST_TASK_DISPATCHER_MODE": "off",
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
            "MCP_ROUTING_MODE": "enforce",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
            "MCP_ENFORCE_COHORTS": "",
            "MCP_ENFORCE_PERCENT": "100",
            "MCP_ENFORCE_HASH_SALT": "main-cp7a-user-scoped-v1",
            "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
        }
        hostile_overrides = (
            {"MAF_API_ENV": "production"},
            {"MAF_RUNTIME_SIDECAR_ENDPOINT": "http://127.0.0.1:50051"},
            {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"},
            {"MAF_RUST_EVENT_LOG_MODE": "enforce"},
            {"MAF_RUST_TASK_DISPATCHER_MODE": "shadow"},
            {"MCP_ROUTING_MODE": "shadow"},
            {"MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true"},
            {"MCP_ENFORCE_COHORTS": "test-cohort"},
            {"MCP_ENFORCE_PERCENT": "99"},
            {"MCP_ENFORCE_HASH_SALT": "other-salt"},
            {"MCP_ENFORCE_COHORT_CONFIG_FILE": "/tmp/cohorts.json"},
        )
        for override in hostile_overrides:
            with self.subTest(override=override), patch.dict(
                os.environ,
                {**baseline, **override},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime_store_artifact_untrusted",
                ):
                    api_runtime._resolve_runtime_sidecar_artifact_trust_from_env(
                        require_runtime_store_attestation=True,
                    )

    async def test_corrupt_terminal_result_store_blocks_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            terminal_root = root / "terminal-results"
            terminal_root.mkdir(mode=0o700)
            (terminal_root / "unexpected.json").write_text("{}", encoding="utf-8")
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"a" * 32,
                user_mcp_terminal_result_store_path=terminal_root,
                planner_text_generator=lambda _prompt, **_kwargs: '{"nodes":[]}',
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            try:
                with self.assertRaisesRegex(
                    ValueError, "unexpected artifact"
                ):
                    await runtime.start()
            finally:
                await runtime.shutdown()

    async def test_internal_explicit_dispatch_without_server_converges_without_network(self) -> None:
        calls: list[str] = []

        def forbidden_client_factory(_server):
            calls.append("network")
            raise AssertionError("no MCP client may be constructed")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"a" * 32,
                mcp_client_factory=forbidden_client_factory,
                planner_text_generator=lambda _prompt, **_kwargs: '{"nodes":[]}',
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            try:
                response = await runtime.submit_chat_message(
                    "conv-no-server",
                    SubmitMessageRequest.model_construct(
                        conversation_id="conv-no-server",
                        content="必须使用 MCP",
                        routing_mode="force_capability",
                        capability_id="mcp.dispatch",
                        metadata={},
                    ),
                    authenticated_username="alice",
                )
                task = await runtime.storage.get_task(str(response["task_id"]))
                self.assertEqual(response["status"], "accepted")
                self.assertEqual(str(task.status), "failed")
                self.assertEqual(task.mcp_execution_mode, "unavailable")
                self.assertEqual(task.mcp_route_reason_code, "no_user_scoped_server")
                self.assertEqual(calls, [])
                self.assertEqual(
                    [
                        event.event_type
                        for event in await runtime.storage.list_events_for_task(task.task_id)
                        if event.event_type in {"mcp.runtime_unavailable", "task.failed"}
                    ],
                    ["mcp.runtime_unavailable", "task.failed"],
                )
            finally:
                await runtime.shutdown()

    async def test_runtime_store_off_does_not_require_cutover_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_RUST_RUNTIME_STORE_MODE": "off",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH": "",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH": "",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "off",
            },
            clear=False,
        ):
            root = Path(directory)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"r" * 32,
                mcp_config={"enabled": False},
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            try:
                self.assertEqual(runtime.storage._mcp_task_authority_mode, "off")
            finally:
                await runtime.shutdown()

    def test_canonical_active_modes_require_runtime_sidecar_at_assembly(self) -> None:
        for routing_mode in ("shadow", "enforce"):
            with self.subTest(routing_mode=routing_mode), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_API_ENV": "test",
                    "MAF_RUNTIME_SIDECAR_ENDPOINT": "",
                    "MAF_RUST_RUNTIME_STORE_MODE": "off",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                    "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                    "MCP_ROUTING_MODE": routing_mode,
                    "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                    "MCP_ENFORCE_COHORTS": "",
                    "MCP_ENFORCE_PERCENT": "100",
                    "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                    "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
                },
                clear=False,
            ):
                root = Path(directory)
                key_path = root / "mcp.key"
                key_path.write_text(
                    "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                    encoding="ascii",
                )
                key_path.chmod(0o600)

                with self.assertRaisesRegex(
                    RuntimeError,
                    f"MCP_ROUTING_MODE={routing_mode} requires a Rust runtime sidecar client",
                ):
                    build_api_runtime(
                        database_path=root / "runtime.sqlite3",
                        audit_log_path=root / "audit.jsonl",
                        master_key_bytes=b"a" * 32,
                        mcp_config={"enabled": False},
                        enable_platform_llm=False,
                        enable_llm_planner=False,
                        enable_conversation_title_llm=False,
                        enable_conversation_memory=False,
                    )

    async def test_runtime_store_authority_is_independent_from_rollout_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_RUNTIME_SIDECAR_ENDPOINT": "",
                "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "off",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "0",
                "MCP_ENFORCE_HASH_SALT": "",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            root = Path(directory)
            os.environ.update(_write_task_authority_migration_evidence(root))
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"r" * 32,
                mcp_config={"enabled": False},
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            task = Task(
                task_id="task-canonical-off",
                conversation_id="conv-canonical-off",
                root_message_id="msg-canonical-off",
                status=TaskStatus.ACCEPTED,
                requested_capability_id="mcp.dispatch",
                mcp_execution_mode="legacy",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="mcp-rollout-off:test",
                mcp_route_reason_code="routing_off",
                mcp_rollout_mode="off",
            )
            try:
                self.assertEqual(runtime.storage._mcp_task_authority_mode, "enforce")
                self.assertEqual(await runtime.storage.save_task(task), task)
                self.assertEqual(await runtime.storage.get_task(task.task_id), task)
            finally:
                await runtime.shutdown()

    def test_runtime_store_enforce_fails_closed_without_migration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH": "",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH": "",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "off",
            },
            clear=False,
        ):
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "runtime_store_migration_blocked"):
                build_api_runtime(
                    database_path=root / "runtime.sqlite3",
                    audit_log_path=root / "audit.jsonl",
                    master_key_bytes=b"r" * 32,
                    mcp_config={"enabled": False},
                    enable_platform_llm=False,
                    enable_llm_planner=False,
                    enable_conversation_title_llm=False,
                    enable_conversation_memory=False,
                    runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
                )

    def test_runtime_store_enforce_rejects_hostile_cutover_inventory_evidence(self) -> None:
        for hostile_case in ("count", "digest", "active_null"):
            with self.subTest(hostile_case=hostile_case), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_API_ENV": "test",
                    "MAF_RUST_RUNTIME_STORE_MODE": "enforce",
                    "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                    "MCP_ROUTING_MODE": "off",
                },
                clear=False,
            ):
                root = Path(directory)
                evidence_env = _write_task_authority_migration_evidence(root)
                evidence_path = Path(evidence_env["MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH"])
                key_path = Path(evidence_env["MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH"])
                artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
                cutover = artifact["migration_plan"]["task_authority_cutover"]
                if hostile_case == "count":
                    cutover["task_inventory"]["sidecar_count"] = 2
                elif hostile_case == "digest":
                    cutover["task_node_inventory"]["sidecar_canonical_digest"] = "f" * 64
                else:
                    cutover["legacy_null_assignment_resolution"]["active_count"] = 1
                unsigned = {key: value for key, value in artifact.items() if key != "hmac_sha256"}
                artifact["hmac_sha256"] = hmac.new(
                    key_path.read_bytes(),
                    json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
                    hashlib.sha256,
                ).hexdigest()
                evidence_path.write_text(json.dumps(artifact), encoding="utf-8")
                os.environ.update(evidence_env)

                with self.assertRaisesRegex(RuntimeError, "runtime_store_migration_blocked"):
                    build_api_runtime(
                        database_path=root / "runtime.sqlite3",
                        audit_log_path=root / "audit.jsonl",
                        master_key_bytes=b"r" * 32,
                        mcp_config={"enabled": False},
                        enable_platform_llm=False,
                        enable_llm_planner=False,
                        enable_conversation_title_llm=False,
                        enable_conversation_memory=False,
                        runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
                    )

    async def test_runtime_store_shadow_does_not_require_cutover_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_RUST_RUNTIME_STORE_MODE": "shadow",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_PATH": "",
                "MAF_RUST_RUNTIME_MIGRATION_EVIDENCE_HMAC_KEY_PATH": "",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "off",
            },
            clear=False,
        ):
            root = Path(directory)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"r" * 32,
                mcp_config={"enabled": False},
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            try:
                self.assertEqual(runtime.storage._mcp_task_authority_mode, "shadow")
            finally:
                await runtime.shutdown()

    async def test_custom_server_percent_miss_is_unavailable_without_legacy_fallback(self) -> None:
        planner_prompts: list[str] = []
        legacy_client = _LegacyMCPClient()

        def planner(prompt, **_kwargs):
            planner_prompts.append(prompt)
            return '{"nodes":[]}'

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "0",
                "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"a" * 32,
                mcp_config=_LEGACY_MCP_CONFIG,
                mcp_client_factory=lambda _server: legacy_client,
                planner_text_generator=planner,
                main_agent_stream_generator=lambda _prompt, **_kwargs: "done",
                main_agent_llm_config={
                    "model_editions": {
                        "default": "test",
                        "options": [
                            {
                                "value": "test",
                                "label": "Test",
                                "reasoning_efforts": {
                                    "default": "minimal",
                                    "disabled_default": "minimal",
                                    "options": [
                                        {
                                            "value": "minimal",
                                            "label": "Minimal",
                                            "allow_when_thinking_disabled": True,
                                        }
                                    ],
                                },
                                "agent_capabilities": {
                                    "supports_messages": True,
                                    "roles": ["system", "developer", "user", "assistant", "tool"],
                                    "supports_native_tools": True,
                                    "supports_required_tool_choice": True,
                                    "supports_streamed_tool_calls": True,
                                },
                            }
                        ],
                    }
                },
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            now = datetime(2026, 8, 13, 12, 0, 0)
            await runtime.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id="custom-crm",
                    owner_user_id="alice",
                    display_name="Custom CRM",
                    routing_description="系统 CRM 查询能力",
                    endpoint_url="https://custom.invalid/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    health_status=UserMCPHealthStatus.AVAILABLE,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                _, automatic = await runtime.submit_message(
                    "conv-auto",
                    SubmitMessageRequest(
                        conversation_id="conv-auto",
                        content="查询客户",
                    ),
                    authenticated_username="alice",
                )
                automatic_handle = runtime._running_tasks.get(automatic.task_id)
                if automatic_handle is not None:
                    await automatic_handle

                self.assertEqual(automatic.mcp_execution_mode, "unavailable")
                self.assertEqual(
                    automatic.mcp_route_reason_code,
                    "user_server_rollout_unavailable",
                )
                self.assertEqual(legacy_client.calls, [])
                self.assertTrue(planner_prompts)
                self.assertNotIn("mcp.crm.search_customer", planner_prompts[-1])
                automatic_events = await runtime.storage.list_events_for_task(
                    automatic.task_id
                )
                self.assertNotIn(
                    "mcp.runtime_unavailable",
                    {event.event_type for event in automatic_events},
                )

                _, explicit = await runtime.submit_message(
                    "conv-explicit",
                    SubmitMessageRequest(
                        conversation_id="conv-explicit",
                        content="查询客户",
                        routing_mode="force_capability",
                        capability_id="mcp.crm.search_customer",
                    ),
                    authenticated_username="alice",
                )

                self.assertEqual(explicit.mcp_execution_mode, "legacy")
                self.assertEqual(
                    explicit.mcp_route_reason_code,
                    "explicit_legacy_capability",
                )
            finally:
                await runtime.shutdown()

    async def test_legacy_assembly_off_builds_no_global_runtime_or_startup_discovery(self) -> None:
        calls: list[str] = []

        def forbidden_client_factory(_server):
            calls.append("client")
            raise AssertionError("legacy MCP client must not be constructed")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
                "MAF_USER_MCP_RESULT_PARSER_MODE": "safe_hide",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"a" * 32,
                mcp_config={
                    "enabled": True,
                    "servers": [
                        {
                            "server_id": "legacy",
                            "endpoint": "https://legacy.invalid/mcp",
                        }
                    ],
                },
                mcp_client_factory=forbidden_client_factory,
                planner_text_generator=lambda _prompt, **_kwargs: '{"action":"finish"}',
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            try:
                self.assertIsNone(runtime._mcp_runtime_state)
                self.assertEqual(calls, [])
                self.assertIsNotNone(runtime.capability_registry.get("mcp.dispatch"))
                self.assertIsNotNone(runtime.user_mcp_gateway._result_service)
                self.assertIsNotNone(
                    runtime.mcp_remote_task_recovery_worker._result_processor
                )
                self.assertIsNotNone(
                    runtime._mcp_durable_result_lifecycle_manager._business_reprojector
                )
                self.assertFalse(
                    any(
                        descriptor.kind == "mcp_tool"
                        for descriptor in runtime.capability_registry.list()
                    )
                )
            finally:
                await runtime.shutdown()

    async def test_authenticated_submission_persists_authoritative_enforce_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_API_ENV": "test",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_COHORTS": "",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "stable-test-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": "",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                master_key_bytes=b"a" * 32,
                mcp_config={"enabled": False},
                planner_text_generator=lambda _prompt, **_kwargs: '{"nodes":[]}',
                main_agent_stream_generator=lambda _prompt, **_kwargs: "done",
                main_agent_llm_config={
                    "model_editions": {
                        "default": "test",
                        "options": [
                            {
                                "value": "test",
                                "label": "Test",
                                "reasoning_efforts": {
                                    "default": "minimal",
                                    "disabled_default": "minimal",
                                    "options": [
                                        {
                                            "value": "minimal",
                                            "label": "Minimal",
                                            "allow_when_thinking_disabled": True,
                                        }
                                    ],
                                },
                                "agent_capabilities": {
                                    "supports_messages": True,
                                    "roles": ["system", "developer", "user", "assistant", "tool"],
                                    "supports_native_tools": True,
                                    "supports_required_tool_choice": True,
                                    "supports_streamed_tool_calls": True,
                                },
                            }
                        ],
                    }
                },
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
            )
            try:
                _, task = await runtime.submit_message(
                    "conv-route",
                    SubmitMessageRequest(
                        conversation_id="conv-route",
                        content="hello",
                        metadata={
                            "mcp_execution_mode": "legacy",
                            "mcp_rollout_mode": "off",
                        },
                    ),
                    authenticated_username="alice",
                )
                stored = await runtime.storage.get_task(task.task_id)

                self.assertEqual(stored.mcp_execution_mode, "user_scoped")
                self.assertEqual(stored.mcp_rollout_mode, "enforce")
                self.assertFalse(stored.mcp_shadow_enabled)
                self.assertEqual(stored.mcp_route_reason_code, "enforce_selected")
                events = await runtime.storage.list_events_for_task(task.task_id)
                route_event = next(
                    event
                    for event in events
                    if event.event_type == "mcp.rollout.route_assigned"
                )
                self.assertNotIn("alice", repr(route_event.payload))
                self.assertEqual(route_event.payload["real_path"], "user_scoped")
            finally:
                await runtime.shutdown()

    def test_runtime_fails_closed_without_master_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(MasterKeyError, "maf_master_key_file_missing"):
                build_api_runtime(
                    database_path=Path(directory) / "runtime.sqlite3",
                    audit_log_path=Path(directory) / "audit.jsonl",
                    enable_user_mcp=True,
                    enable_platform_llm=False,
                    enable_llm_planner=False,
                    enable_conversation_title_llm=False,
                    enable_conversation_memory=False,
                )

    def test_runtime_signature_exposes_only_master_key_authorities(self) -> None:
        parameters = inspect.signature(build_api_runtime).parameters

        self.assertIn("master_key_file", parameters)
        self.assertIn("master_key_bytes", parameters)
        self.assertNotIn("auth_token_hash_secret", parameters)
        self.assertNotIn("auth_token_hash_secret_required", parameters)
        self.assertNotIn("user_mcp_credential_key_file", parameters)

    def test_legacy_master_key_authorities_reject_even_empty_before_database(self) -> None:
        legacy_keys = (
            "MCP_CREDENTIAL_KEY_FILE_HOST",
            "MCP_CREDENTIAL_KEY_FILE",
            "MAF_AUTH_TOKEN_HASH_SECRET",
            "MAF_AUTH_TOKEN_HASH_SECRET_REQUIRED",
        )
        for legacy_key in legacy_keys:
            with self.subTest(legacy_key=legacy_key), tempfile.TemporaryDirectory() as directory:
                database_path = Path(directory) / "must-not-exist.sqlite3"
                with patch.dict(
                    os.environ,
                    {legacy_key: ""},
                    clear=True,
                ), self.assertRaisesRegex(
                    MasterKeyError,
                    "maf_master_key_legacy_authority_configured",
                ):
                    build_api_runtime(
                        database_path=database_path,
                        audit_log_path=Path(directory) / "audit.jsonl",
                        master_key_bytes=b"a" * 32,
                        enable_platform_llm=False,
                        enable_llm_planner=False,
                        enable_conversation_title_llm=False,
                        enable_conversation_memory=False,
                    )
                self.assertFalse(database_path.exists())

    def test_master_key_file_and_bytes_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                build_api_runtime(
                    database_path=Path(directory) / "runtime.sqlite3",
                    audit_log_path=Path(directory) / "audit.jsonl",
                    master_key_file=Path(directory) / "master.key",
                    master_key_bytes=b"a" * 32,
                    enable_platform_llm=False,
                    enable_llm_planner=False,
                    enable_conversation_title_llm=False,
                    enable_conversation_memory=False,
                )

    async def test_enabled_feature_verifies_sentinel_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text("YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=", encoding="ascii")
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                enable_user_mcp=True,
                master_key_bytes=b"a" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            await runtime.start()
            try:
                self.assertIsNotNone(runtime.user_mcp_config_service)
                self.assertIsNotNone(
                    await runtime.storage.get_maf_master_key_validation()
                )
            finally:
                await runtime.shutdown()
            del runtime
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                gc.collect()
            self.assertEqual(
                [warning for warning in caught if warning.category is ResourceWarning],
                [],
            )

    async def test_wrong_master_key_rejects_existing_sentinel_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_STATE_PLATFORM_CONFIG_BRIDGE": "0",
            },
            clear=True,
        ):
            root = Path(directory)
            database_path = root / "runtime.sqlite3"
            first = build_api_runtime(
                database_path=database_path,
                audit_log_path=root / "first-audit.jsonl",
                master_key_bytes=b"a" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                skill_roots=(),
            )
            await first.start()
            token = (await first.username_token_service.login_username("alice"))[1]
            await first.shutdown()

            same_key = build_api_runtime(
                database_path=database_path,
                audit_log_path=root / "second-audit.jsonl",
                master_key_bytes=b"a" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                skill_roots=(),
            )
            await same_key.start()
            try:
                authenticated = await same_key.username_token_service.get_current_token(
                    token,
                    touch=False,
                )
                self.assertEqual(authenticated.username, "alice")
            finally:
                await same_key.shutdown()

            wrong_key = build_api_runtime(
                database_path=database_path,
                audit_log_path=root / "wrong-audit.jsonl",
                master_key_bytes=b"b" * 32,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
                skill_roots=(),
            )
            with self.assertRaisesRegex(
                CredentialSecurityError,
                "maf_master_key_mismatch",
            ):
                await wrong_key.start()

    async def test_routing_flag_registers_only_dispatch_and_injects_safe_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                enable_user_mcp=True,
                enable_user_mcp_routing=True,
                master_key_bytes=b"a" * 32,
                planner_text_generator=lambda _prompt, **_kwargs: '{"action":"finish","reason":"done"}',
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            now = datetime(2026, 8, 12, 12, 0, 0)
            await runtime.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id="server-a",
                    owner_user_id="alice",
                    display_name="CRM",
                    routing_description="客户查询",
                    endpoint_url="https://secret.invalid/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    health_status=UserMCPHealthStatus.AVAILABLE,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                self.assertIsNotNone(runtime.capability_registry.get("mcp.dispatch"))
                self.assertIsNotNone(runtime._mcp_pending_action_payload_store)
                self.assertIsNotNone(
                    runtime._mcp_terminal_candidate_snapshot_authority
                )
                self.assertIsNotNone(runtime._mcp_durable_result_snapshot_authority)
                self.assertIs(
                    runtime.storage._mcp_pending_action_payload_reader,
                    runtime._mcp_pending_action_payload_store,
                )
                self.assertIs(
                    runtime.storage._mcp_terminal_candidate_snapshot_reader,
                    runtime._mcp_terminal_candidate_snapshot_authority,
                )
                self.assertIs(
                    runtime.storage._mcp_durable_result_snapshot_reader,
                    runtime._mcp_durable_result_snapshot_authority,
                )
                profiles = await runtime.available_user_mcp_server_profiles("alice")
                self.assertEqual(len(profiles), 1)
                self.assertEqual(profiles[0].display_name, "CRM")
                self.assertNotIn("secret.invalid", repr(profiles[0]))
                visible = runtime.capability_registry.list_for_request(
                    OrchestrationRequest(
                        task_id="task-a",
                        conversation_id="conv-a",
                        root_message_id="msg-a",
                        user_message="查客户",
                        available_mcp_servers=profiles,
                    ),
                    public_only=True,
                )
                self.assertIn("mcp.dispatch", {item.capability_id for item in visible})
            finally:
                await runtime.shutdown()

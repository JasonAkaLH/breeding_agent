from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.core.enums import ArtifactType, NodeStatus, TaskStatus
from src.core.models import (
    Artifact,
    EventRecord,
    Interrupt,
    MCPBranchRecord,
    MCPCallRecord,
    MCPRemoteTaskBinding,
    Task,
    TaskEdge,
    TaskNode,
)
from src.lifecycle.cancellation_service import CancellationService
from src.lifecycle.interrupt_service import InterruptService
from src.storage.rust_contract import (
    artifact_policy,
    benchmark_policy,
    config_policy,
    decommission_policy,
    error_policy,
    load_runtime_sidecar_contract,
    migration_policy,
    mode_for_component,
    operation_policy,
    ops_policy,
    promotion_policy,
    retry_policy,
    resource_limit,
)
from src.storage.runtime_sidecar_facade import (
    RuntimeLeaseFacade,
    build_sidecar_retry_plan,
    ensure_sidecar_write_allowed,
    runtime_sidecar_max_in_flight,
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_benchmark_report,
    validate_runtime_sidecar_config_authority,
    validate_runtime_sidecar_decommission_readiness,
    validate_runtime_sidecar_endpoint,
    validate_runtime_sidecar_handshake,
    validate_runtime_sidecar_migration_plan,
    validate_runtime_sidecar_migration_evidence_artifact,
    validate_runtime_sidecar_ops_readiness,
    validate_runtime_sidecar_promotion_readiness,
    validate_runtime_sidecar_response,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


def _valid_task_authority_cutover() -> dict[str, object]:
    empty_digest = hashlib.sha256(b"[]").hexdigest()
    inventory_digest = hashlib.sha256(b"canonical-inventory").hexdigest()
    return {
        "backfill_import_complete": True,
        "task_inventory": {
            "legacy_count": 1,
            "sidecar_count": 1,
            "legacy_canonical_digest": inventory_digest,
            "sidecar_canonical_digest": inventory_digest,
        },
        "task_node_inventory": {
            "legacy_count": 1,
            "sidecar_count": 1,
            "legacy_canonical_digest": inventory_digest,
            "sidecar_canonical_digest": inventory_digest,
        },
        "legacy_null_assignment_resolution": {
            "resolution_complete": True,
            "active_count": 0,
            "active_canonical_digest": empty_digest,
            "terminal_historical_count": 1,
            "terminal_historical_canonical_digest": inventory_digest,
            "terminal_historical_remains_unassigned": True,
        },
    }


class _RecordingRuntimeSidecarClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.tasks: dict[str, dict[str, object]] = {}
        self.edges: dict[str, list[dict[str, object]]] = {}
        self.artifacts: dict[str, dict[str, object]] = {}
        self.nodes: dict[str, dict[str, object]] = {}
        self.planner_replan_claims: dict[tuple[str, str], dict[str, object]] = {}

    async def submit_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        idempotency_key: str,
        node: dict[str, object] | None = None,
        task: dict[str, object],
        expected_from_status: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "task_submit",
                {
                    "conversation_id": conversation_id,
                    "idempotency_key": idempotency_key,
                    "task": task,
                    "task_id": task_id,
                    "expected_from_status": expected_from_status,
                },
            )
        )
        current = self.tasks.get(task_id)
        current_status = None if current is None else str(current["status"])
        if expected_from_status != current_status:
            return {
                "operation": "task_submit",
                "task_id": "",
                "duplicate": False,
                "task": None,
                "error": {
                    "code": "runtime_store_idempotency_conflict",
                    "message": "stale expected status",
                    "category": "internal",
                    "retriable": False,
                    "safe_metadata": {},
                },
            }
        self.tasks[task_id] = task
        return {
            "operation": "task_submit",
            "task_id": task_id,
            "duplicate": False,
            "task": task,
            "error": None,
        }

    async def get_task(self, *, task_id: str) -> dict[str, object]:
        self.calls.append(("task_get", {"task_id": task_id}))
        task = self.tasks.get(task_id)
        return {
            "operation": "task_get",
            "found": task is not None,
            "task": task,
            "error": None,
        }

    async def list_tasks_for_conversation(
        self,
        *,
        conversation_id: str,
        statuses: tuple[str, ...] = (),
    ) -> dict[str, object]:
        self.calls.append(
            (
                "task_list_for_conversation",
                {"conversation_id": conversation_id, "statuses": statuses},
            )
        )
        tasks = [
            task
            for task in self.tasks.values()
            if task["conversation_id"] == conversation_id
            and (not statuses or task["status"] in statuses)
        ]
        tasks.sort(key=lambda task: (task.get("created_at") or "", task["task_id"]), reverse=True)
        return {
            "operation": "task_list_for_conversation",
            "tasks": tasks,
            "error": None,
        }

    async def get_active_task_for_conversation(
        self,
        *,
        conversation_id: str,
    ) -> dict[str, object]:
        self.calls.append(
            ("task_get_active_for_conversation", {"conversation_id": conversation_id})
        )
        active_statuses = ("accepted", "planning", "running", "cancelling")
        response = await self.list_tasks_for_conversation(
            conversation_id=conversation_id,
            statuses=active_statuses,
        )
        self.calls.pop()
        task = response["tasks"][0] if response["tasks"] else None
        return {
            "operation": "task_get_active_for_conversation",
            "found": task is not None,
            "task": task,
            "error": None,
        }

    async def claim_planner_replan(
        self,
        *,
        task_id: str,
        decision_digest: str,
        now: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "planner_replan_claim",
                {"task_id": task_id, "decision_digest": decision_digest, "now": now},
            )
        )
        key = (task_id, decision_digest)
        claim = self.planner_replan_claims.get(key)
        if claim is None:
            revision = 1 + sum(1 for stored in self.planner_replan_claims.values() if stored["task_id"] == task_id)
            claim = {
                "task_id": task_id,
                "decision_digest": decision_digest,
                "planning_revision": revision,
                "planning_epoch": f"r{revision}",
                "status": "claimed",
                "created_at": now,
                "updated_at": now,
            }
            self.planner_replan_claims[key] = claim
        return {"operation": "planner_replan_claim", "claim": claim, "found": True, "error": None}

    async def get_planner_replan_claim(
        self,
        *,
        task_id: str,
        decision_digest: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "planner_replan_claim_get",
                {"task_id": task_id, "decision_digest": decision_digest},
            )
        )
        claim = self.planner_replan_claims.get((task_id, decision_digest))
        return {
            "operation": "planner_replan_claim_get",
            "claim": claim,
            "found": claim is not None,
            "error": None,
        }

    async def mark_planner_replan_claim(
        self,
        *,
        task_id: str,
        decision_digest: str,
        status: str,
        now: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "planner_replan_claim_mark",
                {
                    "task_id": task_id,
                    "decision_digest": decision_digest,
                    "status": status,
                    "now": now,
                },
            )
        )
        claim = self.planner_replan_claims[(task_id, decision_digest)]
        claim = {**claim, "status": status, "updated_at": now}
        self.planner_replan_claims[(task_id, decision_digest)] = claim
        return {"operation": "planner_replan_claim_mark", "claim": claim, "found": True, "error": None}

    async def transition_node(
        self,
        *,
        task_id: str,
        node_id: str,
        to_status: str,
        expected_from_status: str,
        idempotency_key: str,
        node: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "node_state_transition",
                {
                    "idempotency_key": idempotency_key,
                    "node_id": node_id,
                    "task_id": task_id,
                    "to_status": to_status,
                    "expected_from_status": expected_from_status,
                    "node": node,
                },
            )
        )
        current = self.nodes.get(node_id)
        current_status = "" if current is None else str(current["status"])
        if expected_from_status != current_status:
            return {
                "operation": "node_state_transition",
                "node_id": "",
                "status": "",
                "error": {
                    "code": "runtime_store_idempotency_conflict",
                    "message": "stale expected status",
                    "category": "internal",
                    "retriable": False,
                    "safe_metadata": {},
                },
                "node": None,
            }
        if node is not None:
            self.nodes[node_id] = node
        return {
            "operation": "node_state_transition",
            "node_id": node_id,
            "status": to_status,
            "error": None,
            "node": node,
        }

    async def get_task_node(self, *, node_id: str) -> dict[str, object]:
        self.calls.append(("task_node_get", {"node_id": node_id}))
        node = self.nodes.get(node_id)
        return {"operation": "task_node_get", "found": node is not None, "node": node, "error": None}

    async def list_task_nodes_for_task(self, *, task_id: str) -> dict[str, object]:
        self.calls.append(("task_node_list", {"task_id": task_id}))
        nodes = sorted(
            (node for node in self.nodes.values() if node["task_id"] == task_id),
            key=lambda node: str(node["node_id"]),
        )
        return {"operation": "task_node_list", "nodes": nodes, "error": None}

    async def save_task_edge(
        self,
        *,
        task_id: str,
        from_node_id: str,
        to_node_id: str,
        edge_type: str,
        condition: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "task_edge_save",
                {
                    "edge_type": edge_type,
                    "from_node_id": from_node_id,
                    "idempotency_key": idempotency_key,
                    "task_id": task_id,
                    "to_node_id": to_node_id,
                },
            )
        )
        edge = {
            "task_id": task_id,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
            "edge_type": edge_type,
            "condition": condition,
        }
        self.edges.setdefault(task_id, [])
        self.edges[task_id] = [
            existing
            for existing in self.edges[task_id]
            if not (existing["from_node_id"] == from_node_id and existing["to_node_id"] == to_node_id)
        ]
        self.edges[task_id].append(edge)
        return {"operation": "task_edge_save", "edge": edge, "error": None}

    async def list_task_edges(self, *, task_id: str) -> dict[str, object]:
        self.calls.append(("task_edge_list", {"task_id": task_id}))
        return {"operation": "task_edge_list", "edges": list(self.edges.get(task_id, [])), "error": None}

    async def save_artifact(
        self,
        *,
        artifact_id: str,
        task_id: str,
        producer_node_id: str,
        artifact_type: str,
        storage_ref: str,
        summary: str,
        is_complete: bool,
        created_at: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "artifact_save",
                {
                    "artifact_id": artifact_id,
                    "artifact_type": artifact_type,
                    "idempotency_key": idempotency_key,
                    "producer_node_id": producer_node_id,
                    "task_id": task_id,
                },
            )
        )
        artifact = {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "producer_node_id": producer_node_id,
            "artifact_type": artifact_type,
            "storage_ref": storage_ref,
            "summary": summary,
            "is_complete": is_complete,
            "created_at": created_at,
        }
        self.artifacts[artifact_id] = artifact
        return {"operation": "artifact_save", "artifact": artifact, "error": None}

    async def get_artifact(self, *, artifact_id: str) -> dict[str, object]:
        self.calls.append(("artifact_get", {"artifact_id": artifact_id}))
        artifact = self.artifacts.get(artifact_id)
        return {"operation": "artifact_get", "artifact": artifact, "found": artifact is not None, "error": None}

    async def list_artifacts_for_task(self, *, task_id: str) -> dict[str, object]:
        self.calls.append(("artifact_list", {"task_id": task_id}))
        artifacts = [artifact for artifact in self.artifacts.values() if artifact["task_id"] == task_id]
        return {"operation": "artifact_list", "artifacts": artifacts, "error": None}

    async def append_event(
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
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "event_append",
            "cursor": {
                "conversation_id": conversation_id,
                "task_id": task_id,
                "sequence": 1,
                "created_at_ms": 1,
            },
            "error": None,
        }

    async def write_cancellation_token(
        self,
        *,
        task_id: str,
        requested_at_ms: int,
        reason: str,
        terminal_policy: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "cancellation_token_write",
                {
                    "idempotency_key": idempotency_key,
                    "reason": reason,
                    "task_id": task_id,
                    "terminal_policy": terminal_policy,
                },
            )
        )
        return {
            "operation": "cancellation_token_write",
            "written": True,
            "error": None,
        }

    def acquire_lease(
        self,
        *,
        task_id: str,
        owner_id: str,
        now_ms: int,
        ttl_ms: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "lease_acquire",
                {
                    "idempotency_key": idempotency_key,
                    "owner_id": owner_id,
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "lease_acquire",
            "task_id": task_id,
            "owner_id": owner_id,
            "revision": 1,
            "expires_at_ms": now_ms + ttl_ms,
            "renew_token": "renew-token",
            "error": None,
        }

    def renew_lease(
        self,
        *,
        task_id: str,
        renew_token: str,
        now_ms: int,
        ttl_ms: int,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "lease_renew",
                {
                    "renew_token": renew_token,
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "lease_renew",
            "task_id": task_id,
            "owner_id": "worker-1",
            "revision": 2,
            "expires_at_ms": now_ms + ttl_ms,
            "renew_token": "renew-token-2",
            "error": None,
        }

    def release_lease(self, *, task_id: str, renew_token: str) -> dict[str, object]:
        self.calls.append(
            (
                "lease_release",
                {
                    "renew_token": renew_token,
                    "task_id": task_id,
                },
            )
        )
        return {
            "operation": "lease_release",
            "released": True,
            "error": None,
        }


class _MismatchedTaskNodeSidecar(_RecordingRuntimeSidecarClient):
    async def transition_node(self, **payload: object) -> dict[str, object]:
        response = await super().transition_node(**payload)  # type: ignore[arg-type]
        response["node_id"] = "different-node"
        return response

    async def get_task_node(self, *, node_id: str) -> dict[str, object]:
        response = await super().get_task_node(node_id=node_id)
        if response["node"] is not None:
            response["node"] = {**response["node"], "node_id": "different-node"}  # type: ignore[misc]
        return response

    async def list_task_nodes_for_task(self, *, task_id: str) -> dict[str, object]:
        response = await super().list_task_nodes_for_task(task_id=task_id)
        response["nodes"] = [
            {**node, "task_id": "different-task"} for node in response["nodes"]  # type: ignore[union-attr]
        ]
        return response


class _RecordingAuditSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    async def record(self, event_type: str, payload: dict[str, object], **_kwargs: object) -> None:
        self.records.append((event_type, dict(payload)))


class _FailingCancellationRuntimeSidecarClient(_RecordingRuntimeSidecarClient):
    async def write_cancellation_token(
        self,
        *,
        task_id: str,
        requested_at_ms: int,
        reason: str,
        terminal_policy: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "cancellation_token_write",
                {
                    "idempotency_key": idempotency_key,
                    "reason": reason,
                    "task_id": task_id,
                    "terminal_policy": terminal_policy,
                },
            )
        )
        raise RuntimeError("runtime_store_unavailable: simulated shadow sidecar outage")


class _FailingRuntimeSidecarClient(_RecordingRuntimeSidecarClient):
    def __init__(self, error_message: str = "event_log_unavailable: simulated shadow sidecar outage") -> None:
        super().__init__()
        self.error_message = error_message

    async def append_event(
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
                    "task_id": task_id,
                },
            )
        )
        raise RuntimeError(self.error_message)


class RuntimeSidecarRustContractTest(SQLiteStorageTestCase):
    def test_runtime_sidecar_contract_artifact_lists_components_and_modes(self) -> None:
        contract = load_runtime_sidecar_contract()
        self.assertEqual(contract["component"], "maf_runtime_sidecar")
        self.assertEqual(contract["protocol_version"], "maf.runtime.v1")
        self.assertEqual(contract["modes"], ["off", "shadow", "enforce"])
        self.assertEqual(
            contract["mode_env"],
            {
                "runtime_store": "MAF_RUST_RUNTIME_STORE_MODE",
                "event_log": "MAF_RUST_EVENT_LOG_MODE",
                "task_dispatcher": "MAF_RUST_TASK_DISPATCHER_MODE",
            },
        )

    def test_runtime_sidecar_write_failures_are_fail_closed(self) -> None:
        contract = load_runtime_sidecar_contract()
        write_ops = {
            operation["name"]: operation
            for operation in contract["operations"]
            if operation["kind"] == "write"
        }
        for name in [
            "task_submit",
            "node_state_transition",
            "task_edge_save",
            "artifact_save",
            "event_append",
            "lease_acquire",
            "lease_renew",
            "lease_release",
            "cancellation_token_write",
            "bundle_revision_pin",
            "bundle_revision_release",
            "agent_state_commit",
        ]:
            self.assertIn(name, write_ops)
            self.assertEqual(write_ops[name]["enforce_failure"], "fail_closed")
            self.assertFalse(write_ops[name]["python_legacy_write_fallback"])

    def test_task_authority_contract_exposes_read_and_non_retriable_conflict(self) -> None:
        contract = load_runtime_sidecar_contract()
        self.assertIn("task_read", contract["supported_features"])
        self.assertIn("agent_state", contract["supported_features"])
        task_get = next(operation for operation in contract["operations"] if operation["name"] == "task_get")
        self.assertEqual(task_get["kind"], "read")
        self.assertFalse(task_get["idempotency_required"])
        self.assertEqual(operation_policy("task_list_for_conversation")["kind"], "read")
        self.assertEqual(operation_policy("task_get_active_for_conversation")["kind"], "read")
        self.assertEqual(operation_policy("task_node_get")["kind"], "read")
        self.assertEqual(operation_policy("task_node_list")["kind"], "read")
        self.assertEqual(operation_policy("agent_run_get")["kind"], "read")
        self.assertEqual(operation_policy("agent_item_list")["kind"], "read")
        conflict = next(error for error in contract["error_codes"] if error["code"] == "runtime_store_idempotency_conflict")
        self.assertEqual(conflict["category"], "internal")
        self.assertFalse(conflict["retriable"])
        self.assertEqual(
            contract["schema_hash"],
            "maf_runtime_v1_schema_20260822_agent_state",
        )
        self.assertEqual(
            contract["artifact_policy"]["expected_proto_hash"],
            "maf_runtime_proto_v1_20260822_agent_state",
        )

    def test_runtime_contract_accessors_drive_event_append_payload_limit(self) -> None:
        event_append = operation_policy("event_append")
        self.assertEqual(event_append["enforce_failure"], "fail_closed")
        self.assertFalse(event_append["python_legacy_write_fallback"])
        self.assertEqual(error_policy("event_log_payload_too_large")["category"], "resource_limit")
        self.assertEqual(resource_limit("event_payload_bytes"), 256 * 1024)

        oversized = EventRecord(
            event_id="evt-too-large",
            conversation_id="conv-1",
            task_id="task-1",
            event_type="oversized",
            payload={"blob": "x" * resource_limit("event_payload_bytes")},
        )
        with self.assertRaisesRegex(
            ValueError,
            "event_log_payload_too_large: event payload exceeds Rust runtime sidecar limit",
        ):
            asyncio.run(SQLiteStorage(self.session_factory).append_event(oversized))
        self.assertEqual(asyncio.run(SQLiteStorage(self.session_factory).list_events_for_task("task-1")), [])

    def test_event_log_enforce_mode_rejects_python_legacy_append_without_sidecar(self) -> None:
        event = EventRecord(
            event_id="evt-enforce",
            conversation_id="conv-enforce",
            task_id="task-enforce",
            event_type="enforce",
            payload={"ok": True},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "enforce"}):
            self.assertEqual(mode_for_component("event_log"), "enforce")
            with self.assertRaisesRegex(
                RuntimeError,
                "event_log_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(SQLiteStorage(self.session_factory).append_event(event))

        self.assertEqual(asyncio.run(SQLiteStorage(self.session_factory).list_event_page_for_task("task-enforce")), [])

    def test_event_log_enforce_routes_append_to_configured_sidecar_without_python_sqlite_write(self) -> None:
        event = EventRecord(
            event_id="evt-sidecar",
            conversation_id="conv-sidecar",
            task_id="task-sidecar",
            event_type="sidecar",
            payload={"ok": True},
        )
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "enforce"}):
            saved = asyncio.run(storage.append_event(event))

        self.assertEqual(saved, event)
        self.assertEqual(
            sidecar.calls,
            [
                (
                    "event_append",
                    {
                        "conversation_id": "conv-sidecar",
                        "event_type": "sidecar",
                        "idempotency_key": "evt-sidecar",
                        "task_id": "task-sidecar",
                    },
                )
            ],
        )
        self.assertEqual(asyncio.run(SQLiteStorage(self.session_factory).list_event_page_for_task("task-sidecar")), [])

    def test_event_log_enforce_rejects_python_sqlite_replay_even_when_append_sidecar_is_configured(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "event_log_replay_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.list_event_page_for_task("task-sidecar"))
            with self.assertRaisesRegex(
                RuntimeError,
                "event_log_replay_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.list_events_for_task("task-sidecar"))
            with self.assertRaisesRegex(
                RuntimeError,
                "event_log_replay_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.list_events_for_task_filtered("task-sidecar"))

    def test_event_log_shadow_keeps_python_visible_write_and_records_sidecar_audit(self) -> None:
        audit_events: list[dict[str, str]] = []
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )
        event = EventRecord(
            event_id="evt-shadow",
            conversation_id="conv-shadow",
            task_id="task-shadow",
            event_type="shadow",
            payload={"secret": "do-not-log"},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "shadow"}):
            saved = asyncio.run(storage.append_event(event))

        self.assertEqual(saved, event)
        self.assertEqual(len(asyncio.run(storage.list_event_page_for_task("task-shadow"))), 1)
        self.assertEqual(sidecar.calls[0][0], "event_append")
        self.assertEqual(audit_events[0]["component"], "event_log")
        self.assertEqual(audit_events[0]["operation"], "event_append")
        self.assertEqual(audit_events[0]["legacy_status"], "ok")
        self.assertEqual(audit_events[0]["rust_status"], "ok")
        self.assertNotIn("do-not-log", str(audit_events[0]))

    def test_event_log_shadow_sidecar_error_does_not_block_python_legacy_write(self) -> None:
        audit_events: list[dict[str, str]] = []
        sidecar = _FailingRuntimeSidecarClient()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )
        event = EventRecord(
            event_id="evt-shadow-error",
            conversation_id="conv-shadow-error",
            task_id="task-shadow-error",
            event_type="shadow",
            payload={"ok": True},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "shadow"}):
            saved = asyncio.run(storage.append_event(event))

        self.assertEqual(saved, event)
        self.assertEqual(len(asyncio.run(storage.list_event_page_for_task("task-shadow-error"))), 1)
        self.assertEqual(sidecar.calls[0][0], "event_append")
        self.assertEqual(audit_events[0]["rust_status"], "error")
        self.assertEqual(audit_events[0]["error_code"], "event_log_unavailable")

    def test_event_log_shadow_error_code_does_not_parse_secret_exception_prefix(self) -> None:
        audit_events: list[dict[str, str]] = []
        sidecar = _FailingRuntimeSidecarClient("sk-secret-token: connection failed")
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )
        event = EventRecord(
            event_id="evt-shadow-secret-error",
            conversation_id="conv-shadow-secret-error",
            task_id="task-shadow-secret-error",
            event_type="shadow",
            payload={"ok": True},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "shadow"}):
            saved = asyncio.run(storage.append_event(event))

        self.assertEqual(saved, event)
        self.assertEqual(len(asyncio.run(storage.list_event_page_for_task("task-shadow-secret-error"))), 1)
        self.assertEqual(audit_events[0]["rust_status"], "error")
        self.assertEqual(audit_events[0]["error_code"], "RuntimeError")
        self.assertNotIn("sk-secret-token", str(audit_events[0]))

    def test_event_log_shadow_audit_error_does_not_block_python_legacy_write(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()

        def failing_audit_sink(_payload: dict[str, str]) -> None:
            raise RuntimeError("audit sink unavailable")

        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=failing_audit_sink,
        )
        event = EventRecord(
            event_id="evt-shadow-audit-error",
            conversation_id="conv-shadow-audit-error",
            task_id="task-shadow-audit-error",
            event_type="shadow",
            payload={"ok": True},
        )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "shadow"}):
            saved = asyncio.run(storage.append_event(event))

        self.assertEqual(saved, event)
        self.assertEqual(len(asyncio.run(storage.list_event_page_for_task("task-shadow-audit-error"))), 1)
        self.assertEqual(sidecar.calls[0][0], "event_append")

    def test_runtime_store_enforce_mode_rejects_python_legacy_task_writes_without_sidecar(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        task = Task(
            task_id="task-enforce-store",
            conversation_id="conv-enforce-store",
            root_message_id="msg-enforce-store",
            status=TaskStatus.ACCEPTED,
        )
        node = TaskNode(
            node_id="node-enforce-store",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            self.assertEqual(mode_for_component("runtime_store"), "enforce")
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.save_task(task))
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.save_task_node(node))

        self.assertIsNone(asyncio.run(storage.get_task(task.task_id)))
        self.assertIsNone(asyncio.run(storage.get_task_node(node.node_id)))

    def test_runtime_store_enforce_routes_task_and_node_to_configured_sidecar_without_python_sqlite_write(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-sidecar-store",
            conversation_id="conv-sidecar-store",
            root_message_id="msg-sidecar-store",
            status=TaskStatus.ACCEPTED,
        )
        node = TaskNode(
            node_id="node-sidecar-store",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            saved_task = asyncio.run(storage.save_task(task))
            saved_node = asyncio.run(storage.save_task_node(node))
            loaded_node = asyncio.run(storage.get_task_node(node.node_id))
            listed_nodes = asyncio.run(storage.list_task_nodes_for_task(task.task_id))

        self.assertEqual(saved_task, task)
        self.assertEqual(saved_node, node)
        self.assertEqual(loaded_node, node)
        self.assertEqual(listed_nodes, [node])
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            [
                "task_get",
                "task_submit",
                "task_node_get",
                "node_state_transition",
                "task_node_get",
                "task_node_list",
            ],
        )
        submit_payload = sidecar.calls[1][1]
        self.assertEqual(submit_payload["conversation_id"], "conv-sidecar-store")
        self.assertEqual(submit_payload["task_id"], "task-sidecar-store")
        self.assertTrue(str(submit_payload["idempotency_key"]).startswith("task-sidecar-store:"))
        self.assertEqual(submit_payload["task"]["root_message_id"], "msg-sidecar-store")
        self.assertTrue(str(sidecar.calls[3][1]["idempotency_key"]).startswith("node-sidecar-store:"))
        self.assertEqual(sidecar.calls[3][1]["node"]["capability_id"], "main_agent.respond")
        self.assertIsNone(asyncio.run(SQLiteStorage(self.session_factory).get_task(task.task_id)))
        self.assertIsNone(asyncio.run(SQLiteStorage(self.session_factory).get_task_node(node.node_id)))

    def test_runtime_store_enforce_routes_planner_replan_claims_to_sidecar(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-sidecar-replan",
            conversation_id="conv-sidecar-replan",
            root_message_id="msg-sidecar-replan",
            status=TaskStatus.RUNNING,
        )
        first_digest = "a" * 64
        second_digest = "b" * 64

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(task))
            first = asyncio.run(
                storage.claim_planner_replan(
                    task.task_id,
                    first_digest,
                    now=datetime(2026, 8, 18, 10, 0, 0),
                )
            )
            retry = asyncio.run(
                storage.claim_planner_replan(
                    task.task_id,
                    first_digest,
                    now=datetime(2026, 8, 18, 10, 1, 0),
                )
            )
            second = asyncio.run(
                storage.claim_planner_replan(
                    task.task_id,
                    second_digest,
                    now=datetime(2026, 8, 18, 10, 2, 0),
                )
            )
            applied = asyncio.run(
                storage.mark_planner_replan_claim(
                    task.task_id,
                    first_digest,
                    status="applied",
                    now=datetime(2026, 8, 18, 10, 3, 0),
                )
            )
            loaded = asyncio.run(storage.get_planner_replan_claim(task.task_id, first_digest))

        self.assertEqual(first, retry)
        self.assertEqual(first.planning_epoch, "r1")
        self.assertEqual(second.planning_epoch, "r2")
        self.assertEqual(applied.status, "applied")
        self.assertEqual(loaded, applied)
        self.assertEqual(
            [call[0] for call in sidecar.calls[-5:]],
            [
                "planner_replan_claim",
                "planner_replan_claim",
                "planner_replan_claim",
                "planner_replan_claim_mark",
                "planner_replan_claim_get",
            ],
        )
        self.assertIsNone(
            asyncio.run(
                SQLiteStorage(self.session_factory).get_planner_replan_claim(
                    task.task_id,
                    first_digest,
                )
            )
        )

    def test_remote_terminal_outbox_transitions_authoritative_sidecar_node_in_enforce(self) -> None:
        now = datetime(2026, 8, 13, 12, 0, 0)
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        task = Task("task-mcp-enforce", "conv-mcp-enforce", "msg-mcp-enforce")
        node = TaskNode(
            node_id="node-mcp-enforce",
            task_id=task.task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.WAITING_FOR_DEPENDENCY,
        )
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(task))
            asyncio.run(storage.save_task_node(node))

        asyncio.run(
            storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-mcp-enforce",
                    owner_user_id="alice",
                    task_id=task.task_id,
                    node_id=node.node_id,
                    status="ready",
                    created_at=now,
                    updated_at=now,
                )
            )
        )
        self.assertTrue(
            asyncio.run(
                storage.reserve_mcp_call(
                    MCPCallRecord(
                        call_ref="call-mcp-enforce",
                        branch_id="branch-mcp-enforce",
                        owner_user_id="alice",
                        task_id=task.task_id,
                        node_id=node.node_id,
                        server_id="server-a",
                        tool_name="lookup",
                        status="active",
                        call_sequence=1,
                        arguments_sha256="args",
                        server_security_version=1,
                        input_schema_sha256="schema",
                        protocol_version="2026-07-28",
                        may_have_dispatched=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
        )
        binding = MCPRemoteTaskBinding(
            safe_remote_task_ref="remote-mcp-enforce",
            owner_user_id="alice",
            task_id=task.task_id,
            node_id=node.node_id,
            call_ref="call-mcp-enforce",
            server_id="server-a",
            protocol_version="2026-07-28",
            remote_task_ciphertext=b"ciphertext",
            remote_task_nonce=b"nonce",
            encryption_version=1,
            last_status="working",
            next_poll_at=now,
            created_at=now,
            updated_at=now,
        )
        asyncio.run(storage.save_mcp_remote_task_binding(binding))
        claimed_binding = asyncio.run(
            storage.claim_due_mcp_remote_task_bindings(
                claim_owner="worker",
                claim_token="binding-token",
                now=now,
                lease_expires_at=now + timedelta(seconds=30),
            )
        )[0]
        asyncio.run(
            storage.finish_mcp_remote_task_binding(
                "alice",
                task.task_id,
                binding.safe_remote_task_ref,
                claim_owner="worker",
                claim_token="binding-token",
                expected_revision=claimed_binding.revision,
                remote_status="completed",
                call_status="completed",
                terminal_at=now + timedelta(seconds=1),
                result_ref="mcp-result-safe",
            )
        )
        outbox = asyncio.run(
            storage.claim_mcp_remote_task_outbox(
                claim_owner="worker",
                claim_token="outbox-token",
                now=now + timedelta(seconds=1),
                lease_expires_at=now + timedelta(seconds=31),
            )
        )[0]

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            applied = asyncio.run(
                storage.apply_mcp_remote_task_continuation(
                    outbox.outbox_id,
                    claim_owner="worker",
                    claim_token="outbox-token",
                    expected_revision=outbox.revision,
                    updated_at=now + timedelta(seconds=2),
                )
            )
            authoritative = asyncio.run(storage.get_task_node(node.node_id))

        self.assertIsNotNone(applied)
        self.assertEqual(authoritative.status, NodeStatus.COMPLETED)
        self.assertIsNone(
            asyncio.run(SQLiteStorage(self.session_factory).get_task_node(node.node_id))
        )

    def test_task_node_sidecar_responses_must_match_requested_identity(self) -> None:
        sidecar = _MismatchedTaskNodeSidecar()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        node = TaskNode(
            node_id="node-response-id",
            task_id="task-response-id",
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
                asyncio.run(storage.save_task_node(node))
            sidecar.nodes[node.node_id] = {
                "node_id": node.node_id,
                "task_id": node.task_id,
                "capability_id": node.capability_id,
                "assigned_instance_id": None,
                "status": str(node.status),
                "criticality": str(node.criticality),
                "dependency_type": str(node.dependency_type),
                "retry_policy": {},
                "timeout_policy": {},
                "resource_class": None,
                "input_refs": [],
                "output_refs": [],
                "started_at": None,
                "finished_at": None,
            }
            with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
                asyncio.run(storage.get_task_node(node.node_id))
            with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
                asyncio.run(storage.list_task_nodes_for_task(node.task_id))

    def test_enforce_compare_and_set_rejects_competing_stale_task_and_node_updates(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-sidecar-cas",
            conversation_id="conv-sidecar-cas",
            root_message_id="msg-sidecar-cas",
            status=TaskStatus.RUNNING,
        )
        node = TaskNode(
            node_id="node-sidecar-cas",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(task))
            asyncio.run(storage.save_task_node(node))
            self.assertEqual(
                asyncio.run(
                    storage.compare_and_set_task(
                        replace(task, status=TaskStatus.COMPLETED),
                        expected_from_status=TaskStatus.RUNNING,
                    )
                ).status,
                TaskStatus.COMPLETED,
            )
            self.assertEqual(
                asyncio.run(
                    storage.compare_and_set_task_node(
                        replace(node, status=NodeStatus.COMPLETED),
                        expected_from_status=NodeStatus.RUNNING,
                    )
                ).status,
                NodeStatus.COMPLETED,
            )
            self.assertIsNone(
                asyncio.run(
                    storage.compare_and_set_task(
                        replace(task, status=TaskStatus.FAILED),
                        expected_from_status=TaskStatus.RUNNING,
                    )
                )
            )
            self.assertIsNone(
                asyncio.run(
                    storage.compare_and_set_task_node(
                        replace(node, status=NodeStatus.FAILED),
                        expected_from_status=NodeStatus.RUNNING,
                    )
                )
            )

    def test_runtime_store_enforce_queries_authoritative_sidecar_without_python_task_rows(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        accepted = Task(
            task_id="task-query-accepted",
            conversation_id="conv-query-sidecar",
            root_message_id="msg-accepted",
            status=TaskStatus.ACCEPTED,
            created_at=datetime(2026, 8, 13, 10, 0, 0),
        )
        completed = Task(
            task_id="task-query-completed",
            conversation_id="conv-query-sidecar",
            root_message_id="msg-completed",
            status=TaskStatus.COMPLETED,
            created_at=datetime(2026, 8, 13, 11, 0, 0),
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(accepted))
            asyncio.run(storage.save_task(completed))
            listed = asyncio.run(storage.list_tasks_for_conversation("conv-query-sidecar"))
            unfinished = asyncio.run(
                storage.list_tasks_for_conversation(
                    "conv-query-sidecar",
                    statuses={TaskStatus.ACCEPTED, TaskStatus.RUNNING},
                )
            )
            active = asyncio.run(storage.get_active_task_for_conversation("conv-query-sidecar"))

        self.assertEqual([task.task_id for task in listed], [completed.task_id, accepted.task_id])
        self.assertEqual(unfinished, [accepted])
        self.assertEqual(active, accepted)
        python_storage = SQLiteStorage(self.session_factory)
        self.assertEqual(asyncio.run(python_storage.list_tasks_for_conversation("conv-query-sidecar")), [])
        self.assertIsNone(asyncio.run(python_storage.get_active_task_for_conversation("conv-query-sidecar")))

    def test_runtime_store_enforce_task_queries_fail_closed_without_sidecar(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            for query in (
                lambda: storage.list_tasks_for_conversation("conv-query-unavailable"),
                lambda: storage.get_active_task_for_conversation("conv-query-unavailable"),
                lambda: storage.get_task_node("node-query-unavailable"),
                lambda: storage.list_task_nodes_for_task("task-query-unavailable"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
                ):
                    asyncio.run(query())

    def test_cancel_and_interrupt_read_authoritative_sidecar_nodes_in_enforce(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-lifecycle-sidecar",
            conversation_id="conv-lifecycle-sidecar",
            root_message_id="msg-lifecycle-sidecar",
            status=TaskStatus.RUNNING,
        )
        node = TaskNode(
            node_id="node-lifecycle-sidecar",
            task_id=task.task_id,
            capability_id="skill.data_query",
            status=NodeStatus.RUNNING,
        )
        interrupt = Interrupt(
            interrupt_id="interrupt-lifecycle-sidecar",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id=node.node_id,
            source_agent="skill.data-query",
            source_message_id="mail-1",
            question="region?",
            reason_code="missing_region",
        )
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(task))
            asyncio.run(storage.save_task_node(node))
            opened = asyncio.run(InterruptService(storage).open_interrupt(interrupt))
            cancelled = asyncio.run(
                CancellationService(storage, runtime_sidecar_client=sidecar).cancel_task_context(task.task_id)
            )

        self.assertEqual(opened.status, "open")
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIn("task_node_get", [call[0] for call in sidecar.calls])
        self.assertIn("task_node_list", [call[0] for call in sidecar.calls])
        self.assertIsNone(asyncio.run(SQLiteStorage(self.session_factory).get_task_node(node.node_id)))

    def test_runtime_store_shadow_routes_to_sidecar_and_keeps_python_visible_write(self) -> None:
        audit_events: list[dict[str, str]] = []
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )
        task = Task(
            task_id="task-shadow-store",
            conversation_id="conv-shadow-store",
            root_message_id="msg-shadow-store",
            status=TaskStatus.ACCEPTED,
        )
        node = TaskNode(
            node_id="node-shadow-store",
            task_id=task.task_id,
            capability_id="main_agent.respond",
            status=NodeStatus.RUNNING,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"}):
            saved_task = asyncio.run(storage.save_task(task))
            saved_node = asyncio.run(storage.save_task_node(node))
            listed_tasks = asyncio.run(storage.list_tasks_for_conversation(task.conversation_id))
            active_task = asyncio.run(storage.get_active_task_for_conversation(task.conversation_id))
            loaded_node = asyncio.run(storage.get_task_node(node.node_id))
            listed_nodes = asyncio.run(storage.list_task_nodes_for_task(task.task_id))

        self.assertEqual(saved_task, task)
        self.assertEqual(saved_node, node)
        self.assertEqual(listed_tasks, [task])
        self.assertEqual(active_task, task)
        self.assertEqual(loaded_node, node)
        self.assertEqual(listed_nodes, [node])
        self.assertIsNotNone(asyncio.run(storage.get_task(task.task_id)))
        self.assertIsNotNone(asyncio.run(storage.get_task_node(node.node_id)))
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            [
                "task_submit",
                "node_state_transition",
                "task_list_for_conversation",
                "task_get_active_for_conversation",
                "task_node_get",
                "task_node_list",
            ],
        )
        self.assertEqual(
            [event["operation"] for event in audit_events],
            [
                "task_submit",
                "node_state_transition",
                "task_list_for_conversation",
                "task_get_active_for_conversation",
                "task_node_get",
                "task_node_list",
            ],
        )
        self.assertTrue(all(event["rust_status"] == "ok" for event in audit_events))

    def test_runtime_store_enforce_mode_rejects_python_legacy_graph_writes_without_sidecar(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        edge = TaskEdge(from_node_id="node-from", to_node_id="node-to")
        artifact = Artifact(
            artifact_id="artifact-enforce-store",
            task_id="task-enforce-store",
            producer_node_id="node-from",
            artifact_type=ArtifactType.JSON,
            storage_ref="memory://artifact/enforce",
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.save_task_edge("task-enforce-store", edge))
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(storage.save_artifact(artifact))

        self.assertEqual(asyncio.run(storage.list_task_edges("task-enforce-store")), [])
        self.assertIsNone(asyncio.run(storage.get_artifact(artifact.artifact_id)))

    def test_runtime_store_enforce_routes_graph_writes_to_configured_sidecar_without_python_sqlite_write(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        edge = TaskEdge(from_node_id="node-from", to_node_id="node-to")
        artifact = Artifact(
            artifact_id="artifact-sidecar-store",
            task_id="task-sidecar-store",
            producer_node_id="node-from",
            artifact_type=ArtifactType.JSON,
            storage_ref="memory://artifact/sidecar",
            summary="sidecar artifact",
            is_complete=True,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            saved_edge = asyncio.run(storage.save_task_edge("task-sidecar-store", edge))
            saved_artifact = asyncio.run(storage.save_artifact(artifact))
            listed_edges = asyncio.run(storage.list_task_edges("task-sidecar-store"))
            loaded_artifact = asyncio.run(storage.get_artifact(artifact.artifact_id))
            listed_artifacts = asyncio.run(storage.list_artifacts_for_task(artifact.task_id))

        self.assertEqual(saved_edge, edge)
        self.assertEqual(saved_artifact, artifact)
        self.assertEqual(listed_edges, [edge])
        self.assertEqual(loaded_artifact, artifact)
        self.assertEqual(listed_artifacts, [artifact])
        self.assertEqual(
            [call[0] for call in sidecar.calls],
            ["task_edge_save", "artifact_save", "task_edge_list", "artifact_get", "artifact_list"],
        )
        self.assertEqual(asyncio.run(SQLiteStorage(self.session_factory).list_task_edges("task-sidecar-store")), [])
        self.assertIsNone(asyncio.run(SQLiteStorage(self.session_factory).get_artifact(artifact.artifact_id)))

    def test_runtime_store_shadow_records_graph_sidecar_audit_without_leaking_artifact_ref(self) -> None:
        audit_events: list[dict[str, str]] = []
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            runtime_sidecar_shadow_sink=audit_events.append,
        )
        edge = TaskEdge(from_node_id="node-shadow-from", to_node_id="node-shadow-to")
        artifact = Artifact(
            artifact_id="artifact-shadow-store",
            task_id="task-shadow-graph",
            producer_node_id="node-shadow-from",
            artifact_type=ArtifactType.JSON,
            storage_ref="memory://artifact/do-not-log",
            summary="shadow artifact",
            is_complete=True,
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"}):
            saved_edge = asyncio.run(storage.save_task_edge(artifact.task_id, edge))
            saved_artifact = asyncio.run(storage.save_artifact(artifact))

        self.assertEqual(saved_edge, edge)
        self.assertEqual(saved_artifact, artifact)
        self.assertEqual(asyncio.run(storage.list_task_edges(artifact.task_id)), [edge])
        self.assertEqual(asyncio.run(storage.get_artifact(artifact.artifact_id)), artifact)
        self.assertEqual([call[0] for call in sidecar.calls], ["task_edge_save", "artifact_save"])
        self.assertEqual([event["operation"] for event in audit_events], ["task_edge_save", "artifact_save"])
        self.assertTrue(all(event["rust_status"] == "ok" for event in audit_events))
        self.assertNotIn("memory://artifact/do-not-log", str(audit_events))

    def test_cancellation_token_write_consumes_runtime_sidecar_contract(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        service = CancellationService(storage)
        task = Task(
            task_id="task-cancel-token",
            conversation_id="conv-cancel-token",
            root_message_id="msg-cancel-token",
            status=TaskStatus.RUNNING,
        )
        asyncio.run(storage.save_task(task))

        cancellation_source = inspect.getsource(CancellationService)
        self.assertIn("cancellation_token_write", cancellation_source)
        self.assertEqual(operation_policy("cancellation_token_write")["enforce_failure"], "fail_closed")

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                asyncio.run(service.cancel_task_context(task.task_id))

        reloaded = asyncio.run(storage.get_task(task.task_id))
        self.assertEqual(reloaded.status, TaskStatus.RUNNING)
        self.assertIsNone(reloaded.cancel_requested_at)

    def test_cancellation_token_enforce_routes_to_configured_sidecar(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        storage = SQLiteStorage(self.session_factory, runtime_sidecar_client=sidecar)
        service = CancellationService(storage, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-cancel-sidecar",
            conversation_id="conv-cancel-sidecar",
            root_message_id="msg-cancel-sidecar",
            status=TaskStatus.RUNNING,
        )
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            asyncio.run(storage.save_task(task))
            sidecar.calls.clear()
            cancelled = asyncio.run(service.cancel_task_context(task.task_id))

        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        cancellation_call = next(call for call in sidecar.calls if call[0] == "cancellation_token_write")
        self.assertEqual(cancellation_call[1]["task_id"], task.task_id)

    def test_cancellation_token_shadow_records_sidecar_audit_after_legacy_write(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        audit_sink = _RecordingAuditSink()
        storage = SQLiteStorage(self.session_factory)
        service = CancellationService(storage, audit_sink=audit_sink, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-cancel-shadow",
            conversation_id="conv-cancel-shadow",
            root_message_id="msg-cancel-shadow",
            status=TaskStatus.RUNNING,
        )
        asyncio.run(storage.save_task(task))

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"}):
            cancelled = asyncio.run(service.cancel_task_context(task.task_id))

        shadow_records = [record for record in audit_sink.records if record[0] == "runtime.sidecar_shadow_diff"]
        reloaded = asyncio.run(storage.get_task(task.task_id))
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIsNotNone(reloaded.cancel_requested_at)
        self.assertEqual(sidecar.calls[0][0], "cancellation_token_write")
        self.assertEqual(shadow_records[-1][1]["component"], "runtime_store")
        self.assertEqual(shadow_records[-1][1]["operation"], "cancellation_token_write")
        self.assertEqual(shadow_records[-1][1]["legacy_status"], "ok")
        self.assertEqual(shadow_records[-1][1]["rust_status"], "ok")

    def test_cancellation_token_shadow_sidecar_error_does_not_block_legacy_cancel(self) -> None:
        sidecar = _FailingCancellationRuntimeSidecarClient()
        audit_sink = _RecordingAuditSink()
        storage = SQLiteStorage(self.session_factory)
        service = CancellationService(storage, audit_sink=audit_sink, runtime_sidecar_client=sidecar)
        task = Task(
            task_id="task-cancel-shadow-error",
            conversation_id="conv-cancel-shadow-error",
            root_message_id="msg-cancel-shadow-error",
            status=TaskStatus.RUNNING,
        )
        asyncio.run(storage.save_task(task))

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "shadow"}):
            cancelled = asyncio.run(service.cancel_task_context(task.task_id))

        shadow_records = [record for record in audit_sink.records if record[0] == "runtime.sidecar_shadow_diff"]
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertEqual(sidecar.calls[0][0], "cancellation_token_write")
        self.assertEqual(shadow_records[-1][1]["rust_status"], "error")
        self.assertEqual(shadow_records[-1][1]["error_code"], "runtime_store_unavailable")

    def test_lease_operations_have_no_python_legacy_fallback_without_sidecar(self) -> None:
        facade = RuntimeLeaseFacade()
        for operation_name in ("lease_acquire", "lease_renew", "lease_release"):
            self.assertEqual(operation_policy(operation_name)["enforce_failure"], "fail_closed")

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                facade.acquire(task_id="task-lease", owner_id="worker-1")
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                facade.renew(task_id="task-lease", owner_id="worker-1", lease_token="lease-token")
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar enforce mode is active",
            ):
                facade.release(task_id="task-lease", owner_id="worker-1", lease_token="lease-token")

    def test_lease_operations_route_to_configured_sidecar_in_enforce(self) -> None:
        sidecar = _RecordingRuntimeSidecarClient()
        facade = RuntimeLeaseFacade(runtime_sidecar_client=sidecar)

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            lease = facade.acquire(
                task_id="task-lease-sidecar",
                owner_id="worker-1",
                now_ms=100,
                ttl_ms=50,
                idempotency_key="lease-key",
            )
            renewed = facade.renew(
                task_id="task-lease-sidecar",
                owner_id="worker-1",
                lease_token=lease["renew_token"],
                now_ms=120,
                ttl_ms=50,
            )
            released = facade.release(
                task_id="task-lease-sidecar",
                owner_id="worker-1",
                lease_token=renewed["renew_token"],
            )

        self.assertEqual([call[0] for call in sidecar.calls], ["lease_acquire", "lease_renew", "lease_release"])
        self.assertTrue(released["released"])

    def test_common_sidecar_write_guard_handles_all_runtime_components(self) -> None:
        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(RuntimeError, "runtime_store_unavailable"):
                ensure_sidecar_write_allowed(
                    component="runtime_store",
                    operation_name="task_submit",
                    unavailable_error_code="runtime_store_unavailable",
                )

        with patch.dict(os.environ, {"MAF_RUST_EVENT_LOG_MODE": "enforce"}):
            with self.assertRaisesRegex(RuntimeError, "event_log_unavailable"):
                ensure_sidecar_write_allowed(
                    component="event_log",
                    operation_name="event_append",
                    unavailable_error_code="event_log_unavailable",
                )

        with patch.dict(os.environ, {"MAF_RUST_TASK_DISPATCHER_MODE": "enforce"}):
            with self.assertRaisesRegex(RuntimeError, "dispatcher_unavailable"):
                ensure_sidecar_write_allowed(
                    component="task_dispatcher",
                    operation_name="bundle_revision_pin",
                    unavailable_error_code="dispatcher_unavailable",
                )

    def test_runtime_sidecar_handshake_validates_rust_contract_compatibility(self) -> None:
        contract = load_runtime_sidecar_contract()
        accepted = validate_runtime_sidecar_handshake(
            {
                "component": contract["component"],
                "protocol_version": contract["protocol_version"],
                "schema_hash": contract["schema_hash"],
                "error_code_table_hash": contract["error_code_table_hash"],
                "supported_features": list(contract["supported_features"]),
                "build_version": "test-build",
            }
        )
        self.assertEqual(accepted["protocol_version"], contract["protocol_version"])

        incompatible = dict(accepted)
        incompatible["schema_hash"] = "wrong-schema"
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_protocol_incompatible: Rust runtime sidecar handshake is incompatible",
        ):
            validate_runtime_sidecar_handshake(incompatible)

        missing_feature = dict(accepted)
        missing_feature["supported_features"] = ["runtime_store"]
        with self.assertRaisesRegex(RuntimeError, "runtime_store_protocol_incompatible"):
            validate_runtime_sidecar_handshake(missing_feature)

    def test_runtime_sidecar_endpoint_validation_rejects_public_endpoint_in_enforce(self) -> None:
        self.assertEqual(
            validate_runtime_sidecar_endpoint(
                "unix:///var/run/maf-runtime.sock",
                component="runtime_store",
                unavailable_error_code="runtime_store_unavailable",
            ),
            "unix:///var/run/maf-runtime.sock",
        )
        for endpoint in ("unix:relative.sock", "unix://runtime-sidecar.sock"):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar endpoint is not internally allowlisted",
            ):
                validate_runtime_sidecar_endpoint(
                    endpoint,
                    component="runtime_store",
                    unavailable_error_code="runtime_store_unavailable",
                )
        self.assertEqual(
            validate_runtime_sidecar_endpoint(
                "http://127.0.0.1:38481",
                component="runtime_store",
                unavailable_error_code="runtime_store_unavailable",
            ),
            "http://127.0.0.1:38481",
        )
        self.assertEqual(
            validate_runtime_sidecar_endpoint(
                "https://runtime.internal:9443",
                component="runtime_store",
                unavailable_error_code="runtime_store_unavailable",
                allowed_hosts={"runtime.internal"},
            ),
            "https://runtime.internal:9443",
        )

        with patch.dict(os.environ, {"MAF_RUST_RUNTIME_STORE_MODE": "enforce"}):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_unavailable: Rust runtime sidecar endpoint is not internally allowlisted",
            ):
                validate_runtime_sidecar_endpoint(
                    "https://example.com:443",
                    component="runtime_store",
                    unavailable_error_code="runtime_store_unavailable",
                )

    def test_runtime_sidecar_response_validation_rejects_unverified_structured_output(self) -> None:
        self.assertEqual(error_policy("runtime_store_response_invalid")["category"], "protocol")

        accepted = validate_runtime_sidecar_response(
            "event_append",
            {
                "operation": "event_append",
                "cursor": {
                    "conversation_id": "conv-response",
                    "task_id": "task-response",
                    "sequence": 1,
                    "created_at_ms": 123,
                },
                "error": None,
            },
        )
        self.assertEqual(accepted["cursor"]["sequence"], 1)

        edge_accepted = validate_runtime_sidecar_response(
            "task_edge_save",
            {
                "operation": "task_edge_save",
                "edge": {
                    "task_id": "task-response",
                    "from_node_id": "node-a",
                    "to_node_id": "node-b",
                    "edge_type": "data",
                    "condition": "",
                },
                "error": None,
            },
        )
        self.assertEqual(edge_accepted["edge"]["from_node_id"], "node-a")

        artifact_accepted = validate_runtime_sidecar_response(
            "artifact_save",
            {
                "operation": "artifact_save",
                "artifact": {
                    "artifact_id": "artifact-response",
                    "task_id": "task-response",
                    "producer_node_id": "node-b",
                    "artifact_type": "json",
                    "storage_ref": "opaque://artifact",
                    "summary": "",
                    "is_complete": True,
                    "created_at": "",
                },
                "error": None,
            },
        )
        self.assertEqual(artifact_accepted["artifact"]["artifact_id"], "artifact-response")

        typed_error = validate_runtime_sidecar_response(
            "event_append",
            {
                "operation": "event_append",
                "error": {
                    "code": "event_log_unavailable",
                    "message": "sidecar unavailable",
                    "retriable": True,
                    "category": "internal",
                    "safe_metadata": {},
                },
            },
        )
        self.assertEqual(typed_error["error"]["code"], "event_log_unavailable")

        for invalid_response in [
            {"operation": "lease_acquire", "cursor": {"task_id": "task-response", "sequence": 1}},
            {"operation": "event_append", "cursor": {"task_id": "task-response", "sequence": 1}},
            {"operation": "task_edge_save", "edge": {"task_id": "task-response"}},
            {"operation": "artifact_save", "artifact": {"artifact_id": "artifact-response"}},
            {
                "operation": "event_append",
                "error": {
                    "code": "unknown_error",
                    "message": "bad",
                    "retriable": False,
                    "category": "internal",
                    "safe_metadata": {},
                },
            },
        ]:
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_response_invalid: Rust runtime sidecar response failed contract validation",
            ):
                validate_runtime_sidecar_response("event_append", invalid_response)

    def test_runtime_sidecar_retry_plan_requires_idempotency_and_same_sidecar(self) -> None:
        self.assertEqual(
            retry_policy(),
            {
                "max_attempts": 3,
                "initial_backoff_ms": 100,
                "max_backoff_ms": 1000,
                "jitter_percent": 20,
                "same_sidecar_only": True,
                "requires_idempotency_key": True,
            },
        )
        retriable_error = {
            "code": "event_log_unavailable",
            "message": "sidecar unavailable",
            "retriable": True,
            "category": "internal",
            "safe_metadata": {},
        }

        retry_plan = build_sidecar_retry_plan(
            "event_append",
            error=retriable_error,
            failed_attempt=1,
            idempotency_key="idem-event-append",
        )
        self.assertEqual(retry_plan["next_attempt"], 2)
        self.assertEqual(retry_plan["backoff_ms"], 100)
        self.assertEqual(retry_plan["idempotency_key"], "idem-event-append")
        self.assertTrue(retry_plan["same_sidecar_only"])

        self.assertIsNone(
            build_sidecar_retry_plan(
                "event_append",
                error=retriable_error,
                failed_attempt=1,
                idempotency_key="",
            )
        )
        self.assertIsNone(
            build_sidecar_retry_plan(
                "event_append",
                error=retriable_error,
                failed_attempt=1,
                idempotency_key="idem-event-append",
                same_sidecar=False,
            )
        )
        self.assertIsNone(
            build_sidecar_retry_plan(
                "event_append",
                error=retriable_error,
                failed_attempt=3,
                idempotency_key="idem-event-append",
            )
        )
        self.assertIsNone(
            build_sidecar_retry_plan(
                "event_replay",
                error=retriable_error,
                failed_attempt=1,
                idempotency_key="idem-read",
            )
        )

    def test_runtime_sidecar_backpressure_and_deadline_limits_are_rust_owned(self) -> None:
        self.assertEqual(resource_limit("max_in_flight_min"), 8)
        self.assertEqual(resource_limit("max_in_flight_cap"), 64)
        self.assertEqual(resource_limit("max_in_flight_cpu_multiplier"), 4)
        self.assertEqual(runtime_sidecar_max_in_flight(cpu_count=1), 8)
        self.assertEqual(runtime_sidecar_max_in_flight(cpu_count=4), 16)
        self.assertEqual(runtime_sidecar_max_in_flight(cpu_count=32), 64)

        self.assertEqual(resource_limit("task_submit_deadline_ms"), 3000)
        self.assertEqual(resource_limit("state_transition_deadline_ms"), 2000)
        self.assertEqual(resource_limit("task_edge_deadline_ms"), 2000)
        self.assertEqual(resource_limit("artifact_metadata_deadline_ms"), 2000)
        self.assertEqual(resource_limit("event_append_deadline_ms"), 2000)
        self.assertEqual(resource_limit("lease_deadline_ms"), 1000)
        self.assertEqual(resource_limit("event_replay_deadline_ms"), 10000)

    def test_runtime_sidecar_config_source_and_identity_are_rust_guarded(self) -> None:
        self.assertEqual(error_policy("runtime_store_config_untrusted")["category"], "security")
        self.assertEqual(
            config_policy()["allowed_sources"],
            [
                "deployment_config",
                "environment_variable",
                "secret_manager",
                "readonly_config_file",
                "runtime_allowlist",
            ],
        )
        safe = validate_runtime_sidecar_config_authority(
            "service_endpoint",
            "deployment_config",
            component="runtime_store",
        )
        self.assertEqual(
            safe,
            {
                "config_name": "service_endpoint",
                "source": "deployment_config",
                "cross_host": "false",
                "mtls": "not_required",
            },
        )
        self.assertEqual(
            validate_runtime_sidecar_config_authority(
                "mtls_identity",
                "secret_manager",
                component="runtime_store",
                cross_host=True,
                mtls_enabled=True,
            )["mtls"],
            "configured",
        )

        for source in ("user_input", "skill_manifest", "llm_output", "external_tool_output", "unknown"):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_config_untrusted: Rust runtime sidecar config source is not trusted",
            ):
                validate_runtime_sidecar_config_authority("service_endpoint", source, component="runtime_store")

        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_config_untrusted: Rust runtime sidecar cross-host access requires mTLS identity",
        ):
            validate_runtime_sidecar_config_authority(
                "service_endpoint",
                "runtime_allowlist",
                component="runtime_store",
                cross_host=True,
                mtls_enabled=False,
            )

    def test_runtime_sidecar_artifact_provenance_is_contract_validated(self) -> None:
        contract = load_runtime_sidecar_contract()
        self.assertEqual(error_policy("runtime_store_artifact_untrusted")["category"], "security")
        policy = artifact_policy()
        self.assertEqual(
            policy["allowed_sources"],
            ["ci_pipeline", "deployment_pipeline", "runtime_allowlist"],
        )
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "sidecar_binary",
            "checksum_sha256": "sha256:runtime-sidecar",
            "sbom_digest": "sha256:sbom",
            "cargo_lock_digest": "sha256:cargo-lock",
            "proto_hash": policy["expected_proto_hash"],
            "schema_hash": contract["schema_hash"],
            "provenance_attestation": "slsa-provenance",
        }

        safe = validate_runtime_sidecar_artifact_provenance(
            metadata,
            allowed_checksums={"sha256:runtime-sidecar"},
            allowed_cargo_lock_digests={"sha256:cargo-lock"},
        )
        self.assertEqual(
            safe,
            {
                "source": "ci_pipeline",
                "artifact_kind": "sidecar_binary",
                "checksum_sha256": "sha256:runtime-sidecar",
                "cargo_lock_digest": "sha256:cargo-lock",
                "proto_hash": policy["expected_proto_hash"],
                "schema_hash": contract["schema_hash"],
                "provenance_attestation": "configured",
                "sbom": "configured",
            },
        )

        for invalid_metadata in [
            {**metadata, "source": "user_input"},
            {**metadata, "checksum_sha256": "sha256:tampered"},
            {**metadata, "cargo_lock_digest": "sha256:other-lock"},
            {**metadata, "proto_hash": "wrong-proto"},
            {**metadata, "schema_hash": "wrong-schema"},
            {key: value for key, value in metadata.items() if key != "provenance_attestation"},
        ]:
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_artifact_untrusted: Rust runtime sidecar artifact provenance is not trusted",
            ):
                validate_runtime_sidecar_artifact_provenance(
                    invalid_metadata,
                    allowed_checksums={"sha256:runtime-sidecar"},
                    allowed_cargo_lock_digests={"sha256:cargo-lock"},
                )

    def test_runtime_sidecar_benchmark_report_requires_prd_slo_metrics(self) -> None:
        self.assertEqual(error_policy("runtime_store_benchmark_invalid")["category"], "quality_gate")
        policy = benchmark_policy()
        self.assertEqual(
            policy["required_operations"],
            [
                "task_submit",
                "node_state_transition",
                "task_edge_save",
                "artifact_save",
                "event_append",
                "lease_acquire",
                "event_replay",
                "sse_snapshot",
            ],
        )
        operation_metrics = {
            operation: {
                "p50_ms": 1.0,
                "p95_ms": 2.0,
                "p99_ms": 3.0,
                "queue_wait_ms": 0.5,
                "cpu_percent": 10.0,
                "memory_mb": 64.0,
                "throughput_per_sec": 100.0,
            }
            for operation in policy["required_operations"]
        }
        report = {
            "python_baseline": operation_metrics,
            "rust_sidecar_baseline": operation_metrics,
        }
        safe = validate_runtime_sidecar_benchmark_report(report)
        self.assertEqual(safe["baselines"], "python_baseline,rust_sidecar_baseline")
        self.assertEqual(safe["operations"], ",".join(policy["required_operations"]))

        invalid_report = {
            "python_baseline": operation_metrics,
            "rust_sidecar_baseline": {
                **operation_metrics,
                "event_append": {
                    key: value for key, value in operation_metrics["event_append"].items() if key != "p99_ms"
                },
            },
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_benchmark_invalid: Rust runtime sidecar benchmark report is incomplete",
        ):
            validate_runtime_sidecar_benchmark_report(invalid_report)

    def test_runtime_sidecar_promotion_readiness_requires_global_thresholds(self) -> None:
        self.assertEqual(error_policy("runtime_store_promotion_blocked")["category"], "quality_gate")
        policy = promotion_policy()
        self.assertEqual(policy["min_shadow_days"], 7)
        self.assertEqual(policy["min_shadow_samples"], 1000)
        self.assertEqual(policy["max_contract_mismatch_rate_ppm"], 0)
        self.assertEqual(policy["max_p95_latency_ratio_percent"], 110)
        evidence = {name: True for name in policy["required_evidence"]}
        report = {
            "scope": "single_instance",
            "shadow_days": 7,
            "shadow_samples": 1000,
            "contract_mismatch_rate_ppm": 0,
            "panic_count": 0,
            "crash_count": 0,
            "rust_p95_ms": 110.0,
            "python_legacy_p95_ms": 100.0,
            "rust_error_rate_ppm": 10,
            "python_legacy_error_rate_ppm": 10,
            "evidence": evidence,
        }
        safe = validate_runtime_sidecar_promotion_readiness(report)
        self.assertEqual(safe["promotion"], "ready")
        self.assertEqual(safe["scope"], "single_instance")
        self.assertEqual(safe["shadow_samples"], "1000")

        for invalid_report in [
            {**report, "shadow_samples": 999},
            {**report, "contract_mismatch_rate_ppm": 1},
            {**report, "rust_p95_ms": 111.0},
            {**report, "rust_error_rate_ppm": 11},
            {**report, "evidence": {**evidence, "rollback_drill": False}},
        ]:
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime_store_promotion_blocked: Rust runtime sidecar promotion threshold is not satisfied",
            ):
                validate_runtime_sidecar_promotion_readiness(invalid_report)

    def test_runtime_sidecar_migration_plan_requires_dr_evidence(self) -> None:
        self.assertEqual(error_policy("runtime_store_migration_blocked")["category"], "state")
        policy = migration_policy()
        self.assertEqual(
            policy["required_components"],
            [
                "sqlite_schema",
                "event_log",
                "lease",
                "cursor",
                "task_edge",
                "artifact_metadata",
                "bundle_pin",
            ],
        )
        component_evidence = {
            component: {evidence: True for evidence in policy["required_evidence"]}
            for component in policy["required_components"]
        }
        plan = {
            "target_schema_version": load_runtime_sidecar_contract()["schema_hash"],
            "components": component_evidence,
            "task_authority_cutover": _valid_task_authority_cutover(),
        }
        safe = validate_runtime_sidecar_migration_plan(plan)
        self.assertEqual(safe["migration"], "ready")
        self.assertEqual(
            safe["target_schema_version"],
            load_runtime_sidecar_contract()["schema_hash"],
        )
        self.assertEqual(safe["components"], ",".join(policy["required_components"]))

        invalid_plan = {
            **plan,
            "components": {
                **component_evidence,
                "event_log": {**component_evidence["event_log"], "restore": False},
            },
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_migration_blocked: Rust runtime sidecar migration plan is incomplete",
        ):
            validate_runtime_sidecar_migration_plan(invalid_plan)

        for hostile_cutover in (
            {**_valid_task_authority_cutover(), "backfill_import_complete": False},
            {
                **_valid_task_authority_cutover(),
                "task_inventory": {
                    **_valid_task_authority_cutover()["task_inventory"],
                    "sidecar_count": 2,
                },
            },
            {
                **_valid_task_authority_cutover(),
                "task_node_inventory": {
                    **_valid_task_authority_cutover()["task_node_inventory"],
                    "sidecar_canonical_digest": "f" * 64,
                },
            },
            {
                **_valid_task_authority_cutover(),
                "legacy_null_assignment_resolution": {
                    **_valid_task_authority_cutover()["legacy_null_assignment_resolution"],
                    "active_count": 1,
                },
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime_store_migration_blocked"):
                validate_runtime_sidecar_migration_plan(
                    {**plan, "task_authority_cutover": hostile_cutover}
                )

    def test_task_authority_migration_artifact_is_contract_bound_and_authenticated(self) -> None:
        contract = load_runtime_sidecar_contract()
        key = b"deployment-owned-migration-key-32bytes"
        artifact = {
            "schema": migration_policy()["task_authority_evidence_schema"],
            "component": contract["component"],
            "protocol_version": contract["protocol_version"],
            "schema_hash": contract["schema_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "key_id": "deployment-key-v1",
            "migration_plan": {
                "target_schema_version": contract["schema_hash"],
                "components": {
                    component: {
                        evidence: True
                        for evidence in migration_policy()["required_evidence"]
                    }
                    for component in migration_policy()["required_components"]
                },
                "task_authority_cutover": _valid_task_authority_cutover(),
            },
        }
        signature = hmac.new(
            key,
            json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        signed = {**artifact, "hmac_sha256": signature}
        self.assertEqual(
            validate_runtime_sidecar_migration_evidence_artifact(
                signed, authentication_key=key
            )["migration"],
            "ready",
        )
        with self.assertRaisesRegex(RuntimeError, "runtime_store_migration_blocked"):
            validate_runtime_sidecar_migration_evidence_artifact(
                {
                    **signed,
                    "migration_plan": {
                        **signed["migration_plan"],
                        "target_schema_version": "tampered",
                    },
                },
                authentication_key=key,
            )

    def test_runtime_sidecar_ops_readiness_requires_runbooks_and_drills(self) -> None:
        self.assertEqual(error_policy("runtime_store_ops_readiness_blocked")["category"], "quality_gate")
        policy = ops_policy()
        self.assertEqual(
            policy["required_runbooks"],
            ["drain", "restart", "rollback", "restore", "replay"],
        )
        report = {
            "observability": {item: True for item in policy["required_observability"]},
            "runbooks": {item: True for item in policy["required_runbooks"]},
            "drills": {item: True for item in policy["required_drills"]},
        }
        safe = validate_runtime_sidecar_ops_readiness(report)
        self.assertEqual(safe["ops"], "ready")
        self.assertEqual(safe["runbooks"], ",".join(policy["required_runbooks"]))
        self.assertEqual(safe["drills"], ",".join(policy["required_drills"]))

        invalid_report = {
            **report,
            "drills": {**report["drills"], "queue_full": False},
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_ops_readiness_blocked: Rust runtime sidecar ops readiness evidence is incomplete",
        ):
            validate_runtime_sidecar_ops_readiness(invalid_report)

    def test_runtime_sidecar_decommission_requires_legacy_write_path_removal(self) -> None:
        self.assertEqual(error_policy("runtime_store_decommission_blocked")["category"], "quality_gate")
        policy = decommission_policy()
        self.assertEqual(
            policy["required_removed_legacy_paths"],
            [
                "python_storage_task_write",
                "python_storage_node_write",
                "python_storage_task_edge_write",
                "python_storage_artifact_write",
                "python_event_append_write",
                "python_bundle_pin_write",
                "python_cancellation_token_write",
                "python_lease_state",
            ],
        )
        report = {
            "canonical_sidecar_stable": True,
            "rollback_path": "deployment_or_restore",
            "legacy_write_paths_removed": {
                item: True for item in policy["required_removed_legacy_paths"]
            },
            "facade_only_paths": {item: True for item in policy["required_facade_only_paths"]},
            "evidence": {item: True for item in policy["required_evidence"]},
        }
        safe = validate_runtime_sidecar_decommission_readiness(report)
        self.assertEqual(safe["decommission"], "ready")
        self.assertEqual(safe["rollback_path"], "deployment_or_restore")

        invalid_report = {
            **report,
            "legacy_write_paths_removed": {
                **report["legacy_write_paths_removed"],
                "python_event_append_write": False,
            },
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "runtime_store_decommission_blocked: Rust runtime sidecar legacy decommission evidence is incomplete",
        ):
            validate_runtime_sidecar_decommission_readiness(invalid_report)

    def test_runtime_contract_accessors_drive_event_replay_page_limit(self) -> None:
        event_replay = operation_policy("event_replay")
        self.assertEqual(event_replay["kind"], "read")
        self.assertFalse(event_replay["python_legacy_write_fallback"])
        self.assertEqual(error_policy("event_log_replay_page_exceeded")["category"], "resource_limit")

        storage = SQLiteStorage(self.session_factory)
        for index in range(resource_limit("replay_page_events") + 1):
            asyncio.run(
                storage.append_event(
                    EventRecord(
                        event_id=f"evt-replay-{index:04d}",
                        conversation_id="conv-replay",
                        task_id="task-replay",
                        event_type="replay",
                        payload={"index": index},
                    )
                )
            )

        with self.assertRaisesRegex(
            ValueError,
            "event_log_replay_page_exceeded: event replay exceeds Rust runtime sidecar page limit",
        ):
            asyncio.run(storage.list_events_for_task("task-replay"))

    def test_runtime_contract_accessors_drive_paginated_event_replay(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        page_limit = resource_limit("replay_page_events")
        for index in range(page_limit + 1):
            asyncio.run(
                storage.append_event(
                    EventRecord(
                        event_id=f"evt-page-{index:04d}",
                        conversation_id="conv-page",
                        task_id="task-page",
                        event_type="page",
                        payload={"index": index},
                    )
                )
            )

        first_page = asyncio.run(storage.list_event_page_for_task("task-page"))
        second_page = asyncio.run(storage.list_event_page_for_task("task-page", after_event_id=first_page[-1].event_id))

        self.assertEqual(len(first_page), page_limit)
        self.assertEqual([event.event_id for event in second_page], [f"evt-page-{page_limit:04d}"])
        with self.assertRaisesRegex(
            ValueError,
            "event_log_replay_page_exceeded: requested event replay page exceeds Rust runtime sidecar limit",
        ):
            asyncio.run(storage.list_event_page_for_task("task-page", limit=page_limit + 1))

    def test_runtime_proto_is_owned_by_native_runtime_v1(self) -> None:
        proto = Path("native/proto/maf/runtime/v1/runtime.proto")
        self.assertTrue(proto.exists())
        text = proto.read_text(encoding="utf-8")
        self.assertIn("package maf.runtime.v1;", text)
        self.assertIn("service RuntimeSidecar", text)
        self.assertIn("rpc AppendEvent", text)

    def test_typed_error_prefixes_are_stable(self) -> None:
        contract = load_runtime_sidecar_contract()
        codes = {entry["code"] for entry in contract["error_codes"]}
        self.assertIn("runtime_store_unavailable", codes)
        self.assertIn("event_log_payload_too_large", codes)
        self.assertIn("dispatcher_queue_full", codes)
        self.assertTrue(
            all(
                code.startswith(("runtime_store_", "event_log_", "dispatcher_"))
                for code in codes
            )
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.core.models import (
    MCPCP7CandidateGuard,
    MCPCP7ReadyEpochEvent,
    MCPCP7SafetyLedgerRecord,
    MCPDispatchAggregateMigration,
    MCPDispatchResumeOutbox,
    MCPDurableResultLifecycle,
    MCPExecutionTerminalProjection,
    MCPNoServerIntent,
    MCPPendingToolAction,
    MCPTerminalCandidateLifecycle,
    MCPTerminalResultReceipt,
    UserMCPOwnerMutationGuard,
)
from src.integrations.mcp.safety_detectors import (
    AUTHORITATIVE_MCP_SAFETY_HOOKS,
    MCP_SAFETY_VIOLATION_REASONS,
)
from src.storage.sqlite.base import SQLiteBase
from src.storage.sqlite import bootstrap_sqlite_database, create_sqlite_engine


CP7_TABLES = {
    "user_mcp_owner_mutation_guard",
    "mcp_no_server_intent",
    "mcp_dispatch_resume_outbox",
    "mcp_pending_tool_action",
    "mcp_terminal_candidate_lifecycle",
    "mcp_durable_result_lifecycle",
    "mcp_dispatch_aggregate_migration",
    "mcp_terminal_result_receipt",
    "mcp_execution_terminal_projection",
    "mcp_cp7_safety_ledger",
    "mcp_cp7_ready_epoch_event",
    "mcp_cp7_candidate_guard",
}


class CP7SQLiteSchemaContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.engine = create_sqlite_engine(Path(self._tmpdir.name) / "cp7-schema.db")
        self.addCleanup(self.engine.dispose)
        bootstrap_sqlite_database(self.engine)

    def test_bootstrap_creates_closed_cp7_authority_tables(self) -> None:
        self.assertTrue(CP7_TABLES.issubset(inspect(self.engine).get_table_names()))

    def test_table_columns_match_core_closed_models(self) -> None:
        contracts = {
            "user_mcp_owner_mutation_guard": UserMCPOwnerMutationGuard,
            "mcp_no_server_intent": MCPNoServerIntent,
            "mcp_dispatch_resume_outbox": MCPDispatchResumeOutbox,
            "mcp_pending_tool_action": MCPPendingToolAction,
            "mcp_terminal_candidate_lifecycle": MCPTerminalCandidateLifecycle,
            "mcp_durable_result_lifecycle": MCPDurableResultLifecycle,
            "mcp_dispatch_aggregate_migration": MCPDispatchAggregateMigration,
            "mcp_terminal_result_receipt": MCPTerminalResultReceipt,
            "mcp_execution_terminal_projection": MCPExecutionTerminalProjection,
            "mcp_cp7_safety_ledger": MCPCP7SafetyLedgerRecord,
            "mcp_cp7_ready_epoch_event": MCPCP7ReadyEpochEvent,
            "mcp_cp7_candidate_guard": MCPCP7CandidateGuard,
        }
        for table_name, contract in contracts.items():
            self.assertEqual(
                set(SQLiteBase.metadata.tables[table_name].columns.keys()),
                {field.name for field in fields(contract)},
            )
        self.assertEqual(
            tuple(SQLiteBase.metadata.tables["mcp_dispatch_resume_outbox"].columns.keys()),
            tuple(field.name for field in fields(MCPDispatchResumeOutbox)),
        )

    def test_dispatch_outbox_status_claim_cursor_and_completion_contracts_are_closed(
        self,
    ) -> None:
        now = datetime(2026, 8, 18)
        base = {
            "outbox_id": "outbox-1",
            "intent_id": "intent-1",
            "owner_user_id": "alice",
            "task_id": "task-1",
            "node_id": "node-1",
            "server_id": "server-1",
            "resume_envelope_sha256": "sha256:envelope",
            "payload_sha256": "sha256:payload",
            "status": "pending",
            "claim_owner": None,
            "claim_token": None,
            "lease_expires_at": None,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "result_receipt_id": None,
            "completion_mode": None,
            "resume_reason": "initial",
            "resume_receipt_id": None,
            "resume_answer_id": None,
            "selector_step_total": 0,
            "approval_round_total": 0,
        }
        table = SQLiteBase.metadata.tables["mcp_dispatch_resume_outbox"]
        allowed_statuses = {
            "pending",
            "claimed",
            "active",
            "waiting_approval",
            "waiting_input",
            "remote_pending",
            "completed",
            "aborted",
        }
        for index, status in enumerate(sorted(allowed_statuses)):
            row = {
                **base,
                "outbox_id": f"outbox-{index}",
                "intent_id": f"intent-{index}",
                "status": status,
            }
            if status in {"claimed", "active"}:
                row.update(
                    claim_owner="worker",
                    claim_token="token",
                    lease_expires_at=now + timedelta(seconds=30),
                )
            if status in {"completed", "aborted"}:
                row.update(
                    completed_at=now + timedelta(minutes=1),
                    completion_mode=(
                        "completed" if status == "completed" else "failed_no_call"
                    ),
                )
            with self.engine.begin() as connection:
                connection.execute(table.insert().values(**row))

        invalid_rows = (
            {**base, "outbox_id": "invalid-status", "status": "running"},
            {**base, "outbox_id": "missing-active-claim", "status": "active"},
            {
                **base,
                "outbox_id": "claim-on-waiting",
                "status": "waiting_approval",
                "claim_owner": "worker",
                "claim_token": "token",
                "lease_expires_at": now + timedelta(seconds=30),
            },
            {
                **base,
                "outbox_id": "bad-terminal-mode",
                "status": "completed",
                "completed_at": now + timedelta(minutes=1),
                "completion_mode": "normal_terminal_projection",
            },
            {
                **base,
                "outbox_id": "bad-receipt-cursor",
                "resume_reason": "ordinary_terminal",
                "resume_receipt_id": None,
            },
            {
                **base,
                "outbox_id": "bad-answer-cursor",
                "resume_reason": "approval_accepted",
                "resume_answer_id": None,
            },
        )
        for index, row in enumerate(invalid_rows):
            row = {**row, "intent_id": f"invalid-intent-{index}"}
            with self.subTest(outbox_id=row["outbox_id"]):
                with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                    connection.execute(table.insert().values(**row))

    def test_task_route_reason_accepts_only_the_additive_closed_value(self) -> None:
        insert_sql = text(
            "INSERT INTO task (task_id, conversation_id, root_message_id, status, "
            "routing_mode, mcp_execution_mode, mcp_shadow_enabled, "
            "mcp_rollout_config_version, mcp_route_reason_code, mcp_rollout_mode) "
            "VALUES (:task_id, 'conv', 'message', 'accepted', 'auto', "
            "'unavailable', 0, 'v1', :reason, 'enforce')"
        )
        with self.engine.begin() as connection:
            connection.execute(
                insert_sql,
                {"task_id": "task-no-server", "reason": "no_user_scoped_server"},
            )
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(
                insert_sql,
                {"task_id": "task-unknown", "reason": "NO_USER_SCOPED_SERVER"},
            )

    def test_safety_and_epoch_rows_are_append_only(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO mcp_cp7_safety_ledger ("
                    "record_id,candidate_id,epoch_id,config_fingerprint,record_kind,"
                    "red_line,hook_id,bucket_started_at,bucket_ended_at,reason_code,"
                    "value,boundary_source_sha256,payload_sha256,recorded_at) VALUES ("
                    "'record-1','candidate-1','epoch-1','sha256:config','registration',"
                    "'cross_user_access','gateway.task_owner_boundary',NULL,NULL,"
                    "'registered',0,NULL,'sha256:payload','2026-08-13T00:00:00Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO mcp_cp7_ready_epoch_event ("
                    "event_id,candidate_id,epoch_id,predecessor_epoch_id,event_kind,"
                    "container_id,image_id,config_fingerprint,boundary_at,audit_device,"
                    "audit_inode,audit_offset,ledger_record_count,inflight_state_sha256,"
                    "payload_sha256) VALUES ('event-1','candidate-1','epoch-1',NULL,"
                    "'opened','container-1','sha256:image','sha256:config',"
                    "'2026-08-13T00:00:00Z','device',1,0,1,'sha256:inflight','sha256:event')"
                )
            )
        for table_name, key_name, key_value in (
            ("mcp_cp7_safety_ledger", "record_id", "record-1"),
            ("mcp_cp7_ready_epoch_event", "event_id", "event-1"),
        ):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE {table_name} SET candidate_id='changed' WHERE {key_name}=:key"),
                    {"key": key_value},
                )
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(
                    text(f"DELETE FROM {table_name} WHERE {key_name}=:key"),
                    {"key": key_value},
                )

    def test_safety_ledger_enforces_authoritative_mapping_and_minute_window(self) -> None:
        insert_sql = text(
            "INSERT INTO mcp_cp7_safety_ledger ("
            "record_id,candidate_id,epoch_id,config_fingerprint,record_kind,"
            "red_line,hook_id,bucket_started_at,bucket_ended_at,reason_code,"
            "value,boundary_source_sha256,payload_sha256,recorded_at) VALUES ("
            ":record_id,'candidate-1','epoch-1','sha256:config',:record_kind,"
            ":red_line,:hook_id,:bucket_started_at,:bucket_ended_at,:reason_code,"
            ":value,:boundary_source_sha256,'sha256:payload',"
            "'2026-08-13T00:02:00Z')"
        )
        valid_attestation = {
            "record_id": "valid-attestation",
            "record_kind": "attestation",
            "red_line": "cross_user_access",
            "hook_id": "gateway.task_owner_boundary",
            "bucket_started_at": "2026-08-13T00:00:00Z",
            "bucket_ended_at": "2026-08-13T00:01:00Z",
            "reason_code": "observed_zero",
            "value": 0,
            "boundary_source_sha256": None,
        }
        with self.engine.begin() as connection:
            connection.execute(insert_sql, valid_attestation)
            for red_line, hook_id in AUTHORITATIVE_MCP_SAFETY_HOOKS.items():
                connection.execute(
                    insert_sql,
                    {
                        **valid_attestation,
                        "record_id": f"registration-{red_line.value}",
                        "record_kind": "registration",
                        "red_line": red_line.value,
                        "hook_id": hook_id,
                        "bucket_started_at": None,
                        "bucket_ended_at": None,
                        "reason_code": "registered",
                    },
                )
                connection.execute(
                    insert_sql,
                    {
                        **valid_attestation,
                        "record_id": f"violation-{red_line.value}",
                        "record_kind": "violation",
                        "red_line": red_line.value,
                        "hook_id": hook_id,
                        "reason_code": next(
                            iter(MCP_SAFETY_VIOLATION_REASONS[red_line])
                        ),
                        "value": 1,
                        "boundary_source_sha256": "sha256:boundary",
                    },
                )

        invalid_rows = (
            {**valid_attestation, "record_id": "one-second", "bucket_ended_at": "2026-08-13T00:00:01Z"},
            {
                **valid_attestation,
                "record_id": "non-minute",
                "bucket_started_at": "2026-08-13T00:00:01Z",
                "bucket_ended_at": "2026-08-13T00:01:01Z",
            },
            {
                **valid_attestation,
                "record_id": "non-utc",
                "bucket_started_at": "2026-08-13T08:00:00+08:00",
                "bucket_ended_at": "2026-08-13T08:01:00+08:00",
            },
            {
                **valid_attestation,
                "record_id": "garbage-z",
                "bucket_started_at": "garbageZ",
                "bucket_ended_at": "still-garbageZ",
            },
            {
                **valid_attestation,
                "record_id": "bare-z",
                "bucket_started_at": "Z",
                "bucket_ended_at": "Z",
            },
            {
                **valid_attestation,
                "record_id": "invalid-date",
                "bucket_started_at": "2026-99-99T00:00:00Z",
                "bucket_ended_at": "2026-99-99T00:01:00Z",
            },
            {**valid_attestation, "record_id": "wrong-hook", "hook_id": "gateway.resource_cleanup_boundary"},
            {**valid_attestation, "record_id": "unknown-red-line", "red_line": "unknown_red_line"},
            {
                **valid_attestation,
                "record_id": "wrong-violation-reason",
                "record_kind": "violation",
                "reason_code": "cleanup_failed",
                "value": 1,
                "boundary_source_sha256": "sha256:boundary",
            },
        )
        for row in invalid_rows:
            with self.subTest(record_id=row["record_id"]):
                with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                    connection.execute(insert_sql, row)

    def test_candidate_guard_is_monotonic_and_not_deletable(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO mcp_cp7_candidate_guard ("
                    "candidate_id,invalid_latched,first_invalid_record_id,"
                    "first_invalid_reason,first_invalid_at,created_at,updated_at) VALUES ("
                    "'candidate-1',0,NULL,NULL,NULL,'2026-08-13T00:00:00Z',"
                    "'2026-08-13T00:00:00Z')"
                )
            )
            connection.execute(
                text(
                    "UPDATE mcp_cp7_candidate_guard SET invalid_latched=1, "
                    "first_invalid_record_id='record-1', first_invalid_reason='gap', "
                    "first_invalid_at='2026-08-13T00:01:00Z', "
                    "updated_at='2026-08-13T00:01:00Z' WHERE candidate_id='candidate-1'"
                )
            )
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mcp_cp7_candidate_guard SET invalid_latched=0 "
                    "WHERE candidate_id='candidate-1'"
                )
            )
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM mcp_cp7_candidate_guard WHERE candidate_id='candidate-1'")
            )


if __name__ == "__main__":
    unittest.main()

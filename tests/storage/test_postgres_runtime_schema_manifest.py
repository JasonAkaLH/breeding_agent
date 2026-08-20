from __future__ import annotations

import unittest

from src.storage.sqlite.base import SQLiteBase
from src.state.postgres.runtime_schema import (
    POSTGRES_RUNTIME_TABLES,
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_index_schema_ddl,
    build_runtime_schema_ddl,
    build_runtime_table_schema_ddl,
)


class PostgresRuntimeSchemaManifestTest(unittest.TestCase):
    def test_manifest_covers_all_sqlite_runtime_tables_and_state_tables(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        sqlite_tables = set(SQLiteBase.metadata.tables)
        self.assertEqual(sqlite_tables, set(manifest.runtime_table_names))
        for table_name in POSTGRES_RUNTIME_TABLES:
            self.assertIn(table_name, manifest.runtime_table_names)
        for table_name in (
            "state_write_command",
            "state_partition_cursor",
            "state_write_dead_letter",
            "state_write_archive",
            "state_migration_ledger",
        ):
            self.assertIn(table_name, manifest.operational_table_names)
        self.assertTrue(manifest.checksum)

    def test_runtime_ddl_uses_jsonb_timestamptz_and_no_foreign_keys(self) -> None:
        ddl = build_runtime_schema_ddl()
        lowered = ddl.lower()
        self.assertIn("jsonb", lowered)
        self.assertIn("timestamp with time zone", lowered)
        self.assertNotIn("foreign key", lowered)
        self.assertNotIn("drop table", lowered)
        self.assertNotIn("truncate", lowered)

    def test_runtime_bootstrap_can_create_tables_before_indexes_for_existing_schema_reconciliation(self) -> None:
        table_ddl = build_runtime_table_schema_ddl().lower()
        index_ddl = build_runtime_index_schema_ddl().lower()
        self.assertIn("create table", table_ddl)
        self.assertNotIn("create index", table_ddl)
        self.assertIn("idx_conversation_delete_status_updated", index_ddl)
        self.assertIn("delete_phase", index_ddl)

    def test_task_input_attachment_is_bootstrapped_by_runtime_manifest_and_ddl(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        self.assertIn("task_input_attachment", manifest.runtime_table_names)
        self.assertEqual(
            manifest.table_columns["task_input_attachment"],
            {
                "attachment_id": "text",
                "task_id": "text",
                "conversation_id": "text",
                "source_kind": "text",
                "source_upload_id": "text",
                "source_message_id": "text",
                "interrupt_answer_id": "text",
                "filename": "text",
                "content_type": "text",
                "file_type": "text",
                "size_bytes": "integer",
                "sha256": "text",
                "prompt_artifact": "jsonb",
                "skill_artifact": "jsonb",
                "source_payload": "jsonb",
                "selected_sheet": "text",
                "created_at": "timestamp with time zone",
                "updated_at": "timestamp with time zone",
            },
        )
        self.assertIn("CREATE TABLE IF NOT EXISTS task_input_attachment", build_runtime_table_schema_ddl())
        index_ddl = build_runtime_index_schema_ddl()
        self.assertIn("idx_task_input_attachment_task_created", index_ddl)
        self.assertIn("idx_task_input_attachment_conversation_task", index_ddl)
        self.assertIn("idx_task_input_attachment_upload", index_ddl)

    def test_planner_replan_claim_is_bootstrapped_with_closed_constraints(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        self.assertIn("planner_replan_claim", manifest.runtime_table_names)
        self.assertEqual(
            manifest.table_columns["planner_replan_claim"],
            {
                "task_id": "text",
                "decision_digest": "text",
                "planning_revision": "bigint",
                "planning_epoch": "text",
                "status": "text",
                "created_at": "timestamp with time zone",
                "updated_at": "timestamp with time zone",
            },
        )
        constraints = manifest.check_constraints["planner_replan_claim"]
        self.assertIn("planning_revision >= 1", constraints["ck_planner_replan_claim_planner_replan_claim_positive_revision"])
        self.assertIn("'claimed'", constraints["ck_planner_replan_claim_planner_replan_claim_status"])
        ddl = build_runtime_table_schema_ddl()
        self.assertIn("CREATE TABLE IF NOT EXISTS planner_replan_claim", ddl)
        self.assertIn("uq_planner_replan_claim_task_revision", ddl)

    def test_manifest_checksum_changes_when_table_spec_changes(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        mutated = manifest.with_runtime_table_names((*manifest.runtime_table_names, "extra_table"))
        self.assertNotEqual(manifest.checksum, mutated.checksum)

    def test_cp7_tables_constraints_and_append_only_triggers_are_manifested(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        for table_name in (
            "user_mcp_owner_mutation_guard",
            "mcp_no_server_intent",
            "mcp_dispatch_resume_outbox",
            "mcp_terminal_result_receipt",
            "mcp_execution_terminal_projection",
            "mcp_cp7_safety_ledger",
            "mcp_cp7_ready_epoch_event",
            "mcp_cp7_candidate_guard",
        ):
            self.assertIn(table_name, manifest.runtime_table_names)
        route_check = manifest.check_constraints["task"][
            "ck_task_task_mcp_route_reason_code"
        ]
        self.assertIn("no_user_scoped_server", route_check)
        self.assertEqual(
            set(manifest.append_only_tables),
            {
                "mcp_cp7_safety_ledger",
                "mcp_cp7_ready_epoch_event",
                "mcp_legacy_retirement_evidence",
                "mcp_legacy_retirement_receipt",
                "mcp_no_server_convergence_receipt",
                "mcp_terminal_result_receipt",
            },
        )
        ddl = build_runtime_schema_ddl()
        self.assertIn("maf_reject_append_only_mutation", ddl)
        self.assertIn("trg_mcp_cp7_safety_ledger_append_only", ddl)
        self.assertIn("trg_mcp_cp7_candidate_guard_monotonic", ddl)
        self.assertIn("trg_mcp_cp7_safety_attestation_window", ddl)
        self.assertIn("date_trunc('minute', NEW.bucket_started_at)", ddl)
        safety_ddl = manifest.check_constraints["mcp_cp7_safety_ledger"]
        self.assertIn(
            "gateway.task_owner_boundary",
            safety_ddl["ck_mcp_cp7_safety_ledger_mcp_cp7_safety_authoritative_hook"],
        )
        self.assertIn(
            "task_owner_mismatch",
            safety_ddl["ck_mcp_cp7_safety_ledger_mcp_cp7_safety_violation_reason"],
        )
        call_columns = manifest.table_columns["mcp_call_record"]
        self.assertEqual(call_columns["output_schema"], "jsonb")
        self.assertEqual(call_columns["output_schema_sha256"], "text")
        self.assertEqual(call_columns["terminal_result_source"], "text")
        receipt_columns = manifest.table_columns["mcp_terminal_result_receipt"]
        self.assertEqual(receipt_columns["result_parser_revision"], "text")
        self.assertEqual(receipt_columns["validated_checkpoint_sha256"], "text")
        self.assertEqual(receipt_columns["parsed_model_sha256"], "text")

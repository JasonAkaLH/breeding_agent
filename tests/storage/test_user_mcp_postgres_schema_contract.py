from __future__ import annotations

import inspect
import unittest

from src.state.postgres.runtime_schema import (
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_index_schema_ddl,
    build_runtime_table_schema_ddl,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlite.repositories import SQLiteStateRepository


class UserMCPPostgresSchemaContractTest(unittest.TestCase):
    def test_manifest_contains_coordination_tables_without_runtime_payload_columns(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        expected = {
            "user_mcp_server",
            "user_mcp_tool_grant",
            "user_mcp_health_attempt",
            "user_mcp_scope_lease",
            "maf_master_key_validation",
            "mcp_branch_record",
            "mcp_call_record",
            "mcp_remote_task_binding",
            "mcp_remote_task_outbox",
            "mcp_sealed_state",
            "mcp_connection_lease",
            "mcp_audit_event",
            "mcp_legacy_migration_record",
            "mcp_rollout_metric_bucket",
            "mcp_shadow_audit_sample",
            "mcp_rollout_evidence_snapshot",
            "mcp_rollout_gate_scope",
            "mcp_rollout_stage_approval",
            "mcp_rollout_deployment_activation",
            "mcp_rollout_promotion_block",
            "mcp_rollout_block_resolution",
            "mcp_rollout_instance_config",
            "user_mcp_owner_mutation_guard",
            "mcp_no_server_intent",
            "mcp_dispatch_resume_outbox",
            "mcp_terminal_result_receipt",
            "mcp_execution_terminal_projection",
            "mcp_cp7_safety_ledger",
            "mcp_cp7_ready_epoch_event",
            "mcp_cp7_candidate_guard",
        }
        self.assertTrue(expected.issubset(manifest.runtime_table_names))
        self.assertNotIn("mcp_credential_key_validation", manifest.runtime_table_names)
        self.assertEqual(
            manifest.table_columns["maf_master_key_validation"],
            {
                "singleton_key": "integer",
                "validation_nonce": "bytea",
                "validation_ciphertext": "bytea",
                "derivation_version": "integer",
                "created_at": "timestamp with time zone",
            },
        )
        validation_checks = manifest.check_constraints["maf_master_key_validation"]
        self.assertEqual(
            set(validation_checks.values()),
            {
                "singleton_key = 1",
                "length(validation_nonce) = 12",
                "derivation_version = 1",
            },
        )
        self.assertEqual(
            manifest.table_columns["mcp_rollout_metric_bucket"]["red_line"],
            "text",
        )
        self.assertEqual(
            manifest.table_columns["mcp_rollout_evidence_snapshot"][
                "attestation_key_id"
            ],
            "text",
        )
        self.assertEqual(
            manifest.table_columns["mcp_rollout_evidence_snapshot"][
                "attestation_signature"
            ],
            "text",
        )
        self.assertEqual(
            manifest.table_columns["mcp_remote_task_outbox"][
                "continuation_status"
            ],
            "text",
        )
        self.assertEqual(
            manifest.table_columns["mcp_remote_task_outbox"][
                "continuation_node_ids"
            ],
            "jsonb",
        )
        forbidden = {"tool_list", "input_schema", "output_schema", "session_id", "remote_task_id", "result"}
        for table_name in expected:
            self.assertFalse(forbidden.intersection(manifest.table_columns[table_name]))
        ddl = build_runtime_index_schema_ddl()
        self.assertIn("idx_user_mcp_server_owner_server", ddl)
        self.assertIn("idx_user_mcp_health_attempt_lease", ddl)
        self.assertIn("idx_user_mcp_scope_lease_expiry", ddl)
        self.assertIn("idx_mcp_call_owner_task", ddl)
        self.assertIn("idx_mcp_remote_task_poll", ddl)
        self.assertIn("idx_mcp_audit_expiry", ddl)
        self.assertIn("idx_mcp_legacy_migration_plan", ddl)
        self.assertIn("idx_mcp_rollout_metric_window", ddl)
        self.assertIn("idx_mcp_shadow_sample_scope_window", ddl)
        self.assertIn("idx_mcp_rollout_evidence_scope", ddl)
        self.assertIn("idx_mcp_rollout_activation_scope", ddl)
        self.assertIn("idx_mcp_rollout_block_scope", ddl)
        self.assertIn("idx_mcp_rollout_instance_lease", ddl)
        self.assertIn("idx_mcp_no_server_intent_owner_status", ddl)
        self.assertIn("idx_mcp_dispatch_resume_claim", ddl)
        self.assertIn("idx_mcp_cp7_safety_candidate_epoch", ddl)

        table_ddl = build_runtime_table_schema_ddl()
        validation_table_ddl = _table_ddl(table_ddl, "maf_master_key_validation")
        self.assertIn("PRIMARY KEY (singleton_key)", validation_table_ddl)
        self.assertIn("CHECK (singleton_key = 1)", validation_table_ddl)
        self.assertIn("CHECK (length(validation_nonce) = 12)", validation_table_ddl)
        self.assertIn("CHECK (derivation_version = 1)", validation_table_ddl)
        self.assertNotIn("validation_id", validation_table_ddl)
        self.assertNotIn("encryption_version", validation_table_ddl)
        self.assertIn("uq_mcp_rollout_evidence_nonce", table_ddl)
        self.assertIn("uq_mcp_shadow_sample_scope_nonce", table_ddl)
        self.assertIn("uq_mcp_rollout_evidence_snapshot", table_ddl)
        self.assertIn("uq_mcp_rollout_activation_approval", table_ddl)
        self.assertIn("uq_mcp_rollout_activation_target", table_ddl)
        self.assertIn("uq_mcp_rollout_resolution_block", table_ddl)
        self.assertIn("user_mcp_phase3", table_ddl)
        self.assertIn("no_user_scoped_server", _table_ddl(table_ddl, "task"))
        self.assertIn(
            "late_result_no_continuation",
            _table_ddl(table_ddl, "mcp_terminal_result_receipt"),
        )
        self.assertIn(
            "maintenance_boundary_invalid",
            _table_ddl(table_ddl, "mcp_cp7_safety_ledger"),
        )
        metric_table_ddl = _table_ddl(table_ddl, "mcp_rollout_metric_bucket")
        self.assertIn("red_line TEXT DEFAULT 'not_applicable' NOT NULL", metric_table_ddl)
        self.assertIn("mcp_safety_red_line_total", metric_table_ddl)
        self.assertIn("cross_user_access", metric_table_ddl)
        self.assertIn(
            "error_category, call_kind, red_line, latency_bucket",
            metric_table_ddl,
        )
        block_table_ddl = _table_ddl(
            table_ddl, "mcp_rollout_promotion_block"
        )
        for reason in (
            "attestation_missing",
            "attestation_invalid",
            "metric_series_missing",
            "metric_summary_mismatch",
        ):
            self.assertIn(reason, block_table_ddl)
        for table_name in expected:
            if table_name.startswith("mcp_rollout_"):
                self.assertNotIn("FOREIGN KEY", _table_ddl(table_ddl, table_name))

    def test_postgres_hotspots_use_explicit_row_locks(self) -> None:
        source = inspect.getsource(PostgreSQLStorage)
        self.assertIn("with_for_update", source)
        for method in (
            "update_user_mcp_server", "claim_user_mcp_health_attempt", "renew_user_mcp_health_attempt",
            "complete_user_mcp_health_attempt", "acquire_user_mcp_scope_lease", "renew_user_mcp_scope_lease",
            "release_user_mcp_health_attempt", "mark_user_mcp_server_deleted", "finalize_user_mcp_server_delete",
            "reserve_mcp_call",
            "append_mcp_rollout_evidence_snapshot",
            "activate_mcp_rollout_deployment",
            "append_mcp_rollout_promotion_block",
            "append_mcp_rollout_block_resolution",
            "save_mcp_rollout_instance_config_lease",
        ):
            self.assertIn(f"def {method}", source)
        for function_name in (
            "append_ci_evidence_snapshot",
            "append_deployment_activation",
            "append_promotion_block",
            "append_block_resolution",
            "upsert_instance_config_lease",
        ):
            self.assertIn(f"mcp_rollout_api.{function_name}", source)
        self.assertIn("mcp_rollout_session_factory", source)
        self.assertNotIn("MCPRolloutGateScopeRow", source)

    def test_master_key_validation_repository_uses_postgres_create_or_get(self) -> None:
        source = inspect.getsource(
            SQLiteStateRepository.create_or_get_maf_master_key_validation
        )
        self.assertIn("postgresql_insert", source)
        self.assertIn("on_conflict_do_nothing", source)
        self.assertIn("select(MAFMasterKeyValidationRow)", source.replace("\n", ""))


def _table_ddl(ddl: str, table_name: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {table_name}"
    start = ddl.index(marker)
    next_table = ddl.find("CREATE TABLE IF NOT EXISTS", start + len(marker))
    return ddl[start:] if next_table < 0 else ddl[start:next_table]

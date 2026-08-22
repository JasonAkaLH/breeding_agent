from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import DateTimeText, JSONText, SQLiteBase


class UserMCPServerRow(SQLiteBase):
    __tablename__ = "user_mcp_server"
    __table_args__ = (
        Index("idx_user_mcp_server_owner_server", "owner_user_id", "server_id"),
        Index("idx_user_mcp_server_owner_updated", "owner_user_id", "updated_at"),
        Index("idx_user_mcp_server_health_deletion", "health_status", "deletion_pending", "updated_at"),
    )

    server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    routing_description: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_preference: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(Text, nullable=False)
    auth_metadata: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    health_status: Mapped[str] = mapped_column(Text, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    security_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    last_tested_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    last_test_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    credential_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encryption_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    deletion_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    deleted_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class UserMCPToolGrantRow(SQLiteBase):
    __tablename__ = "user_mcp_tool_grant"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "server_id", "tool_name", "server_security_version", "input_schema_sha256",
            name="uq_user_mcp_tool_grant_scope",
        ),
        Index("idx_user_mcp_tool_grant_owner_server", "owner_user_id", "server_id"),
    )

    grant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    server_security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_schema_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    invalidated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    invalid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MCPBranchRecordRow(SQLiteBase):
    __tablename__ = "mcp_branch_record"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "task_id", "node_id", name="uq_mcp_branch_task_node"),
        Index("idx_mcp_branch_owner_task", "owner_user_id", "task_id"),
        Index("idx_mcp_branch_status_updated", "status", "updated_at"),
    )

    branch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    initial_server_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    active_call_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    terminal_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPCallRecordRow(SQLiteBase):
    __tablename__ = "mcp_call_record"
    __table_args__ = (
        UniqueConstraint("branch_id", "call_sequence", name="uq_mcp_call_branch_sequence"),
        UniqueConstraint("pending_action_id", name="uq_mcp_call_pending_action"),
        UniqueConstraint(
            "continuation_of_call_ref", name="uq_mcp_call_continuation_source"
        ),
        Index("idx_mcp_call_owner_task", "owner_user_id", "task_id"),
        Index("idx_mcp_call_branch_status", "branch_id", "status"),
        CheckConstraint(
            "status IN ('reserved', 'active', 'completed', 'failed', 'cancelled', "
            "'input_required', 'remote_pending', 'unknown')",
            name="mcp_call_status",
        ),
        CheckConstraint("call_sequence > 0", name="mcp_call_sequence_positive"),
    )

    call_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    call_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    server_security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_config_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_schema_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    output_schema_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_result_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_field_names: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    may_have_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_action_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    continuation_of_call_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    terminal_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPRemoteTaskBindingRow(SQLiteBase):
    __tablename__ = "mcp_remote_task_binding"
    __table_args__ = (
        Index("idx_mcp_remote_task_owner_task", "owner_user_id", "task_id"),
        Index("idx_mcp_remote_task_poll", "last_status", "next_poll_at"),
        Index("idx_mcp_remote_task_claim", "terminal_at", "next_poll_at", "lease_expires_at"),
    )

    safe_remote_task_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_ref: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    remote_task_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    remote_task_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_status: Mapped[str] = mapped_column(Text, nullable=False)
    next_poll_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    published_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    continuation_plan: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    terminal_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True, server_default=text("0"))


class MCPRemoteTaskOutboxRow(SQLiteBase):
    __tablename__ = "mcp_remote_task_outbox"
    __table_args__ = (
        Index("idx_mcp_remote_task_outbox_claim", "status", "lease_expires_at", "created_at"),
        Index("idx_mcp_remote_task_outbox_task", "owner_user_id", "task_id"),
    )

    outbox_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_ref: Mapped[str] = mapped_column(Text, nullable=False)
    safe_remote_task_ref: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONText(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    continuation_admitted_at: Mapped[object | None] = mapped_column(
        DateTimeText(), nullable=True
    )
    continuation_dispatched_at: Mapped[object | None] = mapped_column(
        DateTimeText(), nullable=True
    )
    continuation_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    continuation_claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    continuation_claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    continuation_lease_expires_at: Mapped[object | None] = mapped_column(
        DateTimeText(), nullable=True
    )
    continuation_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    continuation_node_ids: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    continuation_safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPSealedStateRow(SQLiteBase):
    __tablename__ = "mcp_sealed_state"
    __table_args__ = (Index("idx_mcp_sealed_state_owner_task", "owner_user_id", "task_id"),)

    sealed_state_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_ref: Mapped[str] = mapped_column(Text, nullable=False)
    state_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPConnectionLeaseRow(SQLiteBase):
    __tablename__ = "mcp_connection_lease"
    __table_args__ = (
        Index("idx_mcp_connection_owner_task", "owner_user_id", "task_id"),
        Index("idx_mcp_connection_expiry", "lease_expires_at"),
    )

    connection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    disconnected_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    auth_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPAuditEventRow(SQLiteBase):
    __tablename__ = "mcp_audit_event"
    __table_args__ = (
        Index("idx_mcp_audit_owner_occurred", "owner_user_id", "occurred_at"),
        Index("idx_mcp_audit_expiry", "expires_at"),
    )

    audit_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)


class MCPLegacyMigrationRecordRow(SQLiteBase):
    __tablename__ = "mcp_legacy_migration_record"
    __table_args__ = (
        UniqueConstraint(
            "plan_fingerprint",
            "source_server_id",
            name="uq_mcp_legacy_migration_plan_source",
        ),
        UniqueConstraint(
            "target_server_id",
            name="uq_mcp_legacy_migration_target",
        ),
        CheckConstraint(
            "event_type = 'mcp.legacy.config_migrated'",
            name="mcp_legacy_migration_event_type",
        ),
        CheckConstraint(
            "disposition = 'migrate_owner'",
            name="mcp_legacy_migration_disposition",
        ),
        Index(
            "idx_mcp_legacy_migration_plan",
            "plan_fingerprint",
            "source_server_id",
        ),
    )

    migration_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    source_server_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    owner_consumer_ref: Mapped[str] = mapped_column(Text, nullable=False)
    target_server_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_consumer_set_digest: Mapped[str] = mapped_column(Text, nullable=False)
    capability_obligations_fingerprint: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    catalog_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    capability_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    validator_provenance_fingerprint: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    credential_digest: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    evidence_expires_at: Mapped[object] = mapped_column(
        DateTimeText(), nullable=False
    )


class MCPRolloutGateScopeRow(SQLiteBase):
    __tablename__ = "mcp_rollout_gate_scope"
    __table_args__ = (
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_gate_program",
        ),
    )

    environment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rollout_program: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutDrillObservationRow(SQLiteBase):
    __tablename__ = "mcp_rollout_drill_observation"
    __table_args__ = (
        UniqueConstraint(
            "environment_id",
            "rollout_program",
            "deployment_id",
            "stage",
            "config_fingerprint",
            "drill",
            "observed_at",
            name="uq_mcp_rollout_drill_scope_observed",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_drill_program",
        ),
        CheckConstraint(
            "stage = 'internal_enforce'",
            name="mcp_rollout_drill_stage",
        ),
        CheckConstraint(
            "drill IN ('cancellation', 'long_call_120_seconds', "
            "'disconnect_five_minutes', 'restart_unknown', 'mrtr_recovery', "
            "'tasks_recovery', 'fair_queueing', 'flag_rollback')",
            name="mcp_rollout_drill_name",
        ),
        CheckConstraint(
            "outcome IN ('passed', 'failed')",
            name="mcp_rollout_drill_outcome",
        ),
        Index(
            "idx_mcp_rollout_drill_scope_window",
            "environment_id",
            "deployment_id",
            "observed_at",
        ),
    )

    drill_observation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    drill: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    recorded_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)


class MCPRolloutMetricBucketRow(SQLiteBase):
    __tablename__ = "mcp_rollout_metric_bucket"
    __table_args__ = (
        UniqueConstraint(
            "environment_id",
            "deployment_id",
            "stage",
            "config_fingerprint",
            "metric_name",
            "bucket_started_at",
            "bucket_ended_at",
            "execution_path",
            "routing_mode",
            "transport",
            "protocol_version",
            "adapter",
            "result_category",
            "error_category",
            "call_kind",
            "red_line",
            "latency_bucket",
            name="uq_mcp_rollout_metric_series_bucket",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_metric_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_metric_stage",
        ),
        CheckConstraint(
            "metric_name IN ('mcp_route_requests_total', "
            "'mcp_route_shadow_mismatch_total', 'mcp_gateway_active_scopes', "
            "'mcp_gateway_connect_duration_seconds', 'mcp_tools_list_duration_seconds', "
            "'mcp_tools_list_attempts_total', 'mcp_tool_calls_active', "
            "'mcp_tool_calls_total', 'mcp_tool_call_duration_seconds', "
            "'mcp_tool_call_unknown_total', "
            "'mcp_permission_decisions_total', 'mcp_disconnect_lease_expired_total', "
            "'mcp_temp_spill_bytes', 'mcp_resource_cleanup_failures_total', "
            "'mcp_protocol_negotiation_total', 'mcp_server_discover_duration_seconds', "
            "'mcp_mrtr_rounds_total', 'mcp_remote_tasks_active', "
            "'mcp_safety_red_line_total', 'mcp_result_parser_outcomes_total', "
            "'mcp_result_parser_duration_seconds')",
            name="mcp_rollout_metric_name",
        ),
        CheckConstraint(
            "execution_path IN ('legacy', 'user_scoped', 'unavailable', 'not_applicable')",
            name="mcp_rollout_metric_path",
        ),
        CheckConstraint(
            "routing_mode IN ('off', 'shadow', 'enforce', 'not_applicable')",
            name="mcp_rollout_metric_mode",
        ),
        CheckConstraint(
            "transport IN ('streamable_http', 'legacy_http_sse', 'not_applicable')",
            name="mcp_rollout_metric_transport",
        ),
        CheckConstraint(
            "protocol_version IN ('2024-11-05', '2025-03-26', '2025-06-18', "
            "'2025-11-25', '2026-07-28', 'not_applicable')",
            name="mcp_rollout_metric_protocol",
        ),
        CheckConstraint(
            "adapter IN ('python_legacy', 'python_2026', 'rust_sidecar', "
            "'legacy_global_runtime', 'not_applicable')",
            name="mcp_rollout_metric_adapter",
        ),
        CheckConstraint(
            "result_category IN ('succeeded', 'failed', 'unknown', 'cancelled', "
            "'input_required', 'task_created', 'permission_denied', 'not_comparable', "
            "'not_applicable')",
            name="mcp_rollout_metric_result",
        ),
        CheckConstraint(
            "error_category IN ('none', 'authentication', 'authorization', "
            "'endpoint_policy', 'transport', 'protocol', 'server', 'timeout', "
            "'unknown', 'validation', 'cleanup', 'not_applicable')",
            name="mcp_rollout_metric_error",
        ),
        CheckConstraint(
            "call_kind IN ('ordinary', 'remote_task', 'not_applicable')",
            name="mcp_rollout_metric_call_kind",
        ),
        CheckConstraint(
            "red_line IN ('cross_user_access', 'secret_exposure', 'dual_tool_call', "
            "'unauthorized_tool_call', 'endpoint_policy_bypass', "
            "'unknown_result_replay', 'shadow_tool_call', "
            "'persistent_resource_leak', 'not_applicable')",
            name="mcp_rollout_metric_red_line",
        ),
        CheckConstraint(
            "latency_bucket IN ('le_100_ms', 'le_500_ms', 'le_1_s', 'le_5_s', "
            "'le_30_s', 'le_120_s', 'gt_120_s', 'not_applicable')",
            name="mcp_rollout_metric_latency",
        ),
        CheckConstraint("value >= 0", name="mcp_rollout_metric_value"),
        Index(
            "idx_mcp_rollout_metric_window",
            "environment_id",
            "deployment_id",
            "stage",
            "bucket_started_at",
        ),
    )

    metric_bucket_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    bucket_started_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    bucket_ended_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    execution_path: Mapped[str] = mapped_column(Text, nullable=False)
    routing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    result_category: Mapped[str] = mapped_column(Text, nullable=False)
    error_category: Mapped[str] = mapped_column(Text, nullable=False)
    call_kind: Mapped[str] = mapped_column(Text, nullable=False)
    red_line: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'not_applicable'")
    )
    latency_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutEvidenceSnapshotRow(SQLiteBase):
    __tablename__ = "mcp_rollout_evidence_snapshot"
    __table_args__ = (
        UniqueConstraint("nonce", name="uq_mcp_rollout_evidence_nonce"),
        UniqueConstraint(
            "deployment_id",
            "stage",
            "snapshot_id",
            name="uq_mcp_rollout_evidence_snapshot",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_evidence_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_evidence_stage",
        ),
        CheckConstraint("source IN ('ci', 'production')", name="mcp_rollout_evidence_source"),
        CheckConstraint(
            "producer IN ('ci_pipeline', 'production_snapshot_producer')",
            name="mcp_rollout_evidence_producer",
        ),
        CheckConstraint(
            "evidence_kind IN ('ci_conformance', 'internal_shadow', "
            "'internal_enforce', 'cohort_enforce', 'full_enforce', "
            "'legacy_assembly_off', 'rollback_drill', 'resource_baseline', 'release_tag')",
            name="mcp_rollout_evidence_kind",
        ),
        CheckConstraint("snapshot_id > 0", name="mcp_rollout_evidence_snapshot_id"),
        CheckConstraint(
            "(source = 'ci' AND attestation_key_id IS NULL AND attestation_signature IS NULL) "
            "OR (source = 'production' AND attestation_key_id IS NOT NULL "
            "AND attestation_signature IS NOT NULL)",
            name="mcp_rollout_evidence_attestation",
        ),
        Index(
            "idx_mcp_rollout_evidence_scope",
            "environment_id",
            "deployment_id",
            "stage",
            "recorded_at",
        ),
    )

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    git_sha: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    window_started_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    window_ended_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    recorded_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    producer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONText(), nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attestation_signature: Mapped[str | None] = mapped_column(Text, nullable=True)


class MCPShadowAuditSampleRow(SQLiteBase):
    __tablename__ = "mcp_shadow_audit_sample"
    __table_args__ = (
        UniqueConstraint(
            "environment_id",
            "deployment_id",
            "stage",
            "config_fingerprint",
            "nonce",
            name="uq_mcp_shadow_sample_scope_nonce",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_shadow_sample_program",
        ),
        CheckConstraint(
            "stage = 'internal_shadow'",
            name="mcp_shadow_sample_stage",
        ),
        CheckConstraint(
            "comparison IN ('matched', 'mismatched', 'not_comparable', 'excluded')",
            name="mcp_shadow_sample_comparison",
        ),
        Index(
            "idx_mcp_shadow_sample_scope_window",
            "environment_id",
            "deployment_id",
            "stage",
            "observed_at",
        ),
        Index("idx_mcp_shadow_sample_expiry", "expires_at"),
    )

    sample_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    fixture_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    safe_owner_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_task_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_call_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    shadow_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_policy: Mapped[str] = mapped_column(Text, nullable=False)
    comparison: Mapped[str] = mapped_column(Text, nullable=False)
    blockers: Mapped[list] = mapped_column(JSONText(), nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    recorded_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutStageApprovalRow(SQLiteBase):
    __tablename__ = "mcp_rollout_stage_approval"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_mcp_rollout_approval_evidence"),
        UniqueConstraint(
            "environment_id",
            "deployment_id",
            "stage",
            "config_fingerprint",
            name="uq_mcp_rollout_approval_target",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_approval_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_approval_stage",
        ),
    )

    approval_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approver: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutDeploymentActivationRow(SQLiteBase):
    __tablename__ = "mcp_rollout_deployment_activation"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_mcp_rollout_activation_approval"),
        UniqueConstraint(
            "environment_id",
            "deployment_id",
            "stage",
            "config_fingerprint",
            name="uq_mcp_rollout_activation_target",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_activation_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_activation_stage",
        ),
        Index(
            "idx_mcp_rollout_activation_scope",
            "environment_id",
            "rollout_program",
            "created_at",
        ),
    )

    activation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[str] = mapped_column(Text, nullable=False)
    previous_activation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_rollback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutPromotionBlockRow(SQLiteBase):
    __tablename__ = "mcp_rollout_promotion_block"
    __table_args__ = (
        UniqueConstraint(
            "evidence_id",
            "reason_code",
            name="uq_mcp_rollout_block_evidence_reason",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_block_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_block_stage",
        ),
        CheckConstraint(
            "reason_code IN ('no_evidence', 'invalid_transition', 'evidence_id_replay', "
            "'nonce_replay', 'snapshot_replay', 'snapshot_non_monotonic', "
            "'provenance_invalid', 'digest_invalid', 'attestation_missing', "
            "'attestation_invalid', 'evidence_scope_mismatch', "
            "'evidence_stage_mismatch', 'evidence_kind_mismatch', "
            "'source_policy_violation', 'payload_invalid', 'window_too_short', "
            "'window_incomplete', 'metric_series_missing', 'metric_summary_mismatch', "
            "'zero_denominator', 'sample_insufficient', "
            "'scenario_sample_insufficient', 'unresolved_mismatch', 'invalid_sample', "
            "'unapproved_not_comparable', 'required_drill_missing', "
            "'red_line_data_missing', 'safety_red_line', 'safety_red_line_nonzero', "
            "'baseline_missing', "
            "'p95_latency_regressed', 'error_rate_regressed', 'ci_conformance_missing')",
            name="mcp_rollout_block_reason",
        ),
        Index(
            "idx_mcp_rollout_block_scope",
            "environment_id",
            "rollout_program",
            "created_at",
        ),
    )

    block_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutBlockResolutionRow(SQLiteBase):
    __tablename__ = "mcp_rollout_block_resolution"
    __table_args__ = (
        UniqueConstraint("block_id", name="uq_mcp_rollout_resolution_block"),
        UniqueConstraint("approval_id", name="uq_mcp_rollout_resolution_approval"),
    )

    resolution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    approval_id: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approver: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPRolloutInstanceConfigRow(SQLiteBase):
    __tablename__ = "mcp_rollout_instance_config"
    __table_args__ = (
        UniqueConstraint(
            "environment_id",
            "deployment_id",
            "instance_id",
            name="uq_mcp_rollout_instance_deployment",
        ),
        CheckConstraint(
            "rollout_program = 'user_mcp_phase3'",
            name="mcp_rollout_instance_program",
        ),
        CheckConstraint(
            "stage IN ('off', 'internal_shadow', 'internal_enforce', "
            "'cohort_enforce', 'full_enforce', 'legacy_assembly_off')",
            name="mcp_rollout_instance_stage",
        ),
        Index(
            "idx_mcp_rollout_instance_lease",
            "environment_id",
            "deployment_id",
            "lease_expires_at",
        ),
    )

    instance_config_id: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_id: Mapped[str] = mapped_column(Text, nullable=False)
    rollout_program: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    activation_id: Mapped[str] = mapped_column(Text, nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class UserMCPHealthAttemptRow(SQLiteBase):
    __tablename__ = "user_mcp_health_attempt"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "server_id", name="uq_user_mcp_health_attempt_server"),
        Index("idx_user_mcp_health_attempt_lease", "lease_expires_at"),
        Index("idx_user_mcp_health_attempt_owner_server", "owner_user_id", "server_id"),
    )

    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runner_instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class UserMCPScopeLeaseRow(SQLiteBase):
    __tablename__ = "user_mcp_scope_lease"
    __table_args__ = (
        Index("idx_user_mcp_scope_lease_expiry", "lease_expires_at"),
        Index("idx_user_mcp_scope_lease_owner_server", "owner_user_id", "server_id", "lease_expires_at"),
    )

    scope_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gateway_instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MAFMasterKeyValidationRow(SQLiteBase):
    __tablename__ = "maf_master_key_validation"
    __table_args__ = (
        CheckConstraint("singleton_key = 1", name="singleton_key_one"),
        CheckConstraint("length(validation_nonce) = 12", name="validation_nonce_length"),
        CheckConstraint("derivation_version = 1", name="derivation_version_one"),
    )

    singleton_key: Mapped[int] = mapped_column(Integer, primary_key=True)
    validation_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    validation_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    derivation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class ConversationRow(SQLiteBase):
    __tablename__ = "conversation"
    __table_args__ = (
        Index("idx_conversation_username_status_updated", "username", "status", "updated_at"),
        Index("idx_conversation_username_updated", "username", "updated_at"),
        Index("idx_conversation_current_task", "current_task_id"),
        Index("idx_conversation_delete_status_updated", "status", "delete_phase", "updated_at"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_runner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_requested_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_started_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_finished_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_failed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delete_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    delete_phase: Mapped[str | None] = mapped_column(Text, nullable=True)




class ConversationFileResourceRow(SQLiteBase):
    __tablename__ = "conversation_file_resource"
    __table_args__ = (
        Index("idx_conversation_file_conversation_status_created", "conversation_id", "status", "created_at"),
        Index("idx_conversation_file_username_conversation", "username", "conversation_id"),
        Index("idx_conversation_file_storage_key", "storage_key"),
    )

    file_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    preview: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    description_status: Mapped[str] = mapped_column(Text, nullable=False)
    description_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_sheet_selection: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    selected_sheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class ConversationFileIndexRepairMarkerRow(SQLiteBase):
    __tablename__ = "conversation_file_index_repair_marker"
    __table_args__ = (
        Index("idx_conversation_file_index_repair_status_retry", "status", "next_retry_at", "updated_at"),
    )

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repair_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    affected_upload_ids: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class ConversationMemorySummaryRow(SQLiteBase):
    __tablename__ = "conversation_memory_summary"
    __table_args__ = (
        Index("idx_conversation_memory_summary_scope_updated", "conversation_id", "username", "updated_at"),
        Index("idx_conversation_memory_summary_conversation_created", "conversation_id", "created_at"),
    )

    summary_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    covered_until_turn_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    covered_until_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    covered_until_created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids_hash: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_version: Mapped[str] = mapped_column(Text, nullable=False)
    compression_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_metadata_safe: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class PendingSkillContextRow(SQLiteBase):
    __tablename__ = "conversation_pending_skill_context"
    __table_args__ = (
        Index("idx_pending_skill_context_conversation_status", "conversation_id", "status", "updated_at"),
        Index("idx_pending_skill_context_source_task", "source_task_id"),
    )

    context_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_task_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    original_user_message: Mapped[str] = mapped_column(Text, nullable=False)
    missing_requirements: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    assistant_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AuthUserTokenRow(SQLiteBase):
    __tablename__ = "auth_user_token"
    __table_args__ = (
        UniqueConstraint("api_token_hash", name="uq_auth_user_token_hash"),
        Index("idx_auth_user_token_updated", "updated_at"),
    )

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    api_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_issued_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    token_last_used_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    auth_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    auth_generation_updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MessageRow(SQLiteBase):
    __tablename__ = "message"
    __table_args__ = (
        Index("idx_message_conversation_created", "conversation_id", "created_at"),
        Index("idx_message_task_created", "task_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    message_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'chat'"))
    message_metadata: Mapped[dict | None] = mapped_column("metadata", JSONText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class UserMCPOwnerMutationGuardRow(SQLiteBase):
    __tablename__ = "user_mcp_owner_mutation_guard"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="user_mcp_owner_guard_revision"),
    )

    owner_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    server_set_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPNoServerIntentRow(SQLiteBase):
    __tablename__ = "mcp_no_server_intent"
    __table_args__ = (
        Index("idx_mcp_no_server_intent_owner_status", "owner_user_id", "status", "updated_at"),
        Index(
            "uq_mcp_no_server_initial_task",
            "task_id",
            unique=True,
            sqlite_where=text("trigger = 'initial_no_profile'"),
            postgresql_where=text("trigger = 'initial_no_profile'"),
        ),
        Index(
            "uq_mcp_no_server_target_task_node",
            "task_id",
            "node_id",
            unique=True,
            sqlite_where=text("trigger = 'target_server_revalidation'"),
            postgresql_where=text("trigger = 'target_server_revalidation'"),
        ),
        CheckConstraint(
            "trigger IN ('initial_no_profile', 'target_server_revalidation')",
            name="mcp_no_server_intent_trigger",
        ),
        CheckConstraint(
            "status IN ('armed', 'available', 'unavailable', 'dispatched', "
            "'resolved', 'converged', 'unknown')",
            name="mcp_no_server_intent_status",
        ),
        CheckConstraint("revision >= 0", name="mcp_no_server_intent_revision"),
        CheckConstraint(
            "((status IN ('resolved', 'converged', 'unknown')) AND terminal_at IS NOT NULL) OR "
            "((status NOT IN ('resolved', 'converged', 'unknown')) AND terminal_at IS NULL)",
            name="mcp_no_server_intent_terminal_at",
        ),
        CheckConstraint(
            "(trigger = 'initial_no_profile' AND node_id IS NULL AND requested_server_id IS NULL "
            "AND requested_server_config_version IS NULL AND requested_server_security_version IS NULL "
            "AND owner_server_set_fingerprint IS NOT NULL AND resume_envelope_json IS NULL "
            "AND resume_envelope_sha256 IS NULL) OR "
            "(trigger = 'target_server_revalidation' AND node_id IS NOT NULL "
            "AND requested_server_id IS NOT NULL AND owner_server_set_fingerprint IS NULL "
            "AND resume_envelope_json IS NOT NULL AND resume_envelope_sha256 IS NOT NULL "
            "AND ((requested_server_config_version IS NULL AND requested_server_security_version IS NULL) "
            "OR (requested_server_config_version > 0 AND requested_server_security_version > 0)))",
            name="mcp_no_server_intent_shape",
        ),
    )

    intent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    requested_server_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_server_config_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_server_security_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_server_set_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_envelope_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    resume_envelope_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    terminal_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPDispatchResumeOutboxRow(SQLiteBase):
    __tablename__ = "mcp_dispatch_resume_outbox"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_mcp_dispatch_resume_intent"),
        Index("idx_mcp_dispatch_resume_claim", "status", "lease_expires_at", "created_at"),
        Index("idx_mcp_dispatch_resume_status_keyset", "status", "updated_at", "outbox_id"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'active', 'waiting_approval', "
            "'waiting_input', 'remote_pending', 'completed', 'aborted')",
            name="mcp_dispatch_resume_status",
        ),
        CheckConstraint("revision >= 0", name="mcp_dispatch_resume_revision"),
        CheckConstraint(
            "(status IN ('claimed', 'active') AND claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status NOT IN ('claimed', 'active') AND claim_owner IS NULL AND claim_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="mcp_dispatch_resume_claim_shape",
        ),
        CheckConstraint(
            "(status IN ('completed', 'aborted') AND completed_at IS NOT NULL) OR "
            "(status IN ('pending', 'claimed', 'active', 'waiting_approval', "
            "'waiting_input', 'remote_pending') AND completed_at IS NULL)",
            name="mcp_dispatch_resume_terminal_at",
        ),
        CheckConstraint(
            "(status IN ('completed', 'aborted') AND completion_mode IS NOT NULL) OR "
            "(status IN ('pending', 'claimed', 'active', 'waiting_approval', "
            "'waiting_input', 'remote_pending') AND completion_mode IS NULL)",
            name="mcp_dispatch_resume_completion_mode",
        ),
        CheckConstraint(
            "completion_mode IS NULL OR completion_mode IN "
            "('completed', 'stopped_no_call', 'stopped_after_call', "
            "'failed_no_call', 'failed_after_call', 'cancelled_no_call', "
            "'cancelled_after_call', 'unknown_no_replay')",
            name="mcp_dispatch_resume_completion_mode_values",
        ),
        CheckConstraint(
            "(status = 'aborted' AND completion_mode IN "
            "('stopped_no_call', 'failed_no_call', 'cancelled_no_call')) OR "
            "(status = 'completed' AND completion_mode IN "
            "('completed', 'stopped_after_call', 'failed_after_call', "
            "'cancelled_after_call', 'unknown_no_replay')) OR "
            "status NOT IN ('completed', 'aborted')",
            name="mcp_dispatch_resume_terminal_mode_shape",
        ),
        CheckConstraint(
            "resume_reason IN ('initial', 'ordinary_terminal', 'approval_accepted', "
            "'mrtr_answer', 'remote_terminal')",
            name="mcp_dispatch_resume_reason",
        ),
        CheckConstraint(
            "(resume_reason = 'initial' AND resume_receipt_id IS NULL "
            "AND resume_answer_id IS NULL) OR "
            "(resume_reason IN ('ordinary_terminal', 'remote_terminal') "
            "AND resume_receipt_id IS NOT NULL AND resume_answer_id IS NULL "
            "AND resume_receipt_id = result_receipt_id) OR "
            "(resume_reason IN ('approval_accepted', 'mrtr_answer') "
            "AND resume_receipt_id IS NULL AND resume_answer_id IS NOT NULL)",
            name="mcp_dispatch_resume_cursor_shape",
        ),
        CheckConstraint(
            "selector_step_total >= 0 AND approval_round_total >= 0",
            name="mcp_dispatch_resume_cumulative_budget",
        ),
    )

    outbox_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    resume_envelope_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    completed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    result_receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_reason: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'initial'")
    )
    resume_receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_answer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    selector_step_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    approval_round_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class MCPPendingToolActionRow(SQLiteBase):
    __tablename__ = "mcp_pending_tool_action"
    __table_args__ = (
        UniqueConstraint(
            "arguments_payload_ref", name="uq_mcp_pending_action_payload_ref"
        ),
        UniqueConstraint(
            "approval_interrupt_id", name="uq_mcp_pending_action_interrupt"
        ),
        UniqueConstraint(
            "accepted_answer_id", name="uq_mcp_pending_action_answer"
        ),
        Index(
            "idx_mcp_pending_action_status_keyset",
            "status",
            "updated_at",
            "action_id",
        ),
        Index(
            "idx_mcp_pending_action_task_node",
            "owner_user_id",
            "task_id",
            "node_id",
        ),
        CheckConstraint(
            "status IN ('proposed', 'waiting_approval', 'approved', 'consumed', "
            "'denied', 'invalidated')",
            name="mcp_pending_action_status",
        ),
        CheckConstraint(
            "revision >= 0 AND server_config_version > 0 "
            "AND server_security_version > 0 AND encryption_version = 1",
            name="mcp_pending_action_versions",
        ),
        CheckConstraint(
            "payload_size_bytes >= 0 AND payload_size_bytes <= 33554478",
            name="mcp_pending_action_payload_size",
        ),
        CheckConstraint(
            "(status = 'proposed' AND approval_interrupt_id IS NULL "
            "AND accepted_answer_id IS NULL AND approved_at IS NULL "
            "AND consumed_at IS NULL AND invalidated_at IS NULL) OR "
            "(status = 'waiting_approval' AND approval_interrupt_id IS NOT NULL "
            "AND accepted_answer_id IS NULL AND approved_at IS NULL "
            "AND consumed_at IS NULL AND invalidated_at IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL "
            "AND consumed_at IS NULL AND invalidated_at IS NULL) OR "
            "(status = 'consumed' AND approved_at IS NOT NULL "
            "AND consumed_at IS NOT NULL AND invalidated_at IS NULL) OR "
            "(status = 'denied' AND approval_interrupt_id IS NOT NULL "
            "AND accepted_answer_id IS NOT NULL AND consumed_at IS NULL "
            "AND invalidated_at IS NOT NULL) OR "
            "(status = 'invalidated' AND consumed_at IS NULL "
            "AND invalidated_at IS NOT NULL)",
            name="mcp_pending_action_state_shape",
        ),
    )

    action_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    approval_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    payload_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    payload_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encryption_version: Mapped[int] = mapped_column(Integer, nullable=False)
    server_config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_schema_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    approval_interrupt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_answer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    approved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    consumed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    invalidated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MCPTerminalCandidateLifecycleRow(SQLiteBase):
    __tablename__ = "mcp_terminal_candidate_lifecycle"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_mcp_candidate_lifecycle_call"),
        UniqueConstraint("receipt_id", name="uq_mcp_candidate_lifecycle_receipt"),
        Index(
            "idx_mcp_candidate_lifecycle_status_keyset",
            "status",
            "updated_at",
            "candidate_id",
        ),
        CheckConstraint(
            "status IN ('retained', 'archiving', 'archived', 'deleting', 'deleted')",
            name="mcp_candidate_lifecycle_status",
        ),
        CheckConstraint("revision >= 0", name="mcp_candidate_lifecycle_revision"),
        CheckConstraint(
            "receipt_id IS NOT NULL AND consumed_at IS NOT NULL",
            name="mcp_candidate_lifecycle_consumed",
        ),
        CheckConstraint(
            "(status = 'retained' AND archive_candidate_filename IS NULL "
            "AND archive_task_index_filename IS NULL "
            "AND archive_call_index_filename IS NULL) OR "
            "(status IN ('archiving', 'archived', 'deleting', 'deleted') "
            "AND archive_candidate_filename IS NOT NULL "
            "AND archive_task_index_filename IS NOT NULL "
            "AND archive_call_index_filename IS NOT NULL)",
            name="mcp_candidate_lifecycle_archive_shape",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_schema: Mapped[str] = mapped_column(Text, nullable=False)
    active_candidate_filename: Mapped[str] = mapped_column(Text, nullable=False)
    active_task_index_filename: Mapped[str] = mapped_column(Text, nullable=False)
    active_call_index_filename: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    task_index_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    call_index_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_candidate_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_task_index_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_call_index_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    consumed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    eligible_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPDurableResultLifecycleRow(SQLiteBase):
    __tablename__ = "mcp_durable_result_lifecycle"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_mcp_durable_result_lifecycle_call"),
        UniqueConstraint("data_filename", name="uq_mcp_durable_result_data_file"),
        UniqueConstraint(
            "manifest_filename", name="uq_mcp_durable_result_manifest_file"
        ),
        Index(
            "idx_mcp_durable_result_status_keyset",
            "status",
            "updated_at",
            "result_ref",
        ),
        CheckConstraint(
            "status IN ('retained', 'artifact_owned', 'deleting', 'deleted')",
            name="mcp_durable_result_lifecycle_status",
        ),
        CheckConstraint(
            "reason IN ('dispatch_resolved', 'artifact_promoted', 'orphan')",
            name="mcp_durable_result_lifecycle_reason",
        ),
        CheckConstraint(
            "revision >= 0 AND size_bytes >= 0 AND size_bytes <= 67108864 "
            "AND store_kind = 'durable_content_addressed'",
            name="mcp_durable_result_lifecycle_shape",
        ),
        CheckConstraint(
            "(status = 'deleted' AND deleted_at IS NOT NULL) OR "
            "(status <> 'deleted' AND deleted_at IS NULL)",
            name="mcp_durable_result_lifecycle_deleted_at",
        ),
        CheckConstraint(
            "(status = 'artifact_owned' AND reason = 'artifact_promoted') OR "
            "(status = 'retained' AND reason IN ('dispatch_resolved', 'orphan')) OR "
            "status IN ('deleting', 'deleted')",
            name="mcp_durable_result_lifecycle_reason_shape",
        ),
    )

    result_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_filename: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_filename: Mapped[str] = mapped_column(Text, nullable=False)
    data_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    store_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    eligible_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    deleted_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPDispatchAggregateMigrationRow(SQLiteBase):
    __tablename__ = "mcp_dispatch_aggregate_migration"
    __table_args__ = (
        UniqueConstraint(
            "backend", "schema_version", name="uq_mcp_dispatch_migration_backend_schema"
        ),
        Index(
            "idx_mcp_dispatch_migration_status_keyset",
            "status",
            "updated_at",
            "migration_id",
        ),
        CheckConstraint(
            "backend IN ('sqlite', 'postgresql')",
            name="mcp_dispatch_migration_backend",
        ),
        CheckConstraint(
            "status IN ('planned', 'backed_up', 'applying', 'applied', 'failed')",
            name="mcp_dispatch_migration_status",
        ),
        CheckConstraint("revision >= 0", name="mcp_dispatch_migration_revision"),
        CheckConstraint(
            "(backend = 'sqlite' AND ((backup_basename IS NULL "
            "AND backup_sha256 IS NULL AND status IN ('planned', 'failed')) OR "
            "(backup_basename IS NOT NULL AND backup_sha256 IS NOT NULL "
            "AND status IN ('backed_up', 'applying', 'applied', 'failed')))) OR "
            "(backend = 'postgresql' AND backup_basename IS NULL "
            "AND backup_sha256 IS NULL)",
            name="mcp_dispatch_migration_backup_shape",
        ),
        CheckConstraint(
            "(status = 'failed' AND failure_reason_code IS NOT NULL) OR "
            "(status <> 'failed' AND failure_reason_code IS NULL)",
            name="mcp_dispatch_migration_failure_shape",
        ),
    )

    migration_id: Mapped[str] = mapped_column(Text, primary_key=True)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    report_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    backup_basename: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    failure_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPNoServerConvergenceReceiptRow(SQLiteBase):
    __tablename__ = "mcp_no_server_convergence_receipt"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_mcp_no_server_receipt_task"),
        UniqueConstraint("runtime_unavailable_event_id", name="uq_mcp_no_server_runtime_event"),
        UniqueConstraint("task_failed_event_id", name="uq_mcp_no_server_failed_event"),
        CheckConstraint(
            "terminal_code = 'mcp_runtime_unavailable'",
            name="mcp_no_server_receipt_terminal_code",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    terminal_code: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_unavailable_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_failed_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    committed_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPLegacyRetirementEvidenceRow(SQLiteBase):
    __tablename__ = "mcp_legacy_retirement_evidence"
    __table_args__ = (
        UniqueConstraint("task_id", "evidence_sha256", name="uq_mcp_legacy_retirement_evidence"),
        CheckConstraint(
            "bundle_revision IS NOT NULL OR capability_id IS NOT NULL OR may_have_dispatched = true",
            name="mcp_legacy_retirement_evidence_shape",
        ),
    )

    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    may_have_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPLegacyRetirementReceiptRow(SQLiteBase):
    __tablename__ = "mcp_legacy_retirement_receipt"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_mcp_legacy_retirement_receipt_task"),
        UniqueConstraint("event_id", name="uq_mcp_legacy_retirement_receipt_event"),
        CheckConstraint(
            "terminal_reason_code = 'legacy_runtime_retired'",
            name="mcp_legacy_retirement_reason",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    terminal_reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    terminal_evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    committed_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPTerminalResultReceiptRow(SQLiteBase):
    __tablename__ = "mcp_terminal_result_receipt"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_mcp_terminal_result_candidate"),
        UniqueConstraint("call_id", name="uq_mcp_terminal_result_call"),
        CheckConstraint(
            "terminal_state IN ('completed', 'failed', 'cancelled')",
            name="mcp_terminal_result_state",
        ),
        CheckConstraint(
            "completion_mode IN ('normal_terminal_projection', 'late_result_no_continuation')",
            name="mcp_terminal_result_completion_mode",
        ),
        CheckConstraint(
            "(terminal_state = 'completed' AND safe_result_ref IS NOT NULL "
            "AND safe_result_ref_sha256 IS NOT NULL AND safe_error_code IS NULL "
            "AND ((safe_result_content_sha256 IS NULL "
            "AND safe_result_size_bytes IS NULL AND safe_result_store_kind IS NULL) OR "
            "(safe_result_content_sha256 IS NOT NULL "
            "AND length(safe_result_content_sha256) = 71 "
            "AND substr(safe_result_content_sha256, 1, 7) = 'sha256:' "
            "AND safe_result_size_bytes >= 0 AND safe_result_size_bytes <= 67108864 "
            "AND safe_result_store_kind = 'durable_content_addressed'))) OR "
            "(terminal_state IN ('failed', 'cancelled') AND safe_result_ref IS NULL "
            "AND safe_result_ref_sha256 IS NULL AND safe_error_code IS NOT NULL "
            "AND safe_result_content_sha256 IS NULL "
            "AND safe_result_size_bytes IS NULL AND safe_result_store_kind IS NULL)",
            name="mcp_terminal_result_payload_shape",
        ),
        CheckConstraint(
            "server_config_version > 0 AND server_security_version > 0",
            name="mcp_terminal_result_server_versions",
        ),
    )

    result_receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    server_config_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_security_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    terminal_state: Mapped[str] = mapped_column(Text, nullable=False)
    result_payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    safe_result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result_ref_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result_content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    safe_result_store_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_parser_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_checkpoint_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_model_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_mode: Mapped[str] = mapped_column(Text, nullable=False)
    committed_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPExecutionTerminalProjectionRow(SQLiteBase):
    __tablename__ = "mcp_execution_terminal_projection"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_mcp_terminal_projection_call"),
        UniqueConstraint("intent_id", name="uq_mcp_terminal_projection_intent"),
        UniqueConstraint("unknown_event_id", name="uq_mcp_terminal_projection_unknown_event"),
        UniqueConstraint("task_failed_event_id", name="uq_mcp_terminal_projection_failed_event"),
        UniqueConstraint("result_receipt_id", name="uq_mcp_terminal_projection_result_receipt"),
        UniqueConstraint("resolution_event_id", name="uq_mcp_terminal_projection_resolution_event"),
        UniqueConstraint("correction_event_id", name="uq_mcp_terminal_projection_correction_event"),
        CheckConstraint(
            "status IN ('unknown', 'late_result_resolved')",
            name="mcp_terminal_projection_status",
        ),
        CheckConstraint("revision IN (0, 1)", name="mcp_terminal_projection_revision"),
        CheckConstraint("no_replay = true", name="mcp_terminal_projection_no_replay"),
        CheckConstraint(
            "reason_code = 'trusted_terminal_result_absent'",
            name="mcp_terminal_projection_reason",
        ),
        CheckConstraint(
            "task_terminal_status = 'failed' AND node_terminal_status = 'failed'",
            name="mcp_terminal_projection_failed_authority",
        ),
        CheckConstraint(
            "(status = 'unknown' AND revision = 0 AND result_receipt_id IS NULL "
            "AND result_payload_sha256 IS NULL AND resolved_terminal_state IS NULL "
            "AND safe_result_ref IS NULL AND safe_result_ref_sha256 IS NULL "
            "AND safe_error_code IS NULL AND resolved_intent_revision IS NULL "
            "AND resolution_event_id IS NULL AND correction_event_id IS NULL "
            "AND result_committed_at IS NULL AND resolved_at IS NULL) OR "
            "(status = 'late_result_resolved' AND revision = 1 "
            "AND result_receipt_id IS NOT NULL AND result_payload_sha256 IS NOT NULL "
            "AND resolved_terminal_state IN ('completed', 'failed', 'cancelled') "
            "AND resolved_intent_revision IS NOT NULL AND resolution_event_id IS NOT NULL "
            "AND correction_event_id IS NOT NULL AND result_committed_at IS NOT NULL "
            "AND resolved_at IS NOT NULL)",
            name="mcp_terminal_projection_resolution_shape",
        ),
        CheckConstraint(
            "resolved_terminal_state IS NULL OR "
            "(resolved_terminal_state = 'completed' AND safe_result_ref IS NOT NULL "
            "AND safe_result_ref_sha256 IS NOT NULL AND safe_error_code IS NULL) OR "
            "(resolved_terminal_state IN ('failed', 'cancelled') AND safe_result_ref IS NULL "
            "AND safe_result_ref_sha256 IS NULL AND safe_error_code IS NOT NULL)",
            name="mcp_terminal_projection_result_shape",
        ),
    )

    projection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    no_replay: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    unknown_intent_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_failed_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    unknown_terminal_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    task_terminal_status: Mapped[str] = mapped_column(Text, nullable=False)
    node_terminal_status: Mapped[str] = mapped_column(Text, nullable=False)
    result_receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_payload_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_terminal_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result_ref_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_intent_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_committed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPCP7SafetyLedgerRow(SQLiteBase):
    __tablename__ = "mcp_cp7_safety_ledger"
    __table_args__ = (
        Index("idx_mcp_cp7_safety_candidate_epoch", "candidate_id", "epoch_id", "recorded_at"),
        CheckConstraint(
            "record_kind IN ('registration', 'attestation', 'violation', 'gap')",
            name="mcp_cp7_safety_record_kind",
        ),
        CheckConstraint(
            "(red_line IS NULL AND hook_id IS NULL) OR "
            "(red_line = 'cross_user_access' AND hook_id = 'gateway.task_owner_boundary') OR "
            "(red_line = 'secret_exposure' AND hook_id = 'audit.secret_payload_boundary') OR "
            "(red_line = 'dual_tool_call' AND hook_id = 'dispatch.durable_call_idempotency_boundary') OR "
            "(red_line = 'unauthorized_tool_call' AND hook_id = 'dispatch.permission_boundary') OR "
            "(red_line = 'endpoint_policy_bypass' AND hook_id = 'gateway.endpoint_policy_boundary') OR "
            "(red_line = 'unknown_result_replay' AND hook_id = 'recovery.unknown_replay_boundary') OR "
            "(red_line = 'shadow_tool_call' AND hook_id = 'gateway.persisted_assignment_boundary') OR "
            "(red_line = 'persistent_resource_leak' AND hook_id = 'gateway.resource_cleanup_boundary')",
            name="mcp_cp7_safety_authoritative_hook",
        ),
        CheckConstraint(
            "record_kind <> 'violation' OR "
            "(red_line = 'cross_user_access' AND reason_code = 'task_owner_mismatch') OR "
            "(red_line = 'secret_exposure' AND reason_code = 'secret_payload_rejected') OR "
            "(red_line = 'dual_tool_call' AND reason_code = 'call_idempotency_conflict') OR "
            "(red_line = 'unauthorized_tool_call' AND reason_code = 'permission_denied_boundary') OR "
            "(red_line = 'endpoint_policy_bypass' AND reason_code = 'endpoint_policy_rejected') OR "
            "(red_line = 'unknown_result_replay' AND reason_code = 'unknown_replay_blocked') OR "
            "(red_line = 'shadow_tool_call' AND reason_code = 'shadow_call_blocked') OR "
            "(red_line = 'persistent_resource_leak' AND reason_code = 'cleanup_failed')",
            name="mcp_cp7_safety_violation_reason",
        ),
        CheckConstraint("value IN (0, 1)", name="mcp_cp7_safety_value"),
        CheckConstraint(
            "(record_kind = 'registration' AND red_line IS NOT NULL AND hook_id IS NOT NULL "
            "AND bucket_started_at IS NULL AND bucket_ended_at IS NULL "
            "AND reason_code = 'registered' AND value = 0 AND boundary_source_sha256 IS NULL) OR "
            "(record_kind = 'attestation' AND red_line IS NOT NULL AND hook_id IS NOT NULL "
            "AND bucket_started_at IS NOT NULL AND bucket_ended_at IS NOT NULL "
            "AND reason_code = 'observed_zero' AND value = 0 AND boundary_source_sha256 IS NULL) OR "
            "(record_kind = 'violation' AND red_line IS NOT NULL AND hook_id IS NOT NULL "
            "AND bucket_started_at IS NOT NULL AND bucket_ended_at IS NOT NULL "
            "AND value = 1 AND boundary_source_sha256 IS NOT NULL) OR "
            "(record_kind = 'gap' AND value = 1 AND boundary_source_sha256 IS NOT NULL "
            "AND ((red_line IS NULL AND hook_id IS NULL) OR "
            "(red_line IS NOT NULL AND hook_id IS NOT NULL)))",
            name="mcp_cp7_safety_record_shape",
        ),
        CheckConstraint(
            "record_kind <> 'gap' OR reason_code IN ('detector_unregistered', "
            "'detector_unhealthy', 'interval_attestation_missing', "
            "'safety_metric_write_failed', 'terminal_metric_write_failed', "
            "'producer_interval_missed', 'zero_series_write_failed', "
            "'unplanned_process_exit', 'maintenance_boundary_invalid')",
            name="mcp_cp7_safety_gap_reason",
        ),
    )

    record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(Text, nullable=False)
    epoch_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    record_kind: Mapped[str] = mapped_column(Text, nullable=False)
    red_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    bucket_started_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    bucket_ended_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    boundary_source_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class MCPCP7ReadyEpochEventRow(SQLiteBase):
    __tablename__ = "mcp_cp7_ready_epoch_event"
    __table_args__ = (
        UniqueConstraint("candidate_id", "epoch_id", "event_kind", name="uq_mcp_cp7_epoch_kind"),
        Index("idx_mcp_cp7_epoch_candidate_boundary", "candidate_id", "boundary_at"),
        CheckConstraint(
            "event_kind IN ('opened', 'ready', 'maintenance_started', 'closed', 'invalidated')",
            name="mcp_cp7_epoch_event_kind",
        ),
        CheckConstraint("audit_inode >= 0", name="mcp_cp7_epoch_audit_inode"),
        CheckConstraint("audit_offset >= 0", name="mcp_cp7_epoch_audit_offset"),
        CheckConstraint("ledger_record_count >= 0", name="mcp_cp7_epoch_ledger_count"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(Text, nullable=False)
    epoch_id: Mapped[str] = mapped_column(Text, nullable=False)
    predecessor_epoch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    container_id: Mapped[str] = mapped_column(Text, nullable=False)
    image_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    boundary_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    audit_device: Mapped[str] = mapped_column(Text, nullable=False)
    audit_inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ledger_record_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inflight_state_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)


class MCPCP7CandidateGuardRow(SQLiteBase):
    __tablename__ = "mcp_cp7_candidate_guard"
    __table_args__ = (
        CheckConstraint("invalid_latched IN (false, true)", name="mcp_cp7_guard_latch"),
        CheckConstraint(
            "(invalid_latched = false AND first_invalid_record_id IS NULL "
            "AND first_invalid_reason IS NULL AND first_invalid_at IS NULL) OR "
            "(invalid_latched = true AND first_invalid_record_id IS NOT NULL "
            "AND first_invalid_reason IS NOT NULL AND first_invalid_at IS NOT NULL)",
            name="mcp_cp7_guard_shape",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(Text, primary_key=True)
    invalid_latched: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    first_invalid_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_invalid_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_invalid_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class TaskRow(SQLiteBase):
    __tablename__ = "task"
    __table_args__ = (
        Index("idx_task_conversation_created", "conversation_id", "created_at"),
        Index("idx_task_status_updated", "status", "updated_at"),
        CheckConstraint(
            "mcp_execution_mode IS NULL OR "
            "mcp_execution_mode IN ('legacy', 'user_scoped', 'unavailable')",
            name="task_mcp_execution_mode",
        ),
        CheckConstraint(
            "mcp_rollout_mode IS NULL OR mcp_rollout_mode IN ('off', 'shadow', 'enforce')",
            name="task_mcp_rollout_mode",
        ),
        CheckConstraint(
            "mcp_route_reason_code IS NULL OR mcp_route_reason_code IN "
            "('routing_off', 'shadow_enabled', 'enforce_selected', "
            "'cohort_not_selected', 'percent_not_selected', "
            "'explicit_legacy_capability', 'user_server_rollout_unavailable', "
            "'no_execution_path', 'no_user_scoped_server')",
            name="task_mcp_route_reason_code",
        ),
        CheckConstraint(
            "(mcp_execution_mode IS NULL AND mcp_shadow_enabled IS NULL AND "
            "mcp_rollout_config_version IS NULL AND mcp_route_reason_code IS NULL AND "
            "mcp_rollout_mode IS NULL) OR "
            "(mcp_execution_mode IS NOT NULL AND mcp_shadow_enabled IS NOT NULL AND "
            "mcp_rollout_config_version IS NOT NULL AND mcp_route_reason_code IS NOT NULL AND "
            "mcp_rollout_mode IS NOT NULL)",
            name="task_mcp_assignment_all_or_none",
        ),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    root_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    routing_mode: Mapped[str] = mapped_column(Text, nullable=False)
    requested_capability_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    mcp_execution_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_shadow_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mcp_rollout_config_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_route_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_rollout_mode: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRunRow(SQLiteBase):
    __tablename__ = "agent_run"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_agent_run_task"),
        Index("idx_agent_run_status_updated", "status", "updated_at"),
        CheckConstraint(
            "status IN ('running', 'waiting_for_input', 'waiting_for_dependency', "
            "'completed', 'failed', 'cancelled')",
            name="agent_run_status",
        ),
        CheckConstraint("next_item_sequence > 0", name="agent_run_next_sequence_positive"),
        CheckConstraint("compacted_through_sequence >= 0", name="agent_run_compacted_non_negative"),
        CheckConstraint("revision >= 0", name="agent_run_revision_non_negative"),
    )

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    model_edition: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(Text, nullable=False)
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    binding_option_digests: Mapped[dict] = mapped_column(JSONText(), nullable=False)
    next_item_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    compacted_through_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_sample_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    waiting_call_item_ids: Mapped[list] = mapped_column(JSONText(), nullable=False)
    next_batch_call_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claim_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    terminal_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    terminal_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AgentItemRow(SQLiteBase):
    __tablename__ = "agent_item"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_item_run_sequence"),
        UniqueConstraint("source_call_item_id", name="uq_agent_item_call_result"),
        UniqueConstraint("run_id", "provider_sample_id", name="uq_agent_item_provider_sample"),
        Index("idx_agent_item_task_sequence", "task_id", "sequence"),
        CheckConstraint("sequence > 0", name="agent_item_sequence_positive"),
        CheckConstraint(
            "kind IN ('user_message', 'assistant_message', 'tool_call', 'tool_result', "
            "'skill_activation', 'context_summary', 'continuation')",
            name="agent_item_kind",
        ),
        CheckConstraint("state IN ('reserved', 'committed')", name="agent_item_state"),
        CheckConstraint("payload_size_bytes <= 131072", name="agent_item_payload_size"),
    )

    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    parent_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_call_item_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_sample_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_ordinal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    committed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class AgentFinalReceiptRow(SQLiteBase):
    __tablename__ = "agent_final_receipt"
    __table_args__ = (UniqueConstraint("run_id", name="uq_agent_final_receipt_run"),)

    receipt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_item_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class PlannerReplanClaimRow(SQLiteBase):
    __tablename__ = "planner_replan_claim"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "planning_revision",
            name="uq_planner_replan_claim_task_revision",
        ),
        CheckConstraint(
            "planning_revision >= 1",
            name="planner_replan_claim_positive_revision",
        ),
        CheckConstraint(
            "status IN ('claimed', 'applied', 'rejected')",
            name="planner_replan_claim_status",
        ),
        Index(
            "idx_planner_replan_claim_task_status",
            "task_id",
            "status",
            "updated_at",
        ),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    planning_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    planning_epoch: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTimeText(), nullable=False)


class TaskNodeRow(SQLiteBase):
    __tablename__ = "task_node"
    __table_args__ = (
        Index("idx_task_node_task_status", "task_id", "status"),
        Index("idx_task_node_capability_status", "capability_id", "status"),
        Index("idx_task_node_started", "started_at"),
    )

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    assigned_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    criticality: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_type: Mapped[str] = mapped_column(Text, nullable=False)
    retry_policy: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    timeout_policy: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    resource_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_refs: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    output_refs: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskEdgeRow(SQLiteBase):
    __tablename__ = "task_edge"
    __table_args__ = (
        UniqueConstraint("task_id", "from_node_id", "to_node_id"),
        Index("idx_task_edge_to_node", "task_id", "to_node_id"),
    )

    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArtifactRow(SQLiteBase):
    __tablename__ = "artifact"
    __table_args__ = (
        Index("idx_artifact_task_created", "task_id", "created_at"),
        Index("idx_artifact_node_created", "producer_node_id", "created_at"),
    )

    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class TaskInputAttachmentRow(SQLiteBase):
    __tablename__ = "task_input_attachment"
    __table_args__ = (
        Index("idx_task_input_attachment_task_created", "task_id", "created_at"),
        Index("idx_task_input_attachment_conversation_task", "conversation_id", "task_id"),
        Index("idx_task_input_attachment_conversation_recent", "conversation_id", "updated_at", "created_at", "attachment_id"),
        Index("idx_task_input_attachment_upload", "source_upload_id"),
    )

    attachment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    interrupt_answer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_artifact: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    skill_artifact: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    source_payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    selected_sheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class EventRecordRow(SQLiteBase):
    __tablename__ = "event_record"
    __table_args__ = (
        Index("idx_event_task_created", "task_id", "created_at"),
        Index("idx_event_conversation_created", "conversation_id", "created_at"),
        Index("idx_event_type_created", "event_type", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MailboxMessageRow(SQLiteBase):
    __tablename__ = "mailbox_message"
    __table_args__ = (
        Index("idx_mailbox_message_conversation_created", "conversation_id", "created_at"),
        Index("idx_mailbox_message_task_created", "task_id", "created_at"),
        Index("idx_mailbox_message_node_created", "node_id", "created_at"),
        Index("idx_mailbox_message_channel_type_created", "channel", "message_type", "created_at"),
        Index("idx_mailbox_message_correlation", "correlation_id"),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_agent: Mapped[str] = mapped_column(Text, nullable=False)
    to_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    ack_policy: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class MailboxDeliveryRow(SQLiteBase):
    __tablename__ = "mailbox_delivery"
    __table_args__ = (
        UniqueConstraint("message_id", "recipient_agent"),
        Index("idx_mailbox_delivery_message", "message_id"),
        Index("idx_mailbox_delivery_status_expires", "status", "expires_at"),
        Index("idx_mailbox_delivery_recipient_status", "recipient_agent", "status", "created_at"),
        Index("idx_mailbox_delivery_retry", "next_retry_at"),
    )

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_agent: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    delivered_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    acknowledged_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    resolved_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    next_retry_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class InterruptRow(SQLiteBase):
    __tablename__ = "interrupt"
    __table_args__ = (
        Index("idx_interrupt_conversation_status", "conversation_id", "status", "created_at"),
        Index("idx_interrupt_task_node", "task_id", "node_id"),
        Index("idx_interrupt_expires", "expires_at"),
    )

    interrupt_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_agent: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    required_fields: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    answered_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class InterruptAnswerRow(SQLiteBase):
    __tablename__ = "interrupt_answer"
    __table_args__ = (Index("idx_interrupt_answer_interrupt_created", "interrupt_id", "created_at"),)

    interrupt_answer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    interrupt_id: Mapped[str] = mapped_column(Text, nullable=False)
    answer_payload: Mapped[dict] = mapped_column(JSONText(), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    accepted_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class SlotCollectionRow(SQLiteBase):
    __tablename__ = "slot_collection"
    __table_args__ = (
        Index("idx_slot_collection_task_node_status", "task_id", "node_id", "status"),
        Index("idx_slot_collection_task_status", "task_id", "status"),
        Index("idx_slot_collection_conversation_status_updated", "conversation_id", "status", "updated_at"),
    )

    collection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_schema_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_entrypoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_bundle_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    contract_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_snapshot_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    slots_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    resolved_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    missing_json: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    invalid_json: Mapped[list | None] = mapped_column(JSONText(), nullable=True)
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    failed_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class SlotEventRow(SQLiteBase):
    __tablename__ = "slot_event"
    __table_args__ = (
        UniqueConstraint("collection_id", "idempotency_key", name="uq_slot_event_collection_idempotency"),
        Index("idx_slot_event_collection_created", "collection_id", "created_at"),
        Index("idx_slot_event_task_created", "task_id", "created_at"),
    )

    slot_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    collection_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONText(), nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)


class CheckpointRow(SQLiteBase):
    __tablename__ = "checkpoint"
    __table_args__ = (
        Index("idx_checkpoint_task_node", "task_id", "node_id", "created_at"),
        Index("idx_checkpoint_resume_token", "resume_token"),
    )

    checkpoint_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(Text, nullable=False)
    resume_token: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)
    invalidated_at: Mapped[object | None] = mapped_column(DateTimeText(), nullable=True)

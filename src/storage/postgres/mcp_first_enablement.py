"""Explicit administrator operation; never called by application startup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import Engine, func, select, text

from src.integrations.mcp.observability import (
    mcp_evidence_snapshot_to_record,
    validate_mcp_evidence_snapshot_record,
)
from src.integrations.mcp.rollout import MCPRolloutConfig, MCPRoutingMode
from src.integrations.mcp.rollout_evidence import (
    FIRST_ENABLEMENT_EMPTY_TABLES,
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPFirstEnablementPayload,
    MCPRolloutStage,
    canonical_evidence_content_digest,
)
from src.core.models import MCPRolloutEvidenceSnapshot as EvidenceRecord
from src.state.postgres.runtime_schema import (
    build_postgres_fresh_cutover_schema_manifest,
)
from src.state.postgres.schema_reconciler import plan_postgres_schema_reconciliation
from src.storage.postgres.bootstrap import _inspect_current_schema
from src.storage.postgres.session import validate_mcp_rollout_connection_role
from src.storage.sqlalchemy_base import SQLiteBase
from src.storage.sqlalchemy_models import (
    MCPRolloutDeploymentActivationRow,
    MCPRolloutEvidenceSnapshotRow,
    MCPRolloutGateScopeRow,
    MCPRolloutStageApprovalRow,
)

PROGRAM = "user_mcp_phase3"
STAGE = MCPRolloutStage.LEGACY_ASSEMBLY_OFF.value
REASON = (
    "First production enablement of new user-configurable MCP; no legacy MCP migration."
)
_LOCK_KEY = 7421893560821641
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


class MCPFirstEnablementError(ValueError):
    """Only safe, fixed error codes may cross the CLI boundary."""


@dataclass(frozen=True, slots=True)
class FirstEnablementRequest:
    database_name: str
    environment_id: str
    deployment_id: str
    activation_id: str
    git_sha: str
    backend_image_digest: str

    def validate(self) -> None:
        if any(
            not _ID.fullmatch(value)
            for value in (
                self.database_name,
                self.environment_id,
                self.deployment_id,
                self.activation_id,
            )
        ):
            raise MCPFirstEnablementError("invalid_release_identity")
        if not re.fullmatch(r"[0-9a-f]{40}", self.git_sha):
            raise MCPFirstEnablementError("invalid_git_sha")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.backend_image_digest):
            raise MCPFirstEnablementError("invalid_backend_image_digest")

    @property
    def evidence_id(self) -> str:
        return "first-evidence-" + canonical_evidence_content_digest(asdict(self))[:32]

    @property
    def approval_id(self) -> str:
        return "first-approval-" + canonical_evidence_content_digest(asdict(self))[:32]


def initialize_mcp_first_production(
    engine: Engine,
    app_engine: Engine,
    request: FirstEnablementRequest,
    config: MCPRolloutConfig,
    *,
    apply: bool = False,
    expected_check_digest: str | None = None,
) -> dict[str, object]:
    """Check without writes, or atomically apply an explicitly checked release."""
    request.validate()
    if engine.dialect.name != "postgresql" or app_engine.dialect.name != "postgresql":
        raise MCPFirstEnablementError("postgresql_required")
    if not (
        config.gateway_enabled
        and config.routing_mode is MCPRoutingMode.ENFORCE
        and config.enforce_percent == 100
        and not config.enforce_cohorts
        and not config.legacy_enabled
        and not config.cohort_file_digest
    ):
        raise MCPFirstEnablementError(
            "first_enablement_requires_final_production_config"
        )
    with engine.connect() as connection, connection.begin():
        if not apply:
            connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
        connection.execute(text("SET LOCAL search_path = pg_catalog, public, pg_temp"))
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        identity = (
            connection.execute(
                text(
                    "SELECT current_database() AS database, session_user AS login, "
                    "current_user AS effective, rolsuper, current_setting('session_replication_role') AS replication "
                    "FROM pg_roles WHERE rolname = session_user"
                )
            )
            .mappings()
            .one()
        )
        if identity["database"] != request.database_name:
            raise MCPFirstEnablementError("database_mismatch")
        if (
            identity["login"] != identity["effective"]
            or not identity["rolsuper"]
            or identity["replication"] != "origin"
        ):
            raise MCPFirstEnablementError("real_superuser_login_required")
        if apply:
            if not connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            ):
                raise MCPFirstEnablementError("initialization_busy")
        if apply:
            tables = sorted((*FIRST_ENABLEMENT_EMPTY_TABLES, "mcp_rollout_gate_scope"))
            connection.execute(
                text(
                    "LOCK TABLE "
                    + ", ".join(f'public."{name}"' for name in tables)
                    + " IN SHARE ROW EXCLUSIVE MODE NOWAIT"
                )
            )
        manifest = build_postgres_fresh_cutover_schema_manifest()
        plan = plan_postgres_schema_reconciliation(
            manifest, _inspect_current_schema(connection)
        )
        # The reconciler always emits this conditional data backfill, even on
        # an empty current schema. Initialization never executes it.
        schema_actions = [
            action
            for action in plan.actions
            if action.kind != "backfill_mcp_remote_task_publication"
        ]
        if schema_actions or plan.operator_only_actions:
            raise MCPFirstEnablementError("schema_upgrade_required")
        _validate_history_protection(connection)
        # Validate the actual independent LOGIN, its API owner and permission matrix.
        app_login = validate_mcp_rollout_connection_role(app_engine, "app")
        with app_engine.connect() as app_connection:
            marker = "mcp-first-enable-" + uuid4().hex
            app_pid = app_connection.scalar(
                text(
                    "SELECT pg_backend_pid() FROM set_config('application_name', :marker, true)"
                ),
                {"marker": marker},
            )
            same_database = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid AND application_name = :marker "
                    "AND datname = current_database() AND usename = :login"
                ),
                {"pid": app_pid, "marker": marker, "login": app_login},
            )
            if same_database != 1:
                raise MCPFirstEnablementError("app_database_mismatch")
        initial_counts = {name: 0 for name in FIRST_ENABLEMENT_EMPTY_TABLES}
        check_digest = canonical_evidence_content_digest(
            {
                "request": asdict(request),
                "config_fingerprint": config.fingerprint,
                "schema_checksum": manifest.checksum,
                "administrator": identity["login"],
                "app_login": app_login,
                "initial_table_counts": initial_counts,
            }
        )
        report = {
            "database": request.database_name,
            "administrator": identity["login"],
            "app_login": app_login,
            "environment_id": request.environment_id,
            "deployment_id": request.deployment_id,
            "activation_id": request.activation_id,
            "evidence_id": request.evidence_id,
            "approval_id": request.approval_id,
            "config_fingerprint": config.fingerprint,
            "schema_checksum": manifest.checksum,
            "check_digest": check_digest,
        }
        if apply and expected_check_digest != check_digest:
            raise MCPFirstEnablementError("check_digest_mismatch")
        if _is_exact_retry(connection, request, config, str(identity["login"])):
            return {**report, "status": "already_initialized"}
        counts = {
            name: connection.scalar(
                select(func.count()).select_from(SQLiteBase.metadata.tables[name])
            )
            for name in FIRST_ENABLEMENT_EMPTY_TABLES
        }
        if any(counts.values()):
            raise MCPFirstEnablementError("mcp_or_rollout_data_already_exists")
        if not apply:
            return {**report, "status": "ready", "initial_table_counts": counts}
        now = datetime.now(timezone.utc)
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id=request.evidence_id,
            environment_id=request.environment_id,
            git_sha=request.git_sha,
            deployment_id=request.deployment_id,
            stage=MCPRolloutStage.OFF,
            config_fingerprint=config.fingerprint,
            window_started_at=now,
            window_ended_at=now,
            recorded_at=now,
            producer=MCPEvidenceProducer.DEPLOYMENT_INITIALIZER,
            source=MCPEvidenceSource.DEPLOYMENT,
            snapshot_id=1,
            nonce=request.evidence_id,
            payload=MCPFirstEnablementPayload(
                kind=MCPEvidenceKind.FIRST_ENABLEMENT,
                schema_version="maf.mcp.first_enablement.v1",
                database_name=request.database_name,
                backend_image_digest=request.backend_image_digest,
                administrator=str(identity["login"]),
                initial_table_counts=counts,
                initial_state_digest=canonical_evidence_content_digest(counts),
            ),
        )
        scope = MCPRolloutGateScopeRow.__table__
        if (
            connection.execute(
                select(scope).where(
                    scope.c.environment_id == request.environment_id,
                    scope.c.rollout_program == PROGRAM,
                )
            ).first()
            is None
        ):
            connection.execute(
                scope.insert().values(
                    environment_id=request.environment_id,
                    rollout_program=PROGRAM,
                    created_at=now,
                )
            )
        record = mcp_evidence_snapshot_to_record(snapshot)
        connection.execute(
            MCPRolloutEvidenceSnapshotRow.__table__.insert().values(asdict(record))
        )
        target = dict(
            environment_id=request.environment_id,
            rollout_program=PROGRAM,
            deployment_id=request.deployment_id,
            stage=STAGE,
            config_fingerprint=config.fingerprint,
            evidence_id=request.evidence_id,
            created_at=now,
        )
        connection.execute(
            MCPRolloutStageApprovalRow.__table__.insert().values(
                **target,
                approval_id=request.approval_id,
                reason=REASON,
                approver=identity["login"],
            )
        )
        connection.execute(
            MCPRolloutDeploymentActivationRow.__table__.insert().values(
                **target,
                activation_id=request.activation_id,
                approval_id=request.approval_id,
                previous_activation_id=None,
                operator_reason=REASON,
                is_rollback=False,
            )
        )
        return {**report, "status": "initialized", "initial_table_counts": counts}


def _is_exact_retry(connection, request, config, administrator: str) -> bool:
    evidence_table = MCPRolloutEvidenceSnapshotRow.__table__
    evidence = (
        connection.execute(
            select(evidence_table).where(
                evidence_table.c.evidence_id == request.evidence_id
            )
        )
        .mappings()
        .first()
    )
    if evidence is None:
        return False
    record = EvidenceRecord(**dict(evidence))
    if validate_mcp_evidence_snapshot_record(record) or any(
        (
            record.source != MCPEvidenceSource.DEPLOYMENT.value,
            record.environment_id != request.environment_id,
            record.deployment_id != request.deployment_id,
            record.git_sha != request.git_sha,
            record.config_fingerprint != config.fingerprint,
            record.payload.get("database_name") != request.database_name,
            record.payload.get("backend_image_digest") != request.backend_image_digest,
            record.payload.get("administrator") != administrator,
        )
    ):
        raise MCPFirstEnablementError("existing_initialization_mismatch")
    expected = dict(
        environment_id=request.environment_id,
        rollout_program=PROGRAM,
        deployment_id=request.deployment_id,
        stage=STAGE,
        config_fingerprint=config.fingerprint,
        evidence_id=request.evidence_id,
        created_at=record.recorded_at,
    )
    for table, fields in (
        (
            MCPRolloutStageApprovalRow.__table__,
            dict(
                **expected,
                approval_id=request.approval_id,
                reason=REASON,
                approver=administrator,
            ),
        ),
        (
            MCPRolloutDeploymentActivationRow.__table__,
            dict(
                **expected,
                activation_id=request.activation_id,
                approval_id=request.approval_id,
                previous_activation_id=None,
                operator_reason=REASON,
                is_rollback=False,
            ),
        ),
    ):
        rows = connection.execute(select(table)).mappings().all()
        if len(rows) != 1 or dict(rows[0]) != fields:
            raise MCPFirstEnablementError("partial_or_superseded_initialization")
    if connection.scalar(select(func.count()).select_from(evidence_table)) != 1:
        raise MCPFirstEnablementError("partial_or_superseded_initialization")
    # Once any safety block has been recorded, use the ordinary operator process.
    blocks = SQLiteBase.metadata.tables["mcp_rollout_promotion_block"]
    if connection.scalar(select(func.count()).select_from(blocks)):
        raise MCPFirstEnablementError("rollout_safety_block_exists")
    scope = MCPRolloutGateScopeRow.__table__
    if (
        connection.execute(
            select(scope).where(
                scope.c.environment_id == request.environment_id,
                scope.c.rollout_program == PROGRAM,
            )
        ).first()
        is None
    ):
        raise MCPFirstEnablementError("partial_or_superseded_initialization")
    return True


def _validate_history_protection(connection) -> None:
    protected = (
        "mcp_rollout_drill_observation",
        "mcp_rollout_evidence_snapshot",
        "mcp_rollout_stage_approval",
        "mcp_rollout_deployment_activation",
        "mcp_rollout_promotion_block",
        "mcp_rollout_block_resolution",
    )
    for name in protected:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass(:table) "
                "AND tgname = 'mcp_rollout_history_append_only' AND tgenabled IN ('O', 'A') "
                "AND tgtype = 27 AND NOT tgisinternal "
                "AND tgfoid = to_regprocedure('mcp_rollout_api.reject_history_mutation()')"
            ),
            {"table": "public." + name},
        )
        if count != 1:
            raise MCPFirstEnablementError("rollout_history_protection_required")
    if not connection.scalar(
        text(
            "SELECT bool_and(convalidated) FROM pg_constraint "
            "WHERE conrelid = 'public.mcp_rollout_evidence_snapshot'::regclass AND contype = 'c'"
        )
    ):
        raise MCPFirstEnablementError("evidence_constraints_not_validated")

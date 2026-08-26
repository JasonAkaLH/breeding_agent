from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence, TypeVar

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.core.enums import ConversationStatus
from src.core.models import (
    Conversation,
    Interrupt,
    InterruptAnswer,
    MCPApprovalDecisionResult,
    MCPApprovalSuspendResult,
    MCPCallRecord,
    MCPDispatchResumeOutbox,
    MCPDispatchFinalizeResult,
    MCPDurableResultSnapshot,
    MCPInitialIntentCreateResult,
    MCPInputSuspendResult,
    MCPMRTRAnswerResult,
    MCPTerminalCandidateSnapshot,
    MCPTerminalResultCommitResult,
    MCPValidatedTerminalResultCandidate,
    MCPLegacyRetirementConvergenceResult,
    MCPLegacyRetirementEvidence,
    MCPNoServerConvergenceResult,
    MCPNoServerConvergenceReceipt,
    MCPPendingActionPayloadSnapshot,
    MCPPendingToolAction,
    MCPLegacyMigrationBatchResult,
    MCPLegacyMigrationRecord,
    MCPRemoteTaskBinding,
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutDrillObservation,
    MCPRolloutEvidenceSnapshot,
    MCPRolloutGateScope,
    MCPRolloutInstanceConfigLease,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
    MCPRolloutMetricBucket,
    MCPShadowAuditSample,
    MCPTargetIntentArmResult,
    MCPTargetIntentResolveResult,
    Task,
    SubmissionAdmissionRequest,
    SubmissionAdmissionResult,
    SubmissionRecoveryRecord,
    UserMCPCredentialRecord,
    UserMCPHealthAttempt,
    UserMCPScopeLease,
    UserMCPServer,
    validate_mcp_rollout_drill_observation,
)
from src.integrations.mcp.rollout_evidence import is_exact_mcp_metric_bucket_window
from src.storage.sqlalchemy_models import (
    ConversationRow,
    InterruptAnswerRow,
    InterruptRow,
    MCPBranchRecordRow,
    MCPCallRecordRow,
    MCPDispatchResumeOutboxRow,
    MCPDurableResultLifecycleRow,
    MCPExecutionTerminalProjectionRow,
    MCPNoServerIntentRow,
    MCPSealedStateRow,
    MCPPendingToolActionRow,
    MCPTerminalResultReceiptRow,
    MCPTerminalCandidateLifecycleRow,
    TaskNodeRow,
    TaskRow,
    UserMCPOwnerMutationGuardRow,
    UserMCPToolGrantRow,
    MCPRemoteTaskBindingRow,
    UserMCPHealthAttemptRow,
    UserMCPScopeLeaseRow,
    UserMCPServerRow,
)
from src.integrations.mcp.cp7_artifacts import (
    canonical_sha256,
    mcp_dispatch_resume_outbox_id,
    mcp_no_server_intent_id,
)
from src.storage.mcp_legacy_records import (
    _mcp_legacy_migration_record_values,
    _user_mcp_server_insert_values,
    _validate_mcp_legacy_migration_record,
)
from src.storage.row_mappers import (
    _mcp_owner_server_set_fingerprint,
    _row_to_conversation,
    _row_to_mcp_remote_task,
    _row_to_mcp_rollout_block_resolution,
    _row_to_mcp_rollout_deployment_activation,
    _row_to_mcp_rollout_drill_observation,
    _row_to_mcp_rollout_evidence_snapshot,
    _row_to_mcp_rollout_gate_scope,
    _row_to_mcp_rollout_instance_config,
    _row_to_mcp_rollout_metric_bucket,
    _row_to_mcp_rollout_promotion_block,
    _row_to_mcp_rollout_stage_approval,
    _row_to_mcp_shadow_audit_sample,
)
from src.storage.sqlite.repositories import (
    SQLiteStateRepository,
    SQLiteStorage,
)


_T = TypeVar("_T")


def _postgres_sqlstate(error: DBAPIError) -> str | None:
    original = error.orig
    value = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return str(value) if value is not None else None


class PostgreSQLStorage(SQLiteStorage):
    """StoragePort facade backed by PostgreSQL SQLAlchemy sessions.

    Most repository behavior reuses the mature SQLiteStorage contract while
    PostgreSQL-specific hot paths can override operations that need dialect
    features. Long conversation deletion is one such path: it must stay
    set-based inside PostgreSQL and avoid pulling large task/message id lists
    into Python.
    """

    def __init__(
        self,
        session_factory,
        *,
        mcp_rollout_session_factory=None,
        mcp_rollout_role: str | None = None,
        mcp_legacy_migration_session_factory=None,
        mcp_legacy_migration_role: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_factory, **kwargs)
        if mcp_rollout_role not in {
            None,
            "app",
            "snapshot",
            "ci",
            "evaluator",
            "operator",
            "validator",
            "drill",
        }:
            raise ValueError("unsupported MCP rollout PostgreSQL role")
        self._mcp_rollout_session_factory = (
            mcp_rollout_session_factory or session_factory
        )
        self._mcp_rollout_role = mcp_rollout_role
        self._mcp_legacy_migration_session_factory = (
            mcp_legacy_migration_session_factory
        )
        self._mcp_legacy_migration_role = mcp_legacy_migration_role

    async def _run_submission_admission(
        self, request: SubmissionAdmissionRequest
    ) -> SubmissionAdmissionResult:
        return await self._run_submission_write_with_unique_retry(
            lambda state: state.admit_submission_sql(request)
        )

    async def _run_submission_projection(
        self, record: SubmissionRecoveryRecord
    ) -> None:
        await self._run_submission_write_with_unique_retry(
            lambda state: state.project_submission_admission(record)
        )

    async def _run_submission_write_with_unique_retry(
        self,
        operation: Callable[[SQLiteStateRepository], _T],
    ) -> _T:
        try:
            return await self._run(lambda state, collab: operation(state))
        except IntegrityError as exc:
            if _postgres_sqlstate(exc) != "23505":
                raise
        return await self._run(lambda state, collab: operation(state))

    def _run_cp7_authority_sync(
        self,
        *,
        owner_user_id: str,
        operation: Callable[[SQLiteStateRepository], _T],
        server_id: str | None = None,
        intent_id: str | None = None,
        outbox_id: str | None = None,
        pending_action_id: str | None = None,
        branch_id: str | None = None,
        call_id: str | None = None,
        candidate_id: str | None = None,
        terminal_candidate: MCPValidatedTerminalResultCandidate | None = None,
        result_ref: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        interrupt_id: str | None = None,
        answer_id: str | None = None,
        grant_scope: tuple[str, int, str] | None = None,
    ) -> _T:
        """Run CP7 authority under its global PostgreSQL row-lock order."""

        with self._session_factory() as session:
            created_guard_owner = session.scalar(
                text(
                    "INSERT INTO user_mcp_owner_mutation_guard "
                    "(owner_user_id, revision, server_set_fingerprint, created_at, updated_at) "
                    "VALUES (:owner, 0, :fingerprint, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (owner_user_id) DO NOTHING "
                    "RETURNING owner_user_id"
                ),
                {
                    "owner": owner_user_id,
                    "fingerprint": canonical_sha256([]),
                },
            )
            owner_guard = session.scalar(
                select(UserMCPOwnerMutationGuardRow)
                .where(UserMCPOwnerMutationGuardRow.owner_user_id == owner_user_id)
                .with_for_update()
            )
            owner_servers: list[UserMCPServerRow] | None = None
            if server_id is not None and created_guard_owner is None:
                session.scalar(
                    select(UserMCPServerRow.server_id)
                    .where(
                        UserMCPServerRow.owner_user_id == owner_user_id,
                        UserMCPServerRow.server_id == server_id,
                    )
                    .with_for_update()
                )
            else:
                owner_servers = list(
                    session.scalars(
                        select(UserMCPServerRow)
                        .where(UserMCPServerRow.owner_user_id == owner_user_id)
                        .order_by(UserMCPServerRow.server_id)
                        .with_for_update()
                    ).all()
                )
            if created_guard_owner is not None:
                if owner_guard is None or owner_servers is None:
                    raise RuntimeError("user_mcp_owner_guard_create_failed")
                owner_guard.server_set_fingerprint = (
                    _mcp_owner_server_set_fingerprint(owner_servers)
                )
                owner_guard.revision = int(owner_guard.revision) + 1
                owner_guard.updated_at = datetime.now(timezone.utc)
                session.flush()
            if intent_id is not None:
                session.scalar(
                    select(MCPNoServerIntentRow.intent_id)
                    .where(MCPNoServerIntentRow.intent_id == intent_id)
                    .with_for_update()
                )
            if outbox_id is not None:
                session.scalar(
                    select(MCPDispatchResumeOutboxRow.outbox_id)
                    .where(MCPDispatchResumeOutboxRow.outbox_id == outbox_id)
                    .with_for_update()
                )
            if pending_action_id is not None:
                session.scalar(
                    select(MCPPendingToolActionRow.action_id)
                    .where(MCPPendingToolActionRow.action_id == pending_action_id)
                    .with_for_update()
                )
            if branch_id is not None:
                session.scalar(
                    select(MCPBranchRecordRow.branch_id)
                    .where(MCPBranchRecordRow.branch_id == branch_id)
                    .with_for_update()
                )
            sealed_candidate = terminal_candidate
            if call_id is not None:
                session.scalar(
                    select(MCPCallRecordRow.call_ref)
                    .where(MCPCallRecordRow.call_ref == call_id)
                    .with_for_update()
                )
                if candidate_id is not None and sealed_candidate is None:
                    if self._mcp_terminal_candidate_reader is None:
                        raise RuntimeError("mcp_terminal_candidate_reader_unavailable")
                    sealed_candidate = self._mcp_terminal_candidate_reader(
                        call_id, candidate_id
                    )
                session.scalar(
                    select(MCPTerminalResultReceiptRow.result_receipt_id)
                    .where(MCPTerminalResultReceiptRow.call_id == call_id)
                    .with_for_update()
                )
                session.scalar(
                    select(MCPExecutionTerminalProjectionRow.projection_id)
                    .where(MCPExecutionTerminalProjectionRow.call_id == call_id)
                    .with_for_update()
                )
                if candidate_id is not None:
                    session.scalar(
                        select(MCPTerminalCandidateLifecycleRow.candidate_id)
                        .where(
                            MCPTerminalCandidateLifecycleRow.candidate_id
                            == candidate_id
                        )
                        .with_for_update()
                    )
                if result_ref is not None:
                    session.scalar(
                        select(MCPDurableResultLifecycleRow.result_ref)
                        .where(
                            MCPDurableResultLifecycleRow.result_ref == result_ref
                        )
                        .with_for_update()
                    )
            if task_id is not None:
                session.scalar(
                    select(TaskRow.task_id)
                    .where(TaskRow.task_id == task_id)
                    .with_for_update()
                )
            if node_id is not None:
                session.scalar(
                    select(TaskNodeRow.node_id)
                    .where(TaskNodeRow.node_id == node_id)
                    .with_for_update()
                )
            if interrupt_id is not None:
                session.scalar(
                    select(InterruptRow.interrupt_id)
                    .where(InterruptRow.interrupt_id == interrupt_id)
                    .with_for_update()
                )
            if answer_id is not None:
                session.scalar(
                    select(InterruptAnswerRow.interrupt_answer_id)
                    .where(InterruptAnswerRow.interrupt_answer_id == answer_id)
                    .with_for_update()
                )
            if grant_scope is not None and server_id is not None:
                tool_name, security_version, schema_sha256 = grant_scope
                session.scalar(
                    select(UserMCPToolGrantRow.grant_id)
                    .where(
                        UserMCPToolGrantRow.owner_user_id == owner_user_id,
                        UserMCPToolGrantRow.server_id == server_id,
                        UserMCPToolGrantRow.tool_name == tool_name,
                        UserMCPToolGrantRow.server_security_version
                        == security_version,
                        UserMCPToolGrantRow.input_schema_sha256 == schema_sha256,
                    )
                    .with_for_update()
                )
            result = operation(
                SQLiteStateRepository(
                    session,
                    task_authority_mode=self._mcp_task_authority_mode,
                    terminal_candidate_reader=(
                        self._mcp_terminal_candidate_reader
                        if sealed_candidate is None
                        else lambda _call_id, _candidate_id: sealed_candidate
                    ),
                    terminal_candidate_resolver=self._mcp_terminal_candidate_resolver,
                    pending_action_payload_reader=(
                        self._mcp_pending_action_payload_reader
                    ),
                    terminal_candidate_snapshot_reader=(
                        self._mcp_terminal_candidate_snapshot_reader
                    ),
                    durable_result_snapshot_reader=(
                        self._mcp_durable_result_snapshot_reader
                    ),
                    mrtr_request_state_evidence_reader=(
                        self._mcp_mrtr_request_state_evidence_reader
                    ),
                )
            )
            session.commit()
            return result

    async def create_user_mcp_initial_intent(
        self, task: Task, occurred_at: datetime
    ) -> MCPInitialIntentCreateResult:
        def _sync() -> MCPInitialIntentCreateResult:
            with self._session_factory() as session:
                owner = session.scalar(
                    select(ConversationRow.username).where(
                        ConversationRow.conversation_id == task.conversation_id
                    )
                )
            if owner is None:
                raise ValueError("mcp_no_server_task_conversation_missing")
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                task_id=task.task_id,
                operation=lambda state: state.create_user_mcp_initial_intent(
                    task, occurred_at
                ),
            )

        return await asyncio.to_thread(_sync)

    async def create_user_mcp_server(
        self,
        server: UserMCPServer,
        credential: UserMCPCredentialRecord | None = None,
    ) -> UserMCPServer:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=server.owner_user_id,
            server_id=server.server_id,
            operation=lambda state: state.create_user_mcp_server(server, credential),
        )

    async def create_user_mcp_servers_atomic(
        self,
        candidates: Sequence[tuple[UserMCPServer, UserMCPCredentialRecord | None]],
    ) -> list[UserMCPServer]:
        batch = tuple(candidates)

        def _sync() -> list[UserMCPServer]:
            owners = sorted({server.owner_user_id for server, _credential in batch})
            with self._session_factory() as session:
                for owner_user_id in owners:
                    session.execute(
                        text(
                            "INSERT INTO user_mcp_owner_mutation_guard "
                            "(owner_user_id, revision, server_set_fingerprint, created_at, updated_at) "
                            "VALUES (:owner, 0, :fingerprint, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                            "ON CONFLICT (owner_user_id) DO NOTHING"
                        ),
                        {
                            "owner": owner_user_id,
                            "fingerprint": canonical_sha256([]),
                        },
                    )
                session.scalars(
                    select(UserMCPOwnerMutationGuardRow.owner_user_id)
                    .where(UserMCPOwnerMutationGuardRow.owner_user_id.in_(owners))
                    .order_by(UserMCPOwnerMutationGuardRow.owner_user_id)
                    .with_for_update()
                ).all()
                session.scalars(
                    select(UserMCPServerRow.server_id)
                    .where(UserMCPServerRow.owner_user_id.in_(owners))
                    .order_by(UserMCPServerRow.owner_user_id, UserMCPServerRow.server_id)
                    .with_for_update()
                ).all()
                result = SQLiteStateRepository(session).create_user_mcp_servers_atomic(batch)
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    async def arm_user_mcp_target_intent(
        self,
        task_id: str,
        node_id: str,
        requested_server_id: str,
        resume_envelope: Mapping[str, Any],
        occurred_at: datetime,
    ) -> MCPTargetIntentArmResult:
        def _sync() -> MCPTargetIntentArmResult:
            with self._session_factory() as session:
                owner = session.scalar(
                    select(ConversationRow.username)
                    .join(TaskRow, TaskRow.conversation_id == ConversationRow.conversation_id)
                    .where(TaskRow.task_id == task_id)
                )
            if owner is None:
                raise ValueError("mcp_target_intent_task_missing")
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=requested_server_id,
                task_id=task_id,
                node_id=node_id,
                operation=lambda state: state.arm_user_mcp_target_intent(
                    task_id,
                    node_id,
                    requested_server_id,
                    resume_envelope,
                    occurred_at,
                ),
            )

        return await asyncio.to_thread(_sync)

    async def resolve_user_mcp_target_intent(
        self, intent_id: str, occurred_at: datetime
    ) -> MCPTargetIntentResolveResult:
        def _sync() -> MCPTargetIntentResolveResult:
            with self._session_factory() as session:
                intent = session.get(MCPNoServerIntentRow, intent_id)
                if intent is None:
                    raise ValueError("mcp_target_intent_missing")
                owner, server_id, task_id, node_id = (
                    intent.owner_user_id,
                    intent.requested_server_id,
                    intent.task_id,
                    intent.node_id,
                )
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server_id,
                intent_id=intent_id,
                outbox_id=f"mcp-dispatch-resume:v1:{intent_id}",
                task_id=task_id,
                node_id=node_id,
                operation=lambda state: state.resolve_user_mcp_target_intent(
                    intent_id, occurred_at
                ),
            )

        return await asyncio.to_thread(_sync)

    def _cp7_outbox_lock_subject(
        self, outbox_id: str
    ) -> tuple[str, str, str, str, str]:
        with self._session_factory() as session:
            row = session.get(MCPDispatchResumeOutboxRow, outbox_id)
            if row is None:
                raise ValueError("mcp_dispatch_resume_outbox_missing")
            return (
                row.owner_user_id,
                row.server_id,
                row.intent_id,
                row.task_id,
                row.node_id,
            )

    async def claim_mcp_dispatch_resume_outbox(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(outbox_id)
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.claim_mcp_dispatch_resume_outbox(
                    outbox_id, claim_owner, claim_token, now, lease_expires_at
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def reclaim_mcp_dispatch_resume_outbox(
        self, outbox_id: str, expected_revision: int, now: datetime
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(outbox_id)
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.reclaim_mcp_dispatch_resume_outbox(
                    outbox_id, expected_revision, now
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def abort_mcp_dispatch_resume_outbox(
        self, outbox_id: str, expected_revision: int, occurred_at: datetime
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(outbox_id)
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.abort_mcp_dispatch_resume_outbox(
                    outbox_id, expected_revision, occurred_at
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def claim_mcp_dispatch(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(
                outbox_id
            )
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.claim_mcp_dispatch(
                    outbox_id,
                    claim_owner,
                    claim_token,
                    expected_revision,
                    now,
                    lease_expires_at,
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def renew_mcp_dispatch_claim(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(
                outbox_id
            )
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.renew_mcp_dispatch_claim(
                    outbox_id,
                    claim_owner,
                    claim_token,
                    expected_revision,
                    now,
                    lease_expires_at,
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def consume_mcp_dispatch_selector_step(
        self,
        outbox_id: str,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        occurred_at: datetime,
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(
                outbox_id
            )
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.consume_mcp_dispatch_selector_step(
                    outbox_id,
                    claim_owner,
                    claim_token,
                    expected_revision,
                    occurred_at,
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def release_or_recover_mcp_dispatch_claim(
        self,
        outbox_id: str,
        expected_revision: int,
        now: datetime,
    ) -> MCPDispatchResumeOutbox | None:
        def _sync() -> MCPDispatchResumeOutbox | None:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(
                outbox_id
            )
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.release_or_recover_mcp_dispatch_claim(
                    outbox_id, expected_revision, now
                ),
            )

        try:
            return await asyncio.to_thread(_sync)
        except ValueError as exc:
            if str(exc) == "mcp_dispatch_resume_outbox_missing":
                return None
            raise

    async def suspend_mcp_for_approval(
        self,
        intent_id: str,
        outbox_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        action: MCPPendingToolAction,
        interrupt: Interrupt,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        occurred_at: datetime,
    ) -> MCPApprovalSuspendResult:
        with self._session_factory() as session:
            branch = session.scalar(
                select(MCPBranchRecordRow).where(
                    MCPBranchRecordRow.owner_user_id == action.owner_user_id,
                    MCPBranchRecordRow.task_id == action.task_id,
                    MCPBranchRecordRow.node_id == action.node_id,
                )
            )
            branch_id = None if branch is None else branch.branch_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=action.owner_user_id,
            server_id=action.server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            pending_action_id=action.action_id,
            branch_id=branch_id,
            task_id=action.task_id,
            node_id=action.node_id,
            interrupt_id=interrupt.interrupt_id,
            grant_scope=(
                action.tool_name,
                action.server_security_version,
                action.input_schema_sha256,
            ),
            operation=lambda state: state.suspend_mcp_for_approval(
                intent_id,
                outbox_id,
                expected_intent_revision,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                action,
                interrupt,
                payload_snapshot,
                occurred_at,
            ),
        )

    async def accept_mcp_tool_approval(
        self,
        interrupt_id: str,
        answer: InterruptAnswer,
        decision: str,
        occurred_at: datetime,
    ) -> MCPApprovalDecisionResult:
        with self._session_factory() as session:
            action = session.scalar(
                select(MCPPendingToolActionRow).where(
                    MCPPendingToolActionRow.approval_interrupt_id == interrupt_id
                )
            )
            if action is None:
                return MCPApprovalDecisionResult.CONFLICT
            branch = session.scalar(
                select(MCPBranchRecordRow).where(
                    MCPBranchRecordRow.owner_user_id == action.owner_user_id,
                    MCPBranchRecordRow.task_id == action.task_id,
                    MCPBranchRecordRow.node_id == action.node_id,
                )
            )
            values = (
                action.owner_user_id,
                action.server_id,
                mcp_no_server_intent_id(action.task_id, node_id=action.node_id),
                action.action_id,
                action.task_id,
                action.node_id,
                action.tool_name,
                int(action.server_security_version),
                action.input_schema_sha256,
                None if branch is None else branch.branch_id,
            )
        (
            owner_user_id,
            server_id,
            intent_id,
            action_id,
            task_id,
            node_id,
            tool_name,
            security_version,
            schema_sha256,
            branch_id,
        ) = values
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            intent_id=intent_id,
            outbox_id=mcp_dispatch_resume_outbox_id(intent_id),
            pending_action_id=action_id,
            branch_id=branch_id,
            task_id=task_id,
            node_id=node_id,
            interrupt_id=interrupt_id,
            answer_id=answer.interrupt_answer_id,
            grant_scope=(tool_name, security_version, schema_sha256),
            operation=lambda state: state.accept_mcp_tool_approval(
                interrupt_id, answer, decision, occurred_at
            ),
        )

    async def suspend_mcp_for_input(
        self,
        intent_id: str,
        outbox_id: str,
        call_id: str,
        sealed_state_ref: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        interrupt: Interrupt,
        occurred_at: datetime,
    ) -> MCPInputSuspendResult:
        with self._session_factory() as session:
            call = session.get(MCPCallRecordRow, call_id)
            if call is None:
                return MCPInputSuspendResult.CONFLICT
            values = (
                call.owner_user_id,
                call.server_id,
                call.pending_action_id,
                call.branch_id,
                call.task_id,
                call.node_id,
            )
        owner_user_id, server_id, action_id, branch_id, task_id, node_id = values
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            pending_action_id=action_id,
            branch_id=branch_id,
            call_id=call_id,
            task_id=task_id,
            node_id=node_id,
            interrupt_id=interrupt.interrupt_id,
            operation=lambda state: state.suspend_mcp_for_input(
                intent_id,
                outbox_id,
                call_id,
                sealed_state_ref,
                expected_intent_revision,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                interrupt,
                occurred_at,
            ),
        )

    async def accept_mcp_mrtr_answer(
        self,
        interrupt_id: str,
        answer: InterruptAnswer,
        occurred_at: datetime,
    ) -> MCPMRTRAnswerResult:
        with self._session_factory() as session:
            interrupt = session.get(InterruptRow, interrupt_id)
            sealed_ref = (
                ""
                if interrupt is None
                else str(
                    (interrupt.required_fields or {}).get(
                        "sealed_request_state_ref"
                    )
                    or ""
                )
            )
            sealed = session.get(MCPSealedStateRow, sealed_ref)
            call = (
                None
                if sealed is None
                else session.get(MCPCallRecordRow, sealed.call_ref)
            )
            if interrupt is None or call is None:
                return MCPMRTRAnswerResult.CONFLICT
            values = (
                call.owner_user_id,
                call.server_id,
                mcp_no_server_intent_id(call.task_id, node_id=call.node_id),
                call.pending_action_id,
                call.branch_id,
                call.call_ref,
                call.task_id,
                call.node_id,
            )
        (
            owner_user_id,
            server_id,
            intent_id,
            action_id,
            branch_id,
            call_id,
            task_id,
            node_id,
        ) = values
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            intent_id=intent_id,
            outbox_id=mcp_dispatch_resume_outbox_id(intent_id),
            pending_action_id=action_id,
            branch_id=branch_id,
            call_id=call_id,
            task_id=task_id,
            node_id=node_id,
            interrupt_id=interrupt_id,
            answer_id=answer.interrupt_answer_id,
            operation=lambda state: state.accept_mcp_mrtr_answer(
                interrupt_id, answer, occurred_at
            ),
        )

    async def admit_mcp_tool_call(
        self,
        intent_id: str,
        outbox_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=record.owner_user_id,
            server_id=record.server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            call_id=record.call_ref,
            task_id=record.task_id,
            node_id=record.node_id,
            operation=lambda state: state.admit_mcp_tool_call(
                intent_id,
                outbox_id,
                expected_intent_revision,
                expected_outbox_revision,
                record,
                occurred_at,
                cp7_candidate_id=cp7_candidate_id,
                cp7_epoch_id=cp7_epoch_id,
            ),
        )

    async def admit_approved_mcp_action(
        self,
        intent_id: str,
        outbox_id: str,
        action_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        expected_action_revision: int,
        claim_owner: str,
        claim_token: str,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        action_candidate: MCPPendingToolAction | None = None,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=record.owner_user_id,
            server_id=record.server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            pending_action_id=action_id,
            branch_id=record.branch_id,
            call_id=record.call_ref,
            task_id=record.task_id,
            node_id=record.node_id,
            operation=lambda state: state.admit_approved_mcp_action(
                intent_id,
                outbox_id,
                action_id,
                expected_intent_revision,
                expected_outbox_revision,
                expected_action_revision,
                claim_owner,
                claim_token,
                payload_snapshot,
                record,
                occurred_at,
                action_candidate=action_candidate,
                cp7_candidate_id=cp7_candidate_id,
                cp7_epoch_id=cp7_epoch_id,
            ),
        )

    async def admit_mrtr_continuation(
        self,
        intent_id: str,
        outbox_id: str,
        original_call_id: str,
        sealed_state_ref: str,
        answer_id: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        payload_snapshot: MCPPendingActionPayloadSnapshot,
        record: MCPCallRecord,
        occurred_at: datetime,
        *,
        cp7_candidate_id: str | None = None,
        cp7_epoch_id: str | None = None,
    ) -> bool:
        with self._session_factory() as session:
            original = session.get(MCPCallRecordRow, original_call_id)
            if original is None:
                return False
            action_id = original.pending_action_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=record.owner_user_id,
            server_id=record.server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            pending_action_id=action_id,
            branch_id=record.branch_id,
            call_id=original_call_id,
            task_id=record.task_id,
            node_id=record.node_id,
            answer_id=answer_id,
            operation=lambda state: state.admit_mrtr_continuation(
                intent_id,
                outbox_id,
                original_call_id,
                sealed_state_ref,
                answer_id,
                expected_intent_revision,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                payload_snapshot,
                record,
                occurred_at,
                cp7_candidate_id=cp7_candidate_id,
                cp7_epoch_id=cp7_epoch_id,
            ),
        )

    async def publish_mcp_remote_task(
        self,
        intent_id: str,
        outbox_id: str,
        call_id: str,
        safe_remote_task_ref: str,
        expected_intent_revision: int,
        expected_outbox_revision: int,
        claim_owner: str,
        claim_token: str,
        occurred_at: datetime,
    ) -> MCPRemoteTaskBinding | None:
        with self._session_factory() as session:
            call = session.get(MCPCallRecordRow, call_id)
            if call is None:
                return None
            values = (
                call.owner_user_id,
                call.server_id,
                call.pending_action_id,
                call.branch_id,
                call.task_id,
                call.node_id,
            )
        owner_user_id, server_id, action_id, branch_id, task_id, node_id = values
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            intent_id=intent_id,
            outbox_id=outbox_id,
            pending_action_id=action_id,
            branch_id=branch_id,
            call_id=call_id,
            task_id=task_id,
            node_id=node_id,
            operation=lambda state: state.publish_mcp_remote_task(
                intent_id,
                outbox_id,
                call_id,
                safe_remote_task_ref,
                expected_intent_revision,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                occurred_at,
            ),
        )

    async def finalize_mcp_dispatch_no_call(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        outcome: str,
        safe_error_code: str | None,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult:
        def _sync() -> MCPDispatchFinalizeResult:
            owner, server, intent, task, node = self._cp7_outbox_lock_subject(outbox_id)
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent,
                outbox_id=outbox_id,
                task_id=task,
                node_id=node,
                operation=lambda state: state.finalize_mcp_dispatch_no_call(
                    intent_id, outbox_id, node_id, outcome, safe_error_code, occurred_at
                ),
            )
        return await asyncio.to_thread(_sync)

    async def commit_mcp_call_terminal(
        self,
        call_id: str,
        candidate_id: str,
        outbox_id: str,
        expected_outbox_revision: int,
        claim_owner: str | None,
        claim_token: str | None,
        candidate_snapshot: MCPTerminalCandidateSnapshot,
        result_snapshot: MCPDurableResultSnapshot | None,
        occurred_at: datetime,
        *,
        remote_binding_ref: str | None = None,
        remote_claim_owner: str | None = None,
        remote_claim_token: str | None = None,
        remote_expected_revision: int | None = None,
    ) -> MCPTerminalResultCommitResult:
        candidate = candidate_snapshot.candidate
        with self._session_factory() as session:
            call = session.get(MCPCallRecordRow, call_id)
            branch_id = None if call is None else call.branch_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=candidate.owner_user_id,
            server_id=candidate.server_id,
            intent_id=candidate.intent_id,
            outbox_id=outbox_id,
            branch_id=branch_id,
            call_id=call_id,
            candidate_id=candidate_id,
            terminal_candidate=candidate,
            result_ref=(
                None if result_snapshot is None else result_snapshot.result_ref
            ),
            task_id=candidate.task_id,
            node_id=candidate.node_id,
            operation=lambda state: state.commit_mcp_call_terminal(
                call_id,
                candidate_id,
                outbox_id,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                candidate_snapshot,
                result_snapshot,
                occurred_at,
                remote_binding_ref=remote_binding_ref,
                remote_claim_owner=remote_claim_owner,
                remote_claim_token=remote_claim_token,
                remote_expected_revision=remote_expected_revision,
            ),
        )

    async def recover_mcp_terminal_candidate(
        self,
        candidate_snapshot: MCPTerminalCandidateSnapshot,
        result_snapshot: MCPDurableResultSnapshot | None,
        occurred_at: datetime,
    ) -> MCPTerminalResultCommitResult:
        candidate = candidate_snapshot.candidate
        with self._session_factory() as session:
            call = session.get(MCPCallRecordRow, candidate.call_id)
            branch_id = None if call is None else call.branch_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=candidate.owner_user_id,
            server_id=candidate.server_id,
            intent_id=candidate.intent_id,
            outbox_id=mcp_dispatch_resume_outbox_id(candidate.intent_id),
            branch_id=branch_id,
            call_id=candidate.call_id,
            candidate_id=candidate.candidate_id,
            terminal_candidate=candidate,
            result_ref=(
                None if result_snapshot is None else result_snapshot.result_ref
            ),
            task_id=candidate.task_id,
            node_id=candidate.node_id,
            operation=lambda state: state.recover_mcp_terminal_candidate(
                candidate_snapshot, result_snapshot, occurred_at
            ),
        )

    async def finalize_mcp_dispatch(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        outcome: str,
        safe_error_code: str | None,
        expected_outbox_revision: int,
        claim_owner: str | None,
        claim_token: str | None,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult:
        owner, server, intent, task, node = self._cp7_outbox_lock_subject(
            outbox_id
        )
        with self._session_factory() as session:
            branch = session.scalar(
                select(MCPBranchRecordRow).where(
                    MCPBranchRecordRow.task_id == task,
                    MCPBranchRecordRow.node_id == node,
                )
            )
            branch_id = None if branch is None else branch.branch_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner,
            server_id=server,
            intent_id=intent,
            outbox_id=outbox_id,
            branch_id=branch_id,
            task_id=task,
            node_id=node,
            operation=lambda state: state.finalize_mcp_dispatch(
                intent_id,
                outbox_id,
                node_id,
                outcome,
                safe_error_code,
                expected_outbox_revision,
                claim_owner,
                claim_token,
                occurred_at,
            ),
        )

    async def converge_mcp_unknown_no_replay(
        self, task_id: str, occurred_at: datetime
    ) -> MCPNoServerConvergenceResult:
        return await self.converge_user_mcp_no_server(task_id, occurred_at)

    async def cancel_mcp_dispatch(
        self,
        intent_id: str,
        outbox_id: str,
        node_id: str,
        occurred_at: datetime,
    ) -> MCPDispatchFinalizeResult:
        owner, server, intent, task, node = self._cp7_outbox_lock_subject(
            outbox_id
        )
        with self._session_factory() as session:
            branch = session.scalar(
                select(MCPBranchRecordRow).where(
                    MCPBranchRecordRow.task_id == task,
                    MCPBranchRecordRow.node_id == node,
                )
            )
            branch_id = None if branch is None else branch.branch_id
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner,
            server_id=server,
            intent_id=intent,
            outbox_id=outbox_id,
            branch_id=branch_id,
            task_id=task,
            node_id=node,
            operation=lambda state: state.cancel_mcp_dispatch(
                intent_id, outbox_id, node_id, occurred_at
            ),
        )

    async def converge_user_mcp_no_server(
        self, task_id: str, occurred_at: datetime
    ) -> MCPNoServerConvergenceResult:
        def _sync() -> MCPNoServerConvergenceResult:
            with self._session_factory() as session:
                intent = session.scalar(
                    select(MCPNoServerIntentRow)
                    .where(
                        MCPNoServerIntentRow.task_id == task_id,
                        MCPNoServerIntentRow.status.in_(("unavailable", "dispatched", "converged")),
                    )
                    .order_by(MCPNoServerIntentRow.intent_id)
                )
                if intent is None:
                    raise ValueError("mcp_no_server_intent_missing")
                outbox = session.scalar(
                    select(MCPDispatchResumeOutboxRow).where(
                        MCPDispatchResumeOutboxRow.intent_id == intent.intent_id
                    )
                )
                subject = (
                    intent.owner_user_id,
                    intent.requested_server_id,
                    intent.intent_id,
                    None if outbox is None else outbox.outbox_id,
                    intent.node_id,
                )
            owner, server, intent_id, outbox_id, node_id = subject
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                server_id=server,
                intent_id=intent_id,
                outbox_id=outbox_id,
                task_id=task_id,
                node_id=node_id,
                operation=lambda state: state.converge_user_mcp_no_server(
                    task_id, occurred_at
                ),
            )

        return await asyncio.to_thread(_sync)

    async def get_mcp_no_server_convergence_receipt(
        self, task_id: str
    ) -> MCPNoServerConvergenceReceipt | None:
        def _sync() -> MCPNoServerConvergenceReceipt | None:
            with self._session_factory() as session:
                return SQLiteStateRepository(
                    session,
                    task_authority_mode=self._mcp_task_authority_mode,
                    terminal_candidate_reader=self._mcp_terminal_candidate_reader,
                    terminal_candidate_resolver=self._mcp_terminal_candidate_resolver,
                ).get_mcp_no_server_convergence_receipt(task_id)
        return await asyncio.to_thread(_sync)

    async def commit_authoritative_mcp_terminal_result(
        self, call_id: str, candidate_id: str, occurred_at: datetime
    ):
        if self._mcp_terminal_candidate_reader is None:
            raise RuntimeError("mcp_terminal_candidate_reader_unavailable")
        candidate = await asyncio.to_thread(
            self._mcp_terminal_candidate_reader, call_id, candidate_id
        )
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=candidate.owner_user_id,
            server_id=candidate.server_id,
            intent_id=candidate.intent_id,
            outbox_id=f"mcp-dispatch-resume:v1:{candidate.intent_id}",
            call_id=call_id,
            candidate_id=candidate_id,
            task_id=candidate.task_id,
            node_id=candidate.node_id,
            operation=lambda state: state.commit_authoritative_mcp_terminal_result(
                call_id, candidate_id, occurred_at
            ),
        )

    async def append_mcp_legacy_retirement_evidence(
        self, evidence: MCPLegacyRetirementEvidence
    ) -> MCPLegacyRetirementEvidence:
        def _sync() -> MCPLegacyRetirementEvidence:
            with self._session_factory() as session:
                owner = session.scalar(
                    select(ConversationRow.username)
                    .join(TaskRow, TaskRow.conversation_id == ConversationRow.conversation_id)
                    .where(TaskRow.task_id == evidence.task_id)
                )
            if owner is None:
                raise ValueError("mcp_legacy_retirement_task_missing")
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                task_id=evidence.task_id,
                operation=lambda state: state.append_mcp_legacy_retirement_evidence(
                    evidence
                ),
            )

        return await asyncio.to_thread(_sync)

    async def converge_legacy_runtime_retirement(
        self,
        task_id: str,
        inventory_id: str,
        inventory_sha256: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> MCPLegacyRetirementConvergenceResult:
        def _sync() -> MCPLegacyRetirementConvergenceResult:
            with self._session_factory() as session:
                owner = session.scalar(
                    select(ConversationRow.username)
                    .join(TaskRow, TaskRow.conversation_id == ConversationRow.conversation_id)
                    .where(TaskRow.task_id == task_id)
                )
            if owner is None:
                raise ValueError("mcp_legacy_retirement_task_missing")
            return self._run_cp7_authority_sync(
                owner_user_id=owner,
                task_id=task_id,
                operation=lambda state: state.converge_legacy_runtime_retirement(
                    task_id,
                    inventory_id,
                    inventory_sha256,
                    idempotency_key,
                    occurred_at,
                ),
            )

        return await asyncio.to_thread(_sync)

    async def get_legacy_mcp_migration_replay_snapshot(
        self,
        *,
        migration_id: str,
        plan_fingerprint: str,
        source_server_id: str,
        source_fingerprint: str,
        owner_consumer_ref: str,
        target_server_id: str,
    ) -> Mapping[str, Any] | None:
        migration_session_factory = self._mcp_legacy_migration_session_factory
        if migration_session_factory is None or not self._mcp_legacy_migration_role:
            raise RuntimeError(
                "dedicated MCP legacy migration PostgreSQL session is required"
            )
        statement = text(
            "SELECT mcp_migration_api.read_legacy_migration_replay_snapshot("
            ":migration_id, :plan_fingerprint, :source_server_id, "
            ":source_fingerprint, :owner_consumer_ref, :target_server_id)"
        )

        def _sync() -> Mapping[str, Any] | None:
            with migration_session_factory() as session:
                value = session.scalar(
                    statement,
                    {
                        "migration_id": migration_id,
                        "plan_fingerprint": plan_fingerprint,
                        "source_server_id": source_server_id,
                        "source_fingerprint": source_fingerprint,
                        "owner_consumer_ref": owner_consumer_ref,
                        "target_server_id": target_server_id,
                    },
                )
                if value is None:
                    return None
                if not isinstance(value, Mapping):
                    raise RuntimeError(
                        "legacy MCP migration replay API returned an invalid result"
                    )
                return dict(value)

        return await asyncio.to_thread(_sync)

    async def apply_legacy_mcp_migration_atomic(
        self,
        candidates: Sequence[
            tuple[
                UserMCPServer,
                UserMCPCredentialRecord | None,
                MCPLegacyMigrationRecord,
            ],
        ],
    ) -> MCPLegacyMigrationBatchResult:
        migration_session_factory = self._mcp_legacy_migration_session_factory
        if migration_session_factory is None or not self._mcp_legacy_migration_role:
            raise RuntimeError(
                "dedicated MCP legacy migration PostgreSQL session is required"
            )
        batch = tuple(candidates)
        migration_ids: set[str] = set()
        plan_sources: set[tuple[str, str]] = set()
        target_server_ids: set[str] = set()
        for server, credential, record in batch:
            _validate_mcp_legacy_migration_record(record)
            if record.target_server_id != server.server_id:
                raise ValueError("migration record target does not match MCP server")
            if credential is not None and (
                credential.owner_user_id != server.owner_user_id
                or credential.server_id != server.server_id
            ):
                raise ValueError("credential scope does not match MCP server")
            if server.deletion_pending or server.deleted_at is not None:
                raise ValueError("migration MCP server deletion state is invalid")
            plan_source = (record.plan_fingerprint, record.source_server_id)
            if (
                record.migration_id in migration_ids
                or plan_source in plan_sources
                or record.target_server_id in target_server_ids
            ):
                raise ValueError("duplicate legacy MCP migration candidate")
            migration_ids.add(record.migration_id)
            plan_sources.add(plan_source)
            target_server_ids.add(record.target_server_id)

        ordered_batch = tuple(
            sorted(
                batch,
                key=lambda candidate: (
                    candidate[2].migration_id,
                    candidate[2].plan_fingerprint,
                    candidate[2].source_server_id,
                    candidate[2].target_server_id,
                ),
            )
        )
        lock_identities = tuple(
            sorted(
                {
                    identity
                    for _server, _credential, record in ordered_batch
                    for identity in (
                        f"migration:{record.migration_id}",
                        "plan_source:"
                        f"{record.plan_fingerprint}:{record.source_server_id}",
                        f"target:{record.target_server_id}",
                    )
                }
            )
        )
        lock_statement = text(
            "SELECT mcp_migration_api.lock_legacy_migration_batch("
            "CAST(:lock_identities AS text[]))"
        )
        statement = text(
            """
            SELECT mcp_migration_api.apply_legacy_migration_candidate(
                :server_id, :owner_user_id, :display_name,
                :routing_description, :endpoint_url, :transport,
                :protocol_preference, :auth_type, CAST(:auth_metadata AS jsonb),
                :enabled, :health_status, :config_version, :security_version,
                :credential_ciphertext, :credential_nonce, :encryption_version,
                :credential_updated_at, :last_tested_at, :last_test_error_code,
                :created_at, :updated_at, :migration_id, :plan_fingerprint,
                :source_server_id, :source_fingerprint, :owner_consumer_ref,
                :target_server_id, :target_consumer_set_digest,
                :capability_obligations_fingerprint, :catalog_fingerprint,
                :capability_fingerprint, :validator_provenance_fingerprint,
                :credential_digest, :occurred_at, :evidence_expires_at
            )
            """
        )

        def _sync() -> MCPLegacyMigrationBatchResult:
            applied = False
            with migration_session_factory() as session:
                with session.begin():
                    if lock_identities:
                        session.execute(
                            lock_statement,
                            {"lock_identities": list(lock_identities)},
                        )
                    for server, credential, record in ordered_batch:
                        parameters: dict[str, object | None] = {
                            **_user_mcp_server_insert_values(
                                server, credential
                            ),
                            **_mcp_legacy_migration_record_values(
                                record
                            ),
                        }
                        parameters["auth_metadata"] = json.dumps(
                            parameters["auth_metadata"],
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        candidate_applied = session.scalar(statement, parameters)
                        if not isinstance(candidate_applied, bool):
                            raise RuntimeError(
                                "legacy MCP migration API returned an invalid result"
                            )
                        applied = applied or candidate_applied
            return MCPLegacyMigrationBatchResult(
                servers=tuple(
                    replace(
                        server,
                        credential_configured=credential is not None,
                        deletion_pending=False,
                        deleted_at=None,
                    )
                    for server, credential, _record in batch
                ),
                records=tuple(record for _server, _credential, record in batch),
                applied=applied,
            )

        try:
            return await asyncio.to_thread(_sync)
        except DBAPIError as exc:
            database_message = str(exc.orig)
            if (
                "legacy MCP migration" in database_message
                and "conflict" in database_message
            ):
                raise ValueError(database_message.splitlines()[0]) from None
            raise

    async def _run_mcp_rollout_function(
        self,
        statement: str,
        parameters: Mapping[str, object],
        converter: Callable[[Any], _T],
    ) -> _T:
        def _sync() -> _T:
            with self._mcp_rollout_session_factory() as session:
                row = session.execute(text(statement), parameters).mappings().one()
                session.commit()
                return converter(SimpleNamespace(**row))

        return await asyncio.to_thread(_sync)

    @staticmethod
    def _metric_parameters(bucket: MCPRolloutMetricBucket) -> dict[str, object]:
        return {
            "metric_bucket_id": bucket.metric_bucket_id,
            "environment_id": bucket.environment_id,
            "deployment_id": bucket.deployment_id,
            "stage": bucket.stage,
            "config_fingerprint": bucket.config_fingerprint,
            "metric_name": bucket.metric_name,
            "bucket_started_at": bucket.bucket_started_at,
            "bucket_ended_at": bucket.bucket_ended_at,
            "execution_path": bucket.execution_path,
            "routing_mode": bucket.routing_mode,
            "transport": bucket.transport,
            "protocol_version": bucket.protocol_version,
            "adapter": bucket.adapter,
            "result_category": bucket.result_category,
            "error_category": bucket.error_category,
            "call_kind": bucket.call_kind or "not_applicable",
            "red_line": bucket.red_line or "not_applicable",
            "latency_bucket": bucket.latency_bucket,
            "value": bucket.value,
            "recorded_at": bucket.updated_at or bucket.created_at or datetime.now().astimezone(),
        }

    async def upsert_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return await self._write_mcp_rollout_metric_bucket_via_api(bucket, additive=True)

    async def set_mcp_rollout_metric_bucket(
        self, bucket: MCPRolloutMetricBucket
    ) -> MCPRolloutMetricBucket:
        return await self._write_mcp_rollout_metric_bucket_via_api(bucket, additive=False)

    async def _write_mcp_rollout_metric_bucket_via_api(
        self, bucket: MCPRolloutMetricBucket, *, additive: bool
    ) -> MCPRolloutMetricBucket:
        if not is_exact_mcp_metric_bucket_window(
            bucket.bucket_started_at, bucket.bucket_ended_at
        ):
            raise ValueError(
                "MCP rollout metric bucket must be one complete UTC-aligned minute"
            )
        function_name = "upsert_metric_bucket" if additive else "set_metric_bucket"
        return await self._run_mcp_rollout_function(
            f"""
            SELECT result.*
            FROM mcp_rollout_api.{function_name}(
                :metric_bucket_id, :environment_id, :deployment_id, :stage,
                :config_fingerprint, :metric_name, :bucket_started_at,
                :bucket_ended_at, :execution_path, :routing_mode, :transport,
                :protocol_version, :adapter, :result_category, :error_category,
                :call_kind, :red_line, :latency_bucket, :value, :recorded_at
            ) AS result
            """,
            self._metric_parameters(bucket),
            _row_to_mcp_rollout_metric_bucket,
        )

    async def _run_with_user_mcp_server_lock(
        self,
        owner_user_id: str,
        server_id: str,
        callback: Callable[[SQLiteStateRepository], _T],
    ) -> _T:
        def _sync() -> _T:
            with self._session_factory() as session:
                session.scalar(
                    select(UserMCPServerRow.server_id)
                    .where(
                        UserMCPServerRow.owner_user_id == owner_user_id,
                        UserMCPServerRow.server_id == server_id,
                    )
                    .with_for_update()
                )
                result = callback(SQLiteStateRepository(session))
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    async def ensure_mcp_rollout_gate_scope(
        self, scope: MCPRolloutGateScope
    ) -> MCPRolloutGateScope:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.ensure_gate_scope(
                :environment_id, :created_at
            ) AS result
            """,
            {
                "environment_id": scope.environment_id,
                "created_at": scope.created_at or datetime.now().astimezone(),
            },
            _row_to_mcp_rollout_gate_scope,
        )

    async def append_mcp_rollout_drill_observation(
        self, observation: MCPRolloutDrillObservation
    ) -> MCPRolloutDrillObservation:
        blockers = validate_mcp_rollout_drill_observation(observation)
        if blockers:
            raise ValueError(
                "MCP rollout drill observation is invalid: " + ",".join(blockers)
            )
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_drill_observation(
                :drill_observation_id, :environment_id, :deployment_id,
                :config_fingerprint, :drill, :outcome, :observed_at,
                :recorded_at, :expires_at, :payload_digest
            ) AS result
            """,
            {
                "drill_observation_id": observation.drill_observation_id,
                "environment_id": observation.environment_id,
                "deployment_id": observation.deployment_id,
                "config_fingerprint": observation.config_fingerprint,
                "drill": observation.drill,
                "outcome": observation.outcome,
                "observed_at": observation.observed_at,
                "recorded_at": observation.recorded_at,
                "expires_at": observation.expires_at,
                "payload_digest": observation.payload_digest,
            },
            _row_to_mcp_rollout_drill_observation,
        )

    async def list_mcp_rollout_drill_observations(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> list[MCPRolloutDrillObservation]:
        if not environment_id or not deployment_id or window_ended_at <= window_started_at:
            raise ValueError("MCP rollout drill observation query scope is invalid")

        def _sync() -> list[MCPRolloutDrillObservation]:
            with self._mcp_rollout_session_factory() as session:
                rows = session.execute(
                    text(
                        """
                        SELECT drill_observation.*
                        FROM public.mcp_rollout_drill_observation AS drill_observation
                        WHERE drill_observation.environment_id = :environment_id
                          AND drill_observation.rollout_program = 'user_mcp_phase3'
                          AND drill_observation.deployment_id = :deployment_id
                          AND drill_observation.stage = 'internal_enforce'
                          AND drill_observation.observed_at >= :window_started_at
                          AND drill_observation.observed_at < :window_ended_at
                          AND drill_observation.expires_at > :window_ended_at
                        ORDER BY drill_observation.observed_at,
                                 drill_observation.drill,
                                 drill_observation.drill_observation_id
                        """
                    ),
                    {
                        "environment_id": environment_id,
                        "deployment_id": deployment_id,
                        "window_started_at": window_started_at,
                        "window_ended_at": window_ended_at,
                    },
                ).mappings()
                return [
                    _row_to_mcp_rollout_drill_observation(SimpleNamespace(**row))
                    for row in rows
                ]

        return await asyncio.to_thread(_sync)

    async def append_mcp_rollout_evidence_snapshot(
        self, snapshot: MCPRolloutEvidenceSnapshot
    ) -> MCPRolloutEvidenceSnapshot:
        if snapshot.source != "ci":
            raise ValueError(
                "production MCP rollout evidence must use the durable snapshot producer"
            )
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_ci_evidence_snapshot(
                :evidence_id, :environment_id, :git_sha, :deployment_id,
                :stage, :config_fingerprint, :window_started_at,
                :window_ended_at, :recorded_at, :snapshot_id, :nonce,
                :evidence_kind, CAST(:payload AS jsonb), :payload_digest
            ) AS result
            """,
            {
                "evidence_id": snapshot.evidence_id,
                "environment_id": snapshot.environment_id,
                "git_sha": snapshot.git_sha,
                "deployment_id": snapshot.deployment_id,
                "stage": snapshot.stage,
                "config_fingerprint": snapshot.config_fingerprint,
                "window_started_at": snapshot.window_started_at,
                "window_ended_at": snapshot.window_ended_at,
                "recorded_at": snapshot.recorded_at,
                "snapshot_id": snapshot.snapshot_id,
                "nonce": snapshot.nonce,
                "evidence_kind": snapshot.evidence_kind,
                "payload": json.dumps(snapshot.payload, separators=(",", ":"), sort_keys=True),
                "payload_digest": snapshot.payload_digest,
            },
            _row_to_mcp_rollout_evidence_snapshot,
        )

    async def save_mcp_shadow_audit_sample(
        self, sample: MCPShadowAuditSample
    ) -> MCPShadowAuditSample:
        from src.integrations.mcp.shadow_evidence import validate_shadow_audit_sample

        blockers = validate_shadow_audit_sample(sample)
        if blockers:
            raise ValueError(f"MCP shadow audit sample is invalid: {','.join(blockers)}")
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_shadow_audit_sample(
                :sample_id, :environment_id, :deployment_id,
                :config_fingerprint, :manifest_fingerprint,
                :fixture_fingerprint, :mapping_fingerprint, :scenario, :nonce,
                :safe_owner_ref, :safe_task_ref, :safe_call_ref, :legacy_outcome,
                :shadow_outcome, :transport, :endpoint_policy, :comparison,
                CAST(:blockers AS jsonb), :payload_digest, :observed_at,
                :recorded_at, :expires_at
            ) AS result
            """,
            {
                "sample_id": sample.sample_id,
                "environment_id": sample.environment_id,
                "deployment_id": sample.deployment_id,
                "config_fingerprint": sample.config_fingerprint,
                "manifest_fingerprint": sample.manifest_fingerprint,
                "fixture_fingerprint": sample.fixture_fingerprint,
                "mapping_fingerprint": sample.mapping_fingerprint,
                "scenario": sample.scenario,
                "nonce": sample.nonce,
                "safe_owner_ref": sample.safe_owner_ref,
                "safe_task_ref": sample.safe_task_ref,
                "safe_call_ref": sample.safe_call_ref,
                "legacy_outcome": sample.legacy_outcome,
                "shadow_outcome": sample.shadow_outcome,
                "transport": sample.transport,
                "endpoint_policy": sample.endpoint_policy,
                "comparison": sample.comparison,
                "blockers": json.dumps(sample.blockers, separators=(",", ":")),
                "payload_digest": sample.payload_digest,
                "observed_at": sample.observed_at,
                "recorded_at": sample.recorded_at,
                "expires_at": sample.expires_at,
            },
            _row_to_mcp_shadow_audit_sample,
        )

    async def delete_expired_mcp_shadow_audit_samples(
        self, *, now: datetime, limit: int = 1000
    ) -> int:
        def _sync() -> int:
            with self._mcp_rollout_session_factory() as session:
                result = session.scalar(
                    text(
                        "SELECT mcp_rollout_api.delete_expired_shadow_audit_samples("
                        ":now, :limit)"
                    ),
                    {"now": now, "limit": max(1, limit)},
                )
                session.commit()
                return int(result or 0)

        return await asyncio.to_thread(_sync)

    async def produce_mcp_shadow_evidence_snapshot(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        window_started_at: datetime,
        window_ended_at: datetime,
        builder: Callable[
            [list[MCPShadowAuditSample], list[MCPRolloutMetricBucket]],
            MCPRolloutEvidenceSnapshot,
        ],
    ) -> MCPRolloutEvidenceSnapshot:
        del environment_id, deployment_id, window_started_at, window_ended_at, builder
        raise ValueError(
            "PostgreSQL production evidence requires the DB-derived producer boundary"
        )

    async def produce_mcp_shadow_evidence_snapshot_db_derived(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        git_sha: str,
        window_started_at: datetime,
        window_ended_at: datetime,
        attestation_key_id: str,
        attestation_key: bytes,
    ) -> MCPRolloutEvidenceSnapshot:
        return await self.produce_mcp_rollout_evidence_snapshot_db_derived(
            environment_id,
            deployment_id,
            git_sha=git_sha,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            attestation_key_id=attestation_key_id,
            attestation_key=attestation_key,
        )

    async def produce_mcp_rollout_evidence_snapshot_db_derived(
        self,
        environment_id: str,
        deployment_id: str,
        *,
        git_sha: str,
        window_started_at: datetime,
        window_ended_at: datetime,
        attestation_key_id: str,
        attestation_key: bytes,
    ) -> MCPRolloutEvidenceSnapshot:
        def _sync() -> MCPRolloutEvidenceSnapshot:
            from scripts.validate_user_mcp_phase3_evidence import parse_evidence_snapshot
            from src.integrations.mcp.rollout_evidence import (
                MCPEvidenceSnapshot,
            )

            with self._mcp_rollout_session_factory() as session:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                prepared = session.execute(
                    text(
                        """
                        SELECT * FROM mcp_rollout_api.prepare_production_evidence_snapshot(
                            :environment_id, :deployment_id, :git_sha,
                            :window_started_at, :window_ended_at
                        )
                        """
                    ),
                    {
                        "environment_id": environment_id,
                        "deployment_id": deployment_id,
                        "git_sha": git_sha,
                        "window_started_at": window_started_at,
                        "window_ended_at": window_ended_at,
                    },
                ).mappings().one()
                prepared_record = {
                    key: value
                    for key, value in dict(prepared).items()
                    if key not in {"rollout_program", "evidence_kind"}
                }
                for timestamp_field in (
                    "window_started_at",
                    "window_ended_at",
                    "recorded_at",
                ):
                    prepared_record[timestamp_field] = (
                        prepared_record[timestamp_field]
                        .astimezone(timezone.utc)
                        .isoformat(timespec="microseconds")
                    )
                parsed = parse_evidence_snapshot(
                    {
                        **prepared_record,
                        "attestation_key_id": None,
                        "attestation_signature": None,
                    }
                )
                sealed = MCPEvidenceSnapshot.seal(
                    evidence_id=parsed.evidence_id,
                    environment_id=parsed.environment_id,
                    git_sha=parsed.git_sha,
                    deployment_id=parsed.deployment_id,
                    stage=parsed.stage,
                    config_fingerprint=parsed.config_fingerprint,
                    window_started_at=parsed.window_started_at,
                    window_ended_at=parsed.window_ended_at,
                    recorded_at=parsed.recorded_at,
                    producer=parsed.producer,
                    source=parsed.source,
                    snapshot_id=parsed.snapshot_id,
                    nonce=parsed.nonce,
                    payload=parsed.payload,
                    attestation_key_id=attestation_key_id,
                    attestation_key=attestation_key,
                )
                if sealed.payload_digest != parsed.payload_digest:
                    raise ValueError(
                        "PostgreSQL canonical evidence digest differs from Python"
                    )
                saved = session.execute(
                    text(
                        """
                        SELECT result.*
                        FROM mcp_rollout_api.finalize_production_evidence_snapshot(
                            :environment_id, :deployment_id, :git_sha,
                            :window_started_at, :window_ended_at, :evidence_id,
                            :payload_digest, :key_id, :signature
                        ) AS result
                        """
                    ),
                    {
                        "environment_id": environment_id,
                        "deployment_id": deployment_id,
                        "git_sha": git_sha,
                        "window_started_at": window_started_at,
                        "window_ended_at": window_ended_at,
                        "evidence_id": parsed.evidence_id,
                        "payload_digest": parsed.payload_digest,
                        "key_id": attestation_key_id,
                        "signature": sealed.attestation_signature,
                    },
                ).mappings().one()
                session.commit()
                return _row_to_mcp_rollout_evidence_snapshot(
                    SimpleNamespace(**saved)
                )

        return await asyncio.to_thread(_sync)

    async def activate_mcp_rollout_deployment(
        self, activation: MCPRolloutDeploymentActivation
    ) -> MCPRolloutDeploymentActivation:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_deployment_activation(
                :activation_id, :environment_id, :deployment_id, :stage,
                :config_fingerprint, :approval_id, :evidence_id,
                :previous_activation_id, :operator_reason, :is_rollback,
                :created_at
            ) AS result
            """,
            {
                "activation_id": activation.activation_id,
                "environment_id": activation.environment_id,
                "deployment_id": activation.deployment_id,
                "stage": activation.stage,
                "config_fingerprint": activation.config_fingerprint,
                "approval_id": activation.approval_id,
                "evidence_id": activation.evidence_id,
                "previous_activation_id": activation.previous_activation_id,
                "operator_reason": activation.operator_reason,
                "is_rollback": activation.is_rollback,
                "created_at": activation.created_at,
            },
            _row_to_mcp_rollout_deployment_activation,
        )

    async def append_mcp_rollout_stage_approval(
        self, approval: MCPRolloutStageApproval
    ) -> MCPRolloutStageApproval:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_stage_approval(
                :approval_id, :environment_id, :deployment_id, :stage,
                :config_fingerprint, :evidence_id, :reason, :approver,
                :created_at
            ) AS result
            """,
            {
                "approval_id": approval.approval_id,
                "environment_id": approval.environment_id,
                "deployment_id": approval.deployment_id,
                "stage": approval.stage,
                "config_fingerprint": approval.config_fingerprint,
                "evidence_id": approval.evidence_id,
                "reason": approval.reason,
                "approver": approval.approver,
                "created_at": approval.created_at,
            },
            _row_to_mcp_rollout_stage_approval,
        )

    async def append_mcp_rollout_promotion_block(
        self, block: MCPRolloutPromotionBlock
    ) -> MCPRolloutPromotionBlock:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_promotion_block(
                :block_id, :environment_id, :deployment_id, :stage,
                :config_fingerprint, :evidence_id, :reason_code, :created_at
            ) AS result
            """,
            {
                "block_id": block.block_id,
                "environment_id": block.environment_id,
                "deployment_id": block.deployment_id,
                "stage": block.stage,
                "config_fingerprint": block.config_fingerprint,
                "evidence_id": block.evidence_id,
                "reason_code": block.reason_code,
                "created_at": block.created_at,
            },
            _row_to_mcp_rollout_promotion_block,
        )

    async def append_mcp_rollout_block_resolution(
        self, resolution: MCPRolloutBlockResolution
    ) -> MCPRolloutBlockResolution:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_block_resolution(
                :resolution_id, :block_id, :approval_id, :evidence_id,
                :reason, :approver, :created_at
            ) AS result
            """,
            {
                "resolution_id": resolution.resolution_id,
                "block_id": resolution.block_id,
                "approval_id": resolution.approval_id,
                "evidence_id": resolution.evidence_id,
                "reason": resolution.reason,
                "approver": resolution.approver,
                "created_at": resolution.created_at,
            },
            _row_to_mcp_rollout_block_resolution,
        )

    async def save_mcp_rollout_instance_config_lease(
        self, lease: MCPRolloutInstanceConfigLease
    ) -> MCPRolloutInstanceConfigLease:
        return await self._run_mcp_rollout_function(
            """
            SELECT result.*
            FROM mcp_rollout_api.upsert_instance_config_lease(
                :instance_config_id, :environment_id, :deployment_id,
                :instance_id, :stage, :config_fingerprint, :activation_id,
                :lease_expires_at, :recorded_at
            ) AS result
            """,
            {
                "instance_config_id": lease.instance_config_id,
                "environment_id": lease.environment_id,
                "deployment_id": lease.deployment_id,
                "instance_id": lease.instance_id,
                "stage": lease.stage,
                "config_fingerprint": lease.config_fingerprint,
                "activation_id": lease.activation_id,
                "lease_expires_at": lease.lease_expires_at,
                "recorded_at": lease.updated_at,
            },
            _row_to_mcp_rollout_instance_config,
        )

    async def reserve_mcp_call(self, record: MCPCallRecord) -> bool:
        """Serialize the per-task budget and active-call reservation on PostgreSQL."""
        def _sync() -> bool:
            with self._session_factory() as session:
                session.scalar(
                    select(MCPBranchRecordRow.branch_id)
                    .where(
                        MCPBranchRecordRow.branch_id == record.branch_id,
                        MCPBranchRecordRow.owner_user_id == record.owner_user_id,
                        MCPBranchRecordRow.task_id == record.task_id,
                    )
                    .with_for_update()
                )
                result = SQLiteStateRepository(session).reserve_mcp_call(record)
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    async def claim_due_mcp_remote_task_bindings(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskBinding]:
        """Claim due remote tasks with row locks so workers never poll the same task."""
        if not claim_owner or not claim_token:
            raise ValueError("MCP remote task claim owner and token are required")
        if lease_expires_at <= now:
            raise ValueError("MCP remote task claim lease must expire after claim time")

        def _sync() -> list[MCPRemoteTaskBinding]:
            with self._session_factory() as session:
                candidates = session.scalars(
                    select(MCPRemoteTaskBindingRow)
                    .where(
                        MCPRemoteTaskBindingRow.terminal_at.is_(None),
                        MCPRemoteTaskBindingRow.next_poll_at.is_not(None),
                        MCPRemoteTaskBindingRow.next_poll_at <= now,
                        or_(
                            MCPRemoteTaskBindingRow.lease_expires_at.is_(None),
                            MCPRemoteTaskBindingRow.lease_expires_at <= now,
                        ),
                    )
                    .order_by(
                        MCPRemoteTaskBindingRow.next_poll_at,
                        MCPRemoteTaskBindingRow.safe_remote_task_ref,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(max(1, limit))
                ).all()
                claimed_refs: list[str] = []
                for candidate in candidates:
                    revision = 0 if candidate.revision is None else int(candidate.revision)
                    result = session.execute(
                        update(MCPRemoteTaskBindingRow)
                        .where(
                            MCPRemoteTaskBindingRow.safe_remote_task_ref
                            == candidate.safe_remote_task_ref,
                            MCPRemoteTaskBindingRow.terminal_at.is_(None),
                            or_(
                                MCPRemoteTaskBindingRow.lease_expires_at.is_(None),
                                MCPRemoteTaskBindingRow.lease_expires_at <= now,
                            ),
                            func.coalesce(MCPRemoteTaskBindingRow.revision, 0) == revision,
                        )
                        .values(
                            claim_owner=claim_owner,
                            claim_token=claim_token,
                            lease_expires_at=lease_expires_at,
                            revision=revision + 1,
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount:
                        claimed_refs.append(candidate.safe_remote_task_ref)
                session.flush()
                session.expire_all()
                claimed = [
                    _row_to_mcp_remote_task(row)
                    for ref in claimed_refs
                    if (row := session.get(MCPRemoteTaskBindingRow, ref)) is not None
                ]
                session.commit()
                return claimed

        return await asyncio.to_thread(_sync)

    async def update_user_mcp_server(
        self, owner_user_id: str, server_id: str, *, changes: Mapping[str, Any],
        credential_operation: str = "retain", credential: UserMCPCredentialRecord | None = None,
        security_sensitive: bool = False, expected_config_version: int | None = None,
        expected_security_version: int | None = None, updated_at: datetime
    ) -> UserMCPServer | None:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            operation=lambda state: state.update_user_mcp_server(
                owner_user_id, server_id, changes=changes, credential_operation=credential_operation,
                credential=credential, security_sensitive=security_sensitive,
                expected_config_version=expected_config_version,
                expected_security_version=expected_security_version,
                updated_at=updated_at,
            ),
        )

    async def claim_user_mcp_health_attempt(self, attempt: UserMCPHealthAttempt) -> bool:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=attempt.owner_user_id,
            server_id=attempt.server_id,
            operation=lambda state: state.claim_user_mcp_health_attempt(attempt),
        )

    async def renew_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run_with_user_mcp_server_lock(
            owner_user_id,
            server_id,
            lambda state: state.renew_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                lease_expires_at=lease_expires_at, updated_at=updated_at,
            ),
        )

    async def complete_user_mcp_health_attempt(
        self, attempt_id: str, owner_user_id: str, server_id: str, *, runner_instance_id: str,
        config_version: int, security_version: int, health_status: str, error_code: str | None,
        completed_at: datetime
    ) -> UserMCPServer | None:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            operation=lambda state: state.complete_user_mcp_health_attempt(
                attempt_id, owner_user_id, server_id, runner_instance_id=runner_instance_id,
                config_version=config_version, security_version=security_version,
                health_status=health_status, error_code=error_code, completed_at=completed_at,
            ),
        )

    async def acquire_user_mcp_scope_lease(self, lease: UserMCPScopeLease) -> bool:
        return await self._run_with_user_mcp_server_lock(
            lease.owner_user_id,
            lease.server_id,
            lambda state: state.acquire_user_mcp_scope_lease(lease),
        )

    async def renew_user_mcp_scope_lease(
        self, scope_id: str, owner_user_id: str, server_id: str, *, gateway_instance_id: str,
        security_version: int, lease_expires_at: datetime, updated_at: datetime
    ) -> bool:
        return await self._run_with_user_mcp_server_lock(
            owner_user_id,
            server_id,
            lambda state: state.renew_user_mcp_scope_lease(
                scope_id, owner_user_id, server_id, gateway_instance_id=gateway_instance_id,
                security_version=security_version, lease_expires_at=lease_expires_at, updated_at=updated_at,
            ),
        )

    async def mark_user_mcp_server_deleted(
        self, owner_user_id: str, server_id: str, *, deleted_at: datetime
    ) -> UserMCPServer | None:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            operation=lambda state: state.mark_user_mcp_server_deleted(owner_user_id, server_id, deleted_at=deleted_at),
        )

    async def finalize_user_mcp_server_delete(
        self, owner_user_id: str, server_id: str, *, now: datetime
    ) -> bool:
        return await asyncio.to_thread(
            self._run_cp7_authority_sync,
            owner_user_id=owner_user_id,
            server_id=server_id,
            operation=lambda state: state.finalize_user_mcp_server_delete(owner_user_id, server_id, now=now),
        )

    async def expire_user_mcp_health_attempts(
        self, *, now: datetime, error_code: str = "test_interrupted"
    ) -> int:
        def _sync() -> int:
            with self._session_factory() as session:
                affected = session.execute(
                    select(
                        UserMCPHealthAttemptRow.owner_user_id,
                        UserMCPHealthAttemptRow.server_id,
                    )
                    .where(UserMCPHealthAttemptRow.lease_expires_at <= now)
                    .distinct()
                ).all()
                if not affected:
                    session.commit()
                    return 0
                owners = sorted({owner_user_id for owner_user_id, _server_id in affected})
                for owner_user_id in owners:
                    session.execute(
                        text(
                            "INSERT INTO user_mcp_owner_mutation_guard "
                            "(owner_user_id, revision, server_set_fingerprint, created_at, updated_at) "
                            "VALUES (:owner, 0, :fingerprint, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                            "ON CONFLICT (owner_user_id) DO NOTHING"
                        ),
                        {"owner": owner_user_id, "fingerprint": canonical_sha256([])},
                    )
                session.scalars(
                    select(UserMCPOwnerMutationGuardRow.owner_user_id)
                    .where(UserMCPOwnerMutationGuardRow.owner_user_id.in_(owners))
                    .order_by(UserMCPOwnerMutationGuardRow.owner_user_id)
                    .with_for_update()
                ).all()
                session.scalars(
                    select(UserMCPServerRow.server_id)
                    .where(UserMCPServerRow.owner_user_id.in_(owners))
                    .order_by(UserMCPServerRow.owner_user_id, UserMCPServerRow.server_id)
                    .with_for_update()
                ).all()
                session.scalars(
                    select(UserMCPHealthAttemptRow)
                    .where(UserMCPHealthAttemptRow.lease_expires_at <= now)
                    .order_by(
                        UserMCPHealthAttemptRow.owner_user_id,
                        UserMCPHealthAttemptRow.server_id,
                    )
                    .with_for_update()
                ).all()
                result = SQLiteStateRepository(session).expire_user_mcp_health_attempts(
                    now=now, error_code=error_code
                )
                session.commit()
                return result

        return await asyncio.to_thread(_sync)

    async def release_user_mcp_health_attempt(
        self,
        attempt_id: str,
        owner_user_id: str,
        server_id: str,
        *,
        runner_instance_id: str,
        config_version: int,
        security_version: int,
    ) -> bool:
        return await self._run_with_user_mcp_server_lock(
            owner_user_id,
            server_id,
            lambda state: state.release_user_mcp_health_attempt(
                attempt_id,
                owner_user_id,
                server_id,
                runner_instance_id=runner_instance_id,
                config_version=config_version,
                security_version=security_version,
            ),
        )

    async def expire_user_mcp_scope_leases(self, *, now: datetime) -> int:
        def _sync() -> int:
            with self._session_factory() as session:
                rows = session.scalars(
                    select(UserMCPScopeLeaseRow)
                    .where(UserMCPScopeLeaseRow.lease_expires_at <= now)
                    .with_for_update(skip_locked=True)
                ).all()
                for row in rows:
                    session.delete(row)
                session.commit()
                return len(rows)

        return await asyncio.to_thread(_sync)

    async def mark_conversation_deleting(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await asyncio.to_thread(
            self._mark_conversation_deleting_sync,
            conversation_id,
            runner_id=runner_id,
            requested_at=requested_at,
            started_at=started_at,
            phase=phase,
        )

    def _mark_conversation_deleting_sync(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None,
        phase: str,
    ) -> Conversation | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == conversation_id)
                .with_for_update()
            )
            if row is None:
                return None
            if row.status == str(ConversationStatus.DELETING_FAILED):
                session.commit()
                return _row_to_conversation(row)
            if row.status == str(ConversationStatus.DELETING):
                session.commit()
                return _row_to_conversation(row)
            if row.status != str(ConversationStatus.ACTIVE):
                session.commit()
                return None
            row.status = str(ConversationStatus.DELETING)
            row.delete_runner_id = runner_id
            row.delete_requested_at = requested_at
            row.delete_started_at = started_at
            row.delete_finished_at = None
            row.delete_failed_at = None
            row.delete_error_code = None
            row.delete_error_summary = None
            row.delete_phase = phase
            row.updated_at = requested_at
            session.commit()
            return _row_to_conversation(row)

    async def retry_failed_conversation_delete(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None = None,
        phase: str = "marking",
    ) -> Conversation | None:
        return await asyncio.to_thread(
            self._retry_failed_conversation_delete_sync,
            conversation_id,
            runner_id=runner_id,
            requested_at=requested_at,
            started_at=started_at,
            phase=phase,
        )

    def _retry_failed_conversation_delete_sync(
        self,
        conversation_id: str,
        *,
        runner_id: str,
        requested_at: datetime,
        started_at: datetime | None,
        phase: str,
    ) -> Conversation | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == conversation_id)
                .with_for_update()
            )
            if row is None or row.status != str(ConversationStatus.DELETING_FAILED):
                session.commit()
                return None
            row.status = str(ConversationStatus.DELETING)
            row.delete_runner_id = runner_id
            row.delete_requested_at = requested_at
            row.delete_started_at = started_at
            row.delete_finished_at = None
            row.delete_failed_at = None
            row.delete_error_code = None
            row.delete_error_summary = None
            row.delete_phase = phase
            row.updated_at = requested_at
            session.commit()
            return _row_to_conversation(row)

    async def delete_conversation_physical(self, conversation_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._delete_conversation_physical_sync, conversation_id)

    async def delete_conversation(self, conversation_id: str) -> dict[str, int]:
        return await self.delete_conversation_physical(conversation_id)

    def _delete_conversation_physical_sync(self, conversation_id: str) -> dict[str, int]:
        deleted_counts: dict[str, int] = {
            "conversation_file_resource": 0,
            "conversation_memory_summary": 0,
            "conversation_pending_skill_context": 0,
            "mailbox_delivery": 0,
            "interrupt_answer": 0,
            "slot_event": 0,
            "slot_collection": 0,
            "checkpoint": 0,
            "interrupt": 0,
            "mailbox_message": 0,
            "event_record": 0,
            "artifact": 0,
            "task_input_attachment": 0,
            "mcp_remote_task_outbox": 0,
            "mcp_remote_task_binding": 0,
            "mcp_sealed_state": 0,
            "mcp_pending_tool_action": 0,
            "mcp_dispatch_resume_outbox": 0,
            "mcp_no_server_intent": 0,
            "mcp_call_record": 0,
            "mcp_branch_record": 0,
            "mcp_connection_lease": 0,
            "mcp_audit_event": 0,
            "task_node": 0,
            "message": 0,
            "task": 0,
            "conversation": 0,
        }

        def _rowcount(result: Any) -> int:
            rowcount = getattr(result, "rowcount", 0)
            return int(rowcount if rowcount is not None and rowcount > 0 else 0)

        statements: tuple[tuple[str, str], ...] = (
            (
                "mailbox_delivery",
                """
                DELETE FROM mailbox_delivery d
                USING mailbox_message m
                WHERE d.message_id = m.message_id
                  AND (
                    m.conversation_id = :conversation_id
                    OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "interrupt_answer",
                """
                DELETE FROM interrupt_answer a
                USING interrupt i
                WHERE a.interrupt_id = i.interrupt_id
                  AND (
                    i.conversation_id = :conversation_id
                    OR i.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "slot_event",
                """
                DELETE FROM slot_event e
                USING slot_collection c
                WHERE e.collection_id = c.collection_id
                  AND (
                    c.conversation_id = :conversation_id
                    OR c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                  )
                """,
            ),
            (
                "slot_collection",
                """
                DELETE FROM slot_collection c
                WHERE c.conversation_id = :conversation_id
                   OR c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "checkpoint",
                """
                DELETE FROM checkpoint c
                WHERE c.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "interrupt",
                """
                DELETE FROM interrupt i
                WHERE i.conversation_id = :conversation_id
                   OR i.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "mailbox_message",
                """
                DELETE FROM mailbox_message m
                WHERE m.conversation_id = :conversation_id
                   OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "event_record",
                """
                DELETE FROM event_record e
                WHERE e.conversation_id = :conversation_id
                   OR e.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "artifact",
                """
                DELETE FROM artifact a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "task_input_attachment",
                """
                DELETE FROM task_input_attachment a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_pending_tool_action",
                """
                DELETE FROM mcp_pending_tool_action a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_dispatch_resume_outbox",
                """
                DELETE FROM mcp_dispatch_resume_outbox o
                USING task t
                WHERE o.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_no_server_intent",
                """
                DELETE FROM mcp_no_server_intent i
                USING task t
                WHERE i.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_remote_task_outbox",
                """
                DELETE FROM mcp_remote_task_outbox o
                USING task t
                WHERE o.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_remote_task_binding",
                """
                DELETE FROM mcp_remote_task_binding r
                USING task t
                WHERE r.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_sealed_state",
                """
                DELETE FROM mcp_sealed_state s
                USING task t
                WHERE s.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_call_record",
                """
                DELETE FROM mcp_call_record c
                USING task t
                WHERE c.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_branch_record",
                """
                DELETE FROM mcp_branch_record b
                USING task t
                WHERE b.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_connection_lease",
                """
                DELETE FROM mcp_connection_lease l
                USING task t
                WHERE l.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "mcp_audit_event",
                """
                DELETE FROM mcp_audit_event a
                USING task t
                WHERE a.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "task_node",
                """
                DELETE FROM task_node n
                USING task t
                WHERE n.task_id = t.task_id
                  AND t.conversation_id = :conversation_id
                """,
            ),
            (
                "conversation_file_resource",
                "DELETE FROM conversation_file_resource WHERE conversation_id = :conversation_id",
            ),
            (
                "conversation_memory_summary",
                "DELETE FROM conversation_memory_summary WHERE conversation_id = :conversation_id",
            ),
            (
                "conversation_pending_skill_context",
                "DELETE FROM conversation_pending_skill_context WHERE conversation_id = :conversation_id",
            ),
            (
                "message",
                """
                DELETE FROM message m
                WHERE m.conversation_id = :conversation_id
                   OR m.task_id IN (SELECT task_id FROM task WHERE conversation_id = :conversation_id)
                """,
            ),
            (
                "task",
                "DELETE FROM task WHERE conversation_id = :conversation_id",
            ),
            (
                "conversation",
                "DELETE FROM conversation WHERE conversation_id = :conversation_id",
            ),
        )

        with self._session_factory() as session:
            for name, sql in statements:
                deleted_counts[name] = _rowcount(session.execute(text(sql), {"conversation_id": conversation_id}))
            session.commit()
        return deleted_counts

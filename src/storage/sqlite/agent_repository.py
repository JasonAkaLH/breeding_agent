from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentCompactionCommit,
    AgentCompactionResult,
    AgentFinalOutputCommit,
    AgentFinalOutputResult,
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSampleCommit,
    AgentSampleCommitResult,
    AgentStorageConflict,
    AgentTaskLease,
    AgentUserMessageCommit,
    AgentUserMessageCommitResult,
)
from src.orchestration.agent_loop.result_artifacts import (
    validate_skill_result_staged_artifact,
)
from src.storage.agent_payload import (
    CanonicalAgentPayload,
    agent_compaction_source_digest,
    agent_compaction_range_is_closed,
    canonicalize_agent_payload,
)

from src.storage.sqlalchemy_models import (
    AgentFinalReceiptRow,
    AgentItemRow,
    AgentRunRow,
    ArtifactRow,
    EventRecordRow,
    MessageRow,
    TaskNodeRow,
    TaskRow,
)


FaultInjector = Callable[[str], None]


class SQLiteAgentRepository:
    """Additive Agent durable state with transaction-scoped CAS operations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        now_fn: Callable[[], datetime] | None = None,
        fault_injector: FaultInjector | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now_fn or _utcnow_naive
        self._fault_injector = fault_injector
        self._token_factory = token_factory or (lambda: uuid4().hex)

    async def create_run(self, run: AgentRun) -> AgentRun:
        return await self._write(lambda session: self._create_run(session, run))

    async def get_run(self, run_id: str) -> AgentRun | None:
        return await self._read(lambda session: _run_from_row(session.get(AgentRunRow, run_id)))

    async def get_run_for_task(self, task_id: str) -> AgentRun | None:
        return await self._read(
            lambda session: _run_from_row(
                session.scalar(select(AgentRunRow).where(AgentRunRow.task_id == task_id))
            )
        )

    async def list_recoverable_runs(self) -> tuple[AgentRun, ...]:
        statuses = (
            AgentRunStatus.RUNNING.value,
            AgentRunStatus.WAITING_FOR_INPUT.value,
            AgentRunStatus.WAITING_FOR_DEPENDENCY.value,
        )
        return await self._read(
            lambda session: tuple(
                _run_from_row(row)
                for row in session.scalars(
                    select(AgentRunRow)
                    .where(AgentRunRow.status.in_(statuses))
                    .order_by(AgentRunRow.run_id)
                ).all()
            )
        )

    async def list_items(self, run_id: str) -> tuple[AgentItem, ...]:
        return await self._read(
            lambda session: tuple(
                _item_from_row(row)
                for row in session.scalars(
                    select(AgentItemRow)
                    .where(AgentItemRow.run_id == run_id)
                    .order_by(AgentItemRow.sequence)
                ).all()
            )
        )

    async def commit_agent_sample(self, commit: AgentSampleCommit) -> AgentSampleCommitResult:
        return await self._write(lambda session: self._commit_sample(session, commit))

    async def commit_agent_user_message(
        self, commit: AgentUserMessageCommit
    ) -> AgentUserMessageCommitResult:
        return await self._write(
            lambda session: self._commit_user_message(session, commit)
        )

    async def commit_agent_call_outcome(self, commit: AgentCallOutcomeCommit) -> AgentItem:
        return await self._write(lambda session: self._commit_outcome(session, commit))

    async def commit_agent_compaction(
        self, commit: AgentCompactionCommit
    ) -> AgentCompactionResult:
        return await self._write(lambda session: self._commit_compaction(session, commit))

    async def commit_agent_final_output(self, commit: AgentFinalOutputCommit) -> AgentFinalOutputResult:
        return await self._write(lambda session: self._commit_final(session, commit))

    async def reconcile_agent_run_consistency(self, run_id: str) -> AgentRun:
        return await self._write(lambda session: self._reconcile_consistency(session, run_id))

    async def acquire_task_lease(
        self, run_id: str, *, owner_id: str, ttl_seconds: float
    ) -> AgentTaskLease:
        return await self._write(
            lambda session: self._acquire_lease(
                session, run_id, owner_id=owner_id, ttl_seconds=ttl_seconds
            )
        )

    async def renew_task_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        token: str,
        ttl_seconds: float,
    ) -> AgentTaskLease:
        return await self._write(
            lambda session: self._renew_lease(
                session,
                run_id,
                owner_id=owner_id,
                token=token,
                ttl_seconds=ttl_seconds,
            )
        )

    async def release_waiting_task_lease(
        self, run_id: str, *, owner_id: str, token: str
    ) -> AgentRun:
        return await self._write(
            lambda session: self._release_waiting_lease(
                session, run_id, owner_id=owner_id, token=token
            )
        )

    async def fail_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_error_code: str,
    ) -> AgentRun:
        return await self._write(
            lambda session: self._commit_terminal(
                session,
                run_id,
                expected_revision=expected_revision,
                expected_claim_token=expected_claim_token,
                status=AgentRunStatus.FAILED,
                task_status="failed",
                node_status="failed",
                reason_code=safe_error_code,
            )
        )

    async def cancel_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_reason_code: str,
    ) -> AgentRun:
        return await self._write(
            lambda session: self._commit_terminal(
                session,
                run_id,
                expected_revision=expected_revision,
                expected_claim_token=expected_claim_token,
                status=AgentRunStatus.CANCELLED,
                task_status="cancelled",
                node_status="cancelled",
                reason_code=safe_reason_code,
            )
        )

    def _create_run(self, session: Session, run: AgentRun) -> AgentRun:
        if session.get(TaskRow, run.task_id) is None:
            raise AgentStorageConflict("agent_run_task_missing")
        now = self._now()
        row = AgentRunRow(
            run_id=run.run_id,
            task_id=run.task_id,
            conversation_id=run.conversation_id,
            status=run.status.value,
            model_edition=run.binding.model_edition,
            reasoning_effort=run.binding.reasoning_effort,
            thinking_enabled=run.binding.thinking_enabled,
            binding_option_digests=dict(run.binding.option_digests),
            next_item_sequence=run.next_item_sequence,
            compacted_through_sequence=run.compacted_through_sequence,
            active_sample_item_id=run.active_sample_item_id,
            waiting_call_item_ids=list(run.waiting_call_item_ids),
            next_batch_call_ordinal=run.next_batch_call_ordinal,
            claim_owner=run.claim_owner,
            claim_token=run.claim_token,
            lease_expires_at=run.lease_expires_at,
            revision=run.revision,
            terminal_reason_code=None,
            created_at=run.created_at or now,
            updated_at=run.updated_at or now,
            terminal_at=run.terminal_at,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            raise AgentStorageConflict("agent_run_task_already_bound") from exc
        return _run_from_row(row)

    def _commit_sample(self, session: Session, commit: AgentSampleCommit) -> AgentSampleCommitResult:
        row = self._locked_run(session, commit.run_id)
        self._validate_cas(row, commit.expected_revision, commit.expected_claim_token)
        if row.status != AgentRunStatus.RUNNING.value:
            raise AgentStorageConflict("agent_sample_run_not_running")
        binding = _binding_from_row(row)
        if binding != commit.sample.binding:
            raise AgentStorageConflict("agent_sample_binding_mismatch")
        tool_names = {call.provider_safe_name for call in commit.sample.tool_calls}
        if not tool_names.issubset(commit.capability_ids_by_tool_name):
            raise AgentStorageConflict("agent_sample_capability_mapping_missing")

        now = self._now()
        sequence = int(row.next_item_sequence)
        assistant_id = _sample_assistant_item_id(row.run_id, commit.sample.sample_id)
        assistant_payload = canonicalize_agent_payload(
            {
                "finish_reason": commit.sample.finish.finish_reason,
                "mixed_text_and_tool_calls": commit.sample.finish.mixed_text_and_tool_calls,
                "sample_id": commit.sample.sample_id,
                "text": commit.sample.visible_text,
                "usage": {
                    "completion_tokens": commit.sample.usage.completion_tokens,
                    "prompt_tokens": commit.sample.usage.prompt_tokens,
                    "status": commit.sample.usage.status,
                    "total_tokens": commit.sample.usage.total_tokens,
                },
            }
        )
        assistant_row = self._add_item(
            session,
            item_id=assistant_id,
            run=row,
            sequence=sequence,
            kind=AgentItemKind.ASSISTANT_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=assistant_payload,
            provider_sample_id=commit.sample.sample_id,
            created_at=now,
            committed_at=now,
        )
        sequence += 1
        call_rows: list[AgentItemRow] = []
        result_rows: list[AgentItemRow] = []
        node_ids: list[str] = []
        for call in commit.sample.tool_calls:
            call_id = _call_item_id(row.run_id, commit.sample.sample_id, call.call_id)
            result_id = _result_item_id(row.run_id, commit.sample.sample_id, call.call_id)
            node_id = _call_node_id(row.task_id, commit.sample.sample_id, call.call_id)
            call_payload = canonicalize_agent_payload(
                {
                    "arguments_json": call.arguments_json,
                    "call_id": call.call_id,
                    "capability_id": commit.capability_ids_by_tool_name[call.provider_safe_name],
                    "node_id": node_id,
                    "provider_safe_name": call.provider_safe_name,
                    "result_item_id": result_id,
                }
            )
            call_row = self._add_item(
                session,
                item_id=call_id,
                run=row,
                sequence=sequence,
                kind=AgentItemKind.TOOL_CALL,
                state=AgentItemState.COMMITTED,
                payload=call_payload,
                parent_item_id=assistant_id,
                call_ordinal=int(row.next_batch_call_ordinal) + call.ordinal,
                created_at=now,
                committed_at=now,
            )
            sequence += 1
            reservation_payload = canonicalize_agent_payload(
                {"call_item_id": call_id, "status": "reserved"}
            )
            result_row = self._add_item(
                session,
                item_id=result_id,
                run=row,
                sequence=sequence,
                kind=AgentItemKind.TOOL_RESULT,
                state=AgentItemState.RESERVED,
                payload=reservation_payload,
                parent_item_id=assistant_id,
                source_call_item_id=call_id,
                call_ordinal=int(row.next_batch_call_ordinal) + call.ordinal,
                created_at=now,
            )
            sequence += 1
            session.add(
                TaskNodeRow(
                    node_id=node_id,
                    task_id=row.task_id,
                    capability_id=commit.capability_ids_by_tool_name[call.provider_safe_name],
                    assigned_instance_id=None,
                    status="pending",
                    input_refs=[call_id],
                    output_refs=[result_id],
                    started_at=None,
                    finished_at=None,
                )
            )
            call_rows.append(call_row)
            result_rows.append(result_row)
            node_ids.append(node_id)
        session.flush()
        self._inject("sample_after_items")
        updated = session.execute(
            update(AgentRunRow)
            .where(
                AgentRunRow.run_id == row.run_id,
                AgentRunRow.revision == commit.expected_revision,
                AgentRunRow.claim_token.is_(None)
                if commit.expected_claim_token is None
                else AgentRunRow.claim_token == commit.expected_claim_token,
            )
            .values(
                next_item_sequence=sequence,
                active_sample_item_id=assistant_id,
                next_batch_call_ordinal=int(row.next_batch_call_ordinal) + len(call_rows),
                revision=int(row.revision) + 1,
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            raise AgentStorageConflict("agent_sample_cas_lost")
        session.flush()
        self._inject("sample_after_run_update")
        refreshed = self._locked_run(session, row.run_id)
        return AgentSampleCommitResult(
            run=_run_from_row(refreshed),
            assistant_item=_item_from_row(assistant_row),
            call_items=tuple(_item_from_row(item) for item in call_rows),
            result_reservations=tuple(_item_from_row(item) for item in result_rows),
            node_ids=tuple(node_ids),
        )

    def _commit_user_message(
        self,
        session: Session,
        commit: AgentUserMessageCommit,
    ) -> AgentUserMessageCommitResult:
        run = self._locked_run(session, commit.run_id)
        item_id = f"agent-item:{run.run_id}:user-initial"
        user_payload: dict[str, Any] = {"text": commit.text}
        if commit.context_budget is not None:
            user_payload["context_budget"] = commit.context_budget.to_payload()
        payload = canonicalize_agent_payload(user_payload)
        activation_payload, activation_item_id = _initial_activation_payload(
            run.run_id,
            commit,
        )
        activation_rows = tuple(
            session.scalars(
                select(AgentItemRow).where(
                    AgentItemRow.run_id == run.run_id,
                    AgentItemRow.kind == AgentItemKind.SKILL_ACTIVATION.value,
                )
            ).all()
        )
        existing = session.get(AgentItemRow, item_id)
        if existing is not None:
            if existing.payload_sha256 != payload.sha256:
                raise AgentStorageConflict("agent_user_message_conflict")
            activation_item = _validate_existing_initial_activation(
                activation_rows,
                expected_item_id=activation_item_id,
                expected_payload=activation_payload,
            )
            return AgentUserMessageCommitResult(
                _run_from_row(run),
                _item_from_row(existing),
                (
                    _item_from_row(activation_item)
                    if activation_item is not None
                    else None
                ),
            )
        if activation_rows:
            raise AgentStorageConflict("agent_initial_activation_without_user")
        self._validate_cas(
            run,
            commit.expected_revision,
            commit.expected_claim_token,
        )
        if int(run.next_item_sequence) != 1:
            raise AgentStorageConflict("agent_user_message_sequence_conflict")
        now = self._now()
        item = self._add_item(
            session,
            item_id=item_id,
            run=run,
            sequence=1,
            kind=AgentItemKind.USER_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=payload,
            created_at=now,
            committed_at=now,
        )
        self._inject("user_initial_after_user")
        activation_item = None
        next_sequence = 2
        if activation_payload is not None and activation_item_id is not None:
            activation_item = self._add_item(
                session,
                item_id=activation_item_id,
                run=run,
                sequence=2,
                kind=AgentItemKind.SKILL_ACTIVATION,
                state=AgentItemState.COMMITTED,
                payload=activation_payload,
                created_at=now,
                committed_at=now,
            )
            next_sequence = 3
            self._inject("user_initial_after_activation")
        run.next_item_sequence = next_sequence
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        self._inject("user_initial_after_run_update")
        return AgentUserMessageCommitResult(
            _run_from_row(run),
            _item_from_row(item),
            (
                _item_from_row(activation_item)
                if activation_item is not None
                else None
            ),
        )

    def _commit_outcome(self, session: Session, commit: AgentCallOutcomeCommit) -> AgentItem:
        run = self._locked_run(session, commit.run_id)
        call = session.get(AgentItemRow, commit.call_item_id)
        if call is None or call.run_id != run.run_id or call.kind != AgentItemKind.TOOL_CALL.value:
            raise AgentStorageConflict("agent_call_item_missing")
        result = session.scalar(
            select(AgentItemRow).where(AgentItemRow.source_call_item_id == call.item_id)
        )
        if result is None:
            raise AgentStorageConflict("agent_result_reservation_missing")
        call_payload = json.loads(call.payload_json)
        node_id = str(call_payload["node_id"])
        node = session.get(TaskNodeRow, node_id)
        if node is None:
            raise AgentStorageConflict("agent_call_node_missing")
        now = self._now()
        artifact_ids = [artifact.artifact_id for artifact in commit.staged_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise AgentStorageConflict("agent_outcome_duplicate_artifact_id")
        for artifact in commit.staged_artifacts:
            try:
                validate_skill_result_staged_artifact(
                    artifact,
                    run=_run_from_row(run),
                    node_id=node_id,
                    call_item_id=call.item_id,
                    safe_result=(
                        commit.safe_result_payload
                        if isinstance(commit.safe_result_payload, Mapping)
                        else None
                    ),
                )
            except ValueError as exc:
                raise AgentStorageConflict(
                    "agent_skill_result_artifact_metadata_invalid"
                ) from exc
        payload = canonicalize_agent_payload(
            {
                "artifact_refs": artifact_ids,
                "call_item_id": call.item_id,
                "outcome": commit.status.value,
                "safe_result": commit.safe_result_payload,
                "safe_error_code": commit.safe_error_code,
            }
        )
        if result.state != AgentItemState.RESERVED.value:
            expected_node_status = (
                "completed"
                if commit.status is AgentCallOutcomeStatus.COMPLETED
                else "failed"
            )
            artifacts_exact = all(
                (
                    existing := session.get(ArtifactRow, artifact.artifact_id)
                )
                is not None
                and existing.task_id == run.task_id
                and existing.producer_node_id == node_id
                and existing.artifact_type == artifact.artifact_type
                and existing.storage_ref == artifact.storage_ref
                for artifact in commit.staged_artifacts
            )
            if (
                result.state != AgentItemState.COMMITTED.value
                or result.payload_json != payload.json_text
                or result.payload_sha256 != payload.sha256
                or node.status != expected_node_status
                or list(node.output_refs or [])
                != [*artifact_ids, result.item_id]
                or not artifacts_exact
            ):
                raise AgentStorageConflict("agent_call_already_terminal")
            return _item_from_row(result)
        self._validate_cas(
            run,
            commit.expected_revision,
            commit.expected_claim_token,
        )
        continuation = (
            canonicalize_agent_payload(commit.continuation_payload)
            if commit.continuation_payload is not None
            else None
        )
        activation = commit.skill_activation_item
        if activation is not None:
            if continuation is not None:
                raise AgentStorageConflict("agent_outcome_activation_continuation_conflict")
            if (
                activation.run_id != run.run_id
                or activation.task_id != run.task_id
                or activation.sequence != int(run.next_item_sequence)
                or activation.kind is not AgentItemKind.SKILL_ACTIVATION
                or activation.state is not AgentItemState.COMMITTED
            ):
                raise AgentStorageConflict("agent_skill_activation_identity_conflict")
            activation_payload = canonicalize_agent_payload(
                json.loads(activation.payload_json)
            )
            if activation_payload.sha256 != activation.payload_sha256:
                raise AgentStorageConflict("agent_skill_activation_payload_mismatch")
        else:
            activation_payload = None
        result.payload_json = payload.json_text
        result.payload_size_bytes = payload.size_bytes
        result.payload_sha256 = payload.sha256
        result.state = AgentItemState.COMMITTED.value
        result.committed_at = now
        waiting = list(run.waiting_call_item_ids or [])
        if commit.status in {
            AgentCallOutcomeStatus.WAITING_FOR_INPUT,
            AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY,
        }:
            if call.item_id not in waiting:
                waiting.append(call.item_id)
            node.status = commit.status.value
            result.state = AgentItemState.RESERVED.value
            result.committed_at = None
            run_status = self._waiting_run_status(session, waiting)
        else:
            waiting = [item for item in waiting if item != call.item_id]
            run_status = self._waiting_run_status(session, waiting)
            node.status = "completed" if commit.status is AgentCallOutcomeStatus.COMPLETED else "failed"
            node.finished_at = now
            result.state = AgentItemState.COMMITTED.value
            result.committed_at = now
            for artifact in commit.staged_artifacts:
                existing_artifact = session.get(ArtifactRow, artifact.artifact_id)
                if existing_artifact is not None:
                    if (
                        existing_artifact.task_id != run.task_id
                        or existing_artifact.producer_node_id != node_id
                        or existing_artifact.artifact_type != artifact.artifact_type
                        or existing_artifact.storage_ref != artifact.storage_ref
                    ):
                        raise AgentStorageConflict("agent_outcome_artifact_identity_conflict")
                    continue
                session.add(
                    ArtifactRow(
                        artifact_id=artifact.artifact_id,
                        task_id=run.task_id,
                        producer_node_id=node_id,
                        artifact_type=artifact.artifact_type,
                        storage_ref=artifact.storage_ref,
                        summary=artifact.summary,
                        is_complete=True,
                        created_at=now,
                    )
                )
        node.output_refs = artifact_ids + [result.item_id]
        if continuation is not None:
            continuation_id = _continuation_item_id(call.item_id, continuation.sha256)
            existing_continuation = session.get(AgentItemRow, continuation_id)
            if existing_continuation is None:
                self._add_item(
                    session,
                    item_id=continuation_id,
                    run=run,
                    sequence=int(run.next_item_sequence),
                    kind=AgentItemKind.CONTINUATION,
                    state=AgentItemState.COMMITTED,
                    payload=continuation,
                    parent_item_id=call.item_id,
                    created_at=now,
                    committed_at=now,
                )
                run.next_item_sequence = int(run.next_item_sequence) + 1
            elif existing_continuation.payload_sha256 != continuation.sha256:
                raise AgentStorageConflict("agent_continuation_item_conflict")
        if activation is not None and activation_payload is not None:
            existing_activation = session.get(AgentItemRow, activation.item_id)
            if existing_activation is None:
                self._add_item(
                    session,
                    item_id=activation.item_id,
                    run=run,
                    sequence=int(run.next_item_sequence),
                    kind=AgentItemKind.SKILL_ACTIVATION,
                    state=AgentItemState.COMMITTED,
                    payload=activation_payload,
                    parent_item_id=call.item_id,
                    created_at=now,
                    committed_at=now,
                )
                run.next_item_sequence = int(run.next_item_sequence) + 1
            elif existing_activation.payload_sha256 != activation_payload.sha256:
                raise AgentStorageConflict("agent_skill_activation_item_conflict")
        self._inject("outcome_after_result")
        run.status = run_status
        run.waiting_call_item_ids = waiting
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        self._inject("outcome_after_run_update")
        return _item_from_row(result)

    def _commit_compaction(
        self,
        session: Session,
        commit: AgentCompactionCommit,
    ) -> AgentCompactionResult:
        run = self._locked_run(session, commit.run_id)
        self._validate_cas(run, commit.expected_revision, commit.expected_claim_token)
        if (
            commit.covered_start_sequence != int(run.compacted_through_sequence) + 1
            or commit.covered_end_sequence < commit.covered_start_sequence
            or commit.covered_end_sequence >= int(run.next_item_sequence)
            or not commit.summary.strip()
        ):
            raise AgentStorageConflict("agent_compaction_range_invalid")
        covered = session.scalars(
            select(AgentItemRow)
            .where(
                AgentItemRow.run_id == run.run_id,
                AgentItemRow.sequence >= commit.covered_start_sequence,
                AgentItemRow.sequence <= commit.covered_end_sequence,
            )
            .order_by(AgentItemRow.sequence)
        ).all()
        expected_sequences = list(
            range(commit.covered_start_sequence, commit.covered_end_sequence + 1)
        )
        if [int(item.sequence) for item in covered] != expected_sequences or any(
            item.state != AgentItemState.COMMITTED.value for item in covered
        ):
            raise AgentStorageConflict("agent_compaction_source_incomplete")
        all_items = tuple(
            _item_from_row(item)
            for item in session.scalars(
                select(AgentItemRow)
                .where(AgentItemRow.run_id == run.run_id)
                .order_by(AgentItemRow.sequence)
            ).all()
        )
        covered_items = tuple(_item_from_row(item) for item in covered)
        if not agent_compaction_range_is_closed(covered_items, all_items):
            raise AgentStorageConflict("agent_compaction_range_splits_sample")
        digest = agent_compaction_source_digest(covered_items)
        if digest != commit.source_digest:
            raise AgentStorageConflict("agent_compaction_source_digest_mismatch")
        now = self._now()
        payload = canonicalize_agent_payload(
            {
                "covered_end_sequence": commit.covered_end_sequence,
                "covered_start_sequence": commit.covered_start_sequence,
                "source_digest": digest,
                "summary": commit.summary,
            }
        )
        identity = hashlib.sha256(
            f"{run.run_id}\0{commit.covered_start_sequence}\0{commit.covered_end_sequence}\0{digest}".encode()
        ).hexdigest()[:24]
        summary_row = self._add_item(
            session,
            item_id=f"agent-item:{run.run_id}:summary:{identity}",
            run=run,
            sequence=int(run.next_item_sequence),
            kind=AgentItemKind.CONTEXT_SUMMARY,
            state=AgentItemState.COMMITTED,
            payload=payload,
            created_at=now,
            committed_at=now,
        )
        run.compacted_through_sequence = commit.covered_end_sequence
        run.next_item_sequence = int(run.next_item_sequence) + 1
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        return AgentCompactionResult(
            run=_run_from_row(run),
            summary_item=_item_from_row(summary_row),
        )

    def _reconcile_consistency(self, session: Session, run_id: str) -> AgentRun:
        run = self._locked_run(session, run_id)
        if run.status in {
            AgentRunStatus.COMPLETED.value,
            AgentRunStatus.FAILED.value,
            AgentRunStatus.CANCELLED.value,
        }:
            return _run_from_row(run)
        waiting_from_items: set[str] = set()
        invalid = False
        result_rows = session.scalars(
            select(AgentItemRow).where(
                AgentItemRow.run_id == run.run_id,
                AgentItemRow.kind == AgentItemKind.TOOL_RESULT.value,
                AgentItemRow.state == AgentItemState.RESERVED.value,
            )
        ).all()
        for result in result_rows:
            try:
                outcome = json.loads(result.payload_json).get("outcome")
            except (AttributeError, json.JSONDecodeError):
                invalid = True
                continue
            if outcome not in {
                AgentCallOutcomeStatus.WAITING_FOR_INPUT.value,
                AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY.value,
            }:
                continue
            call_id = result.source_call_item_id
            call = session.get(AgentItemRow, call_id) if call_id else None
            if call is None:
                invalid = True
                continue
            waiting_from_items.add(call_id)
            try:
                node_id = json.loads(call.payload_json)["node_id"]
            except (KeyError, TypeError, json.JSONDecodeError):
                invalid = True
                continue
            node = session.get(TaskNodeRow, node_id)
            if node is None or node.status != outcome:
                invalid = True
        if waiting_from_items != set(run.waiting_call_item_ids or ()):
            invalid = True
        if not invalid:
            expected_status = self._waiting_run_status(session, list(waiting_from_items))
            if run.status != expected_status:
                invalid = True
        if not invalid:
            return _run_from_row(run)
        return self._commit_terminal(
            session,
            run.run_id,
            expected_revision=int(run.revision),
            expected_claim_token=run.claim_token,
            status=AgentRunStatus.FAILED,
            task_status="failed",
            node_status="failed",
            reason_code="agent_waiting_consistency_error",
        )

    @staticmethod
    def _waiting_run_status(session: Session, waiting_call_ids: list[str]) -> str:
        if not waiting_call_ids:
            return AgentRunStatus.RUNNING.value
        has_input = False
        for call_id in waiting_call_ids:
            result = session.scalar(
                select(AgentItemRow).where(AgentItemRow.source_call_item_id == call_id)
            )
            if result is None:
                raise AgentStorageConflict("agent_waiting_result_missing")
            try:
                outcome = json.loads(result.payload_json).get("outcome")
            except (AttributeError, json.JSONDecodeError) as exc:
                raise AgentStorageConflict("agent_waiting_payload_invalid") from exc
            if outcome == AgentCallOutcomeStatus.WAITING_FOR_INPUT.value:
                has_input = True
            elif outcome != AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY.value:
                raise AgentStorageConflict("agent_waiting_outcome_invalid")
        return (
            AgentRunStatus.WAITING_FOR_INPUT.value
            if has_input
            else AgentRunStatus.WAITING_FOR_DEPENDENCY.value
        )

    def _commit_final(self, session: Session, commit: AgentFinalOutputCommit) -> AgentFinalOutputResult:
        run = self._locked_run(session, commit.run_id)
        text_payload = canonicalize_agent_payload({"text": commit.text})
        ids = _final_ids(run.task_id)
        existing = session.scalar(
            select(AgentFinalReceiptRow).where(AgentFinalReceiptRow.run_id == run.run_id)
        )
        if existing is not None:
            if existing.text_sha256 != hashlib.sha256(commit.text.encode("utf-8")).hexdigest():
                raise AgentStorageConflict("agent_final_output_conflict")
            return self._final_result(session, run, existing)
        self._validate_cas(run, commit.expected_revision, commit.expected_claim_token)
        if run.waiting_call_item_ids:
            raise AgentStorageConflict("agent_final_output_has_waiting_calls")
        open_results = session.scalar(
            select(AgentItemRow).where(
                AgentItemRow.run_id == run.run_id,
                AgentItemRow.kind == AgentItemKind.TOOL_RESULT.value,
                AgentItemRow.state == AgentItemState.RESERVED.value,
            ).limit(1)
        )
        if open_results is not None:
            raise AgentStorageConflict("agent_final_output_has_open_calls")
        now = self._now()
        assistant_row = self._add_item(
            session,
            item_id=ids["assistant_item_id"],
            run=run,
            sequence=int(run.next_item_sequence),
            kind=AgentItemKind.ASSISTANT_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=text_payload,
            created_at=now,
            committed_at=now,
        )
        session.add_all(
            [
                TaskNodeRow(
                    node_id=ids["node_id"],
                    task_id=run.task_id,
                    capability_id="agent.final_output",
                    assigned_instance_id=None,
                    status="completed",
                    input_refs=[assistant_row.item_id],
                    output_refs=[ids["artifact_id"]],
                    started_at=now,
                    finished_at=now,
                ),
                ArtifactRow(
                    artifact_id=ids["artifact_id"],
                    task_id=run.task_id,
                    producer_node_id=ids["node_id"],
                    artifact_type="text",
                    storage_ref=commit.text,
                    summary=None,
                    is_complete=True,
                    created_at=now,
                ),
                MessageRow(
                    message_id=ids["message_id"],
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=commit.text,
                    task_id=run.task_id,
                    stream_status="complete",
                    created_at=now,
                    message_type="chat",
                    message_metadata={"source": "agent_final_output"},
                    updated_at=now,
                ),
                EventRecordRow(
                    event_id=ids["event_id"],
                    conversation_id=run.conversation_id,
                    task_id=run.task_id,
                    node_id=ids["node_id"],
                    agent_id=None,
                    event_type="agent.final_output",
                    payload={"artifact_id": ids["artifact_id"], "message_id": ids["message_id"]},
                    visibility="frontend",
                    created_at=now,
                ),
            ]
        )
        receipt = AgentFinalReceiptRow(
            receipt_id=ids["receipt_id"],
            run_id=run.run_id,
            task_id=run.task_id,
            assistant_item_id=assistant_row.item_id,
            node_id=ids["node_id"],
            artifact_id=ids["artifact_id"],
            message_id=ids["message_id"],
            event_id=ids["event_id"],
            text_sha256=hashlib.sha256(commit.text.encode("utf-8")).hexdigest(),
            created_at=now,
        )
        session.add(receipt)
        self._inject("final_after_projection")
        task = session.get(TaskRow, run.task_id)
        if task is None:
            raise AgentStorageConflict("agent_final_task_missing")
        task.status = "completed"
        task.updated_at = now
        run.status = AgentRunStatus.COMPLETED.value
        run.next_item_sequence = int(run.next_item_sequence) + 1
        run.active_sample_item_id = assistant_row.item_id
        run.claim_owner = None
        run.claim_token = None
        run.lease_expires_at = None
        run.revision = int(run.revision) + 1
        run.updated_at = now
        run.terminal_at = now
        session.flush()
        self._inject("final_after_run_update")
        return AgentFinalOutputResult(
            run=_run_from_row(run),
            assistant_item=_item_from_row(assistant_row),
            node_id=ids["node_id"],
            artifact_id=ids["artifact_id"],
            message_id=ids["message_id"],
            event_id=ids["event_id"],
            receipt_id=ids["receipt_id"],
        )

    def _commit_terminal(
        self,
        session: Session,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        status: AgentRunStatus,
        task_status: str,
        node_status: str,
        reason_code: str,
    ) -> AgentRun:
        run = self._locked_run(session, run_id)
        self._validate_cas(run, expected_revision, expected_claim_token)
        now = self._now()
        session.execute(
            update(TaskNodeRow)
            .where(
                TaskNodeRow.task_id == run.task_id,
                TaskNodeRow.status.not_in(("completed", "failed", "cancelled")),
            )
            .values(status=node_status, finished_at=now)
        )
        task = session.get(TaskRow, run.task_id)
        if task is None:
            raise AgentStorageConflict("agent_terminal_task_missing")
        task.status = task_status
        task.updated_at = now
        run.status = status.value
        run.waiting_call_item_ids = []
        run.claim_owner = None
        run.claim_token = None
        run.lease_expires_at = None
        run.revision = int(run.revision) + 1
        run.terminal_reason_code = reason_code
        run.updated_at = now
        run.terminal_at = now
        session.flush()
        return _run_from_row(run)

    def _acquire_lease(
        self,
        session: Session,
        run_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
    ) -> AgentTaskLease:
        _validate_lease_inputs(owner_id, ttl_seconds)
        run = self._locked_run(session, run_id)
        if run.status in {
            AgentRunStatus.COMPLETED.value,
            AgentRunStatus.FAILED.value,
            AgentRunStatus.CANCELLED.value,
        }:
            raise AgentStorageConflict("agent_task_lease_run_terminal")
        now = self._storage_now(session)
        if run.claim_token and run.lease_expires_at and run.lease_expires_at > now:
            raise AgentStorageConflict("agent_task_lease_held")
        token = self._token_factory()
        if not token:
            raise AgentStorageConflict("agent_task_lease_token_invalid")
        run.claim_owner = owner_id
        run.claim_token = token
        run.lease_expires_at = now + timedelta(seconds=ttl_seconds)
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        return _lease_from_row(run)

    def _renew_lease(
        self,
        session: Session,
        run_id: str,
        *,
        owner_id: str,
        token: str,
        ttl_seconds: float,
    ) -> AgentTaskLease:
        _validate_lease_inputs(owner_id, ttl_seconds)
        run = self._locked_run(session, run_id)
        now = self._storage_now(session)
        if (
            run.claim_owner != owner_id
            or run.claim_token != token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise AgentStorageConflict("agent_task_lease_lost")
        next_token = self._token_factory()
        if not next_token or next_token == token:
            raise AgentStorageConflict("agent_task_lease_token_not_rotated")
        run.claim_token = next_token
        run.lease_expires_at = now + timedelta(seconds=ttl_seconds)
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        return _lease_from_row(run)

    def _release_waiting_lease(
        self,
        session: Session,
        run_id: str,
        *,
        owner_id: str,
        token: str,
    ) -> AgentRun:
        run = self._locked_run(session, run_id)
        now = self._storage_now(session)
        if (
            run.status not in {
                AgentRunStatus.WAITING_FOR_INPUT.value,
                AgentRunStatus.WAITING_FOR_DEPENDENCY.value,
            }
            or run.claim_owner != owner_id
            or run.claim_token != token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise AgentStorageConflict("agent_waiting_lease_release_rejected")
        run.claim_owner = None
        run.claim_token = None
        run.lease_expires_at = None
        run.revision = int(run.revision) + 1
        run.updated_at = now
        session.flush()
        return _run_from_row(run)

    def _final_result(
        self,
        session: Session,
        run: AgentRunRow,
        receipt: AgentFinalReceiptRow,
    ) -> AgentFinalOutputResult:
        item = session.get(AgentItemRow, receipt.assistant_item_id)
        if item is None:
            raise AgentStorageConflict("agent_final_receipt_item_missing")
        return AgentFinalOutputResult(
            run=_run_from_row(run),
            assistant_item=_item_from_row(item),
            node_id=receipt.node_id,
            artifact_id=receipt.artifact_id,
            message_id=receipt.message_id,
            event_id=receipt.event_id,
            receipt_id=receipt.receipt_id,
        )

    @staticmethod
    def _add_item(
        session: Session,
        *,
        item_id: str,
        run: AgentRunRow,
        sequence: int,
        kind: AgentItemKind,
        state: AgentItemState,
        payload: CanonicalAgentPayload,
        parent_item_id: str | None = None,
        source_call_item_id: str | None = None,
        provider_sample_id: str | None = None,
        call_ordinal: int | None = None,
        created_at: datetime,
        committed_at: datetime | None = None,
    ) -> AgentItemRow:
        row = AgentItemRow(
            item_id=item_id,
            run_id=run.run_id,
            task_id=run.task_id,
            sequence=sequence,
            kind=kind.value,
            state=state.value,
            payload_json=payload.json_text,
            payload_size_bytes=payload.size_bytes,
            payload_sha256=payload.sha256,
            parent_item_id=parent_item_id,
            source_call_item_id=source_call_item_id,
            provider_sample_id=provider_sample_id,
            call_ordinal=call_ordinal,
            created_at=created_at,
            committed_at=committed_at,
        )
        session.add(row)
        return row

    @staticmethod
    def _locked_run(session: Session, run_id: str) -> AgentRunRow:
        row = session.scalar(
            select(AgentRunRow).where(AgentRunRow.run_id == run_id).with_for_update()
        )
        if row is None:
            raise AgentStorageConflict("agent_run_missing")
        return row

    @staticmethod
    def _validate_cas(row: AgentRunRow, revision: int, claim_token: str | None) -> None:
        if int(row.revision) != revision or row.claim_token != claim_token:
            raise AgentStorageConflict("agent_run_cas_mismatch")
        if row.status in {
            AgentRunStatus.COMPLETED.value,
            AgentRunStatus.FAILED.value,
            AgentRunStatus.CANCELLED.value,
        }:
            raise AgentStorageConflict("agent_run_terminal")

    async def _write(self, callback: Callable[[Session], Any]) -> Any:
        def execute() -> Any:
            with self._session_factory() as session:
                if session.get_bind().dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                try:
                    result = callback(session)
                    session.commit()
                    return result
                except Exception:
                    session.rollback()
                    raise

        return await asyncio.shield(asyncio.create_task(asyncio.to_thread(execute)))

    async def _read(self, callback: Callable[[Session], Any]) -> Any:
        def execute() -> Any:
            with self._session_factory() as session:
                return callback(session)

        return await asyncio.to_thread(execute)

    def _inject(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _storage_now(self, session: Session) -> datetime:
        if session.get_bind().dialect.name == "postgresql":
            value = session.scalar(select(func.current_timestamp()))
            if not isinstance(value, datetime):
                raise AgentStorageConflict("agent_storage_clock_unavailable")
            if value.utcoffset() is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        return self._now()


def _binding_from_row(row: AgentRunRow) -> AgentModelBinding:
    return AgentModelBinding(
        model_edition=row.model_edition,
        reasoning_effort=row.reasoning_effort,
        thinking_enabled=bool(row.thinking_enabled),
        option_digests=dict(row.binding_option_digests or {}),
    )


def _run_from_row(row: AgentRunRow | None) -> AgentRun | None:
    if row is None:
        return None
    return AgentRun(
        run_id=row.run_id,
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        status=AgentRunStatus(row.status),
        binding=_binding_from_row(row),
        next_item_sequence=int(row.next_item_sequence),
        compacted_through_sequence=int(row.compacted_through_sequence),
        active_sample_item_id=row.active_sample_item_id,
        waiting_call_item_ids=tuple(row.waiting_call_item_ids or ()),
        next_batch_call_ordinal=int(row.next_batch_call_ordinal),
        claim_owner=row.claim_owner,
        claim_token=row.claim_token,
        lease_expires_at=row.lease_expires_at,
        revision=int(row.revision),
        terminal_reason_code=row.terminal_reason_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
    )


def _item_from_row(row: AgentItemRow) -> AgentItem:
    return AgentItem(
        item_id=row.item_id,
        run_id=row.run_id,
        task_id=row.task_id,
        sequence=int(row.sequence),
        kind=AgentItemKind(row.kind),
        state=AgentItemState(row.state),
        payload_json=row.payload_json,
        payload_sha256=row.payload_sha256,
        parent_item_id=row.parent_item_id,
        source_call_item_id=row.source_call_item_id,
        provider_sample_id=row.provider_sample_id,
        call_ordinal=None if row.call_ordinal is None else int(row.call_ordinal),
        created_at=_as_utc_naive(row.created_at),
        committed_at=_as_utc_naive(row.committed_at),
    )


def _as_utc_naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _initial_activation_payload(
    run_id: str,
    commit: AgentUserMessageCommit,
) -> tuple[CanonicalAgentPayload | None, str | None]:
    if commit.skill_activation_payload_json is None:
        return None, None
    value = json.loads(commit.skill_activation_payload_json)
    profile = value.get("profile")
    capability_id = profile.get("capability_id") if isinstance(profile, dict) else None
    revision = value.get("pinned_bundle_revision")
    profile_digest = value.get("profile_digest")
    if (
        value.get("binding_mode") != "hint"
        or not isinstance(capability_id, str)
        or not capability_id.startswith("skill.")
        or not isinstance(revision, str)
        or not revision
        or not isinstance(profile_digest, str)
        or canonicalize_agent_payload(profile).sha256 != profile_digest
    ):
        raise AgentStorageConflict("agent_initial_activation_payload_invalid")
    payload = canonicalize_agent_payload(value)
    if (
        payload.json_text != commit.skill_activation_payload_json
        or payload.sha256 != commit.skill_activation_payload_sha256
    ):
        raise AgentStorageConflict("agent_initial_activation_payload_invalid")
    identity = hashlib.sha256(
        f"{run_id}\0{capability_id}\0{revision}".encode()
    ).hexdigest()[:24]
    return payload, f"agent-item:{run_id}:skill-activation:{identity}"


def _validate_existing_initial_activation(
    rows: tuple[AgentItemRow, ...],
    *,
    expected_item_id: str | None,
    expected_payload: CanonicalAgentPayload | None,
) -> AgentItemRow | None:
    if expected_payload is None or expected_item_id is None:
        if rows:
            raise AgentStorageConflict("agent_initial_activation_presence_conflict")
        return None
    if len(rows) != 1:
        raise AgentStorageConflict("agent_initial_activation_presence_conflict")
    row = rows[0]
    if (
        row.item_id != expected_item_id
        or int(row.sequence) != 2
        or row.state != AgentItemState.COMMITTED.value
        or row.payload_json != expected_payload.json_text
        or row.payload_sha256 != expected_payload.sha256
    ):
        raise AgentStorageConflict("agent_initial_activation_identity_conflict")
    return row


def _sample_assistant_item_id(run_id: str, sample_id: str) -> str:
    return f"agent-item:{run_id}:sample:{hashlib.sha256(sample_id.encode()).hexdigest()[:24]}"


def _call_item_id(run_id: str, sample_id: str, call_id: str) -> str:
    identity = f"{sample_id}\0{call_id}"
    return f"agent-item:{run_id}:call:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _result_item_id(run_id: str, sample_id: str, call_id: str) -> str:
    identity = f"{sample_id}\0{call_id}"
    return f"agent-item:{run_id}:result:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _continuation_item_id(call_item_id: str, payload_sha256: str) -> str:
    identity = f"{call_item_id}\0{payload_sha256}"
    return f"agent-item:continuation:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _call_node_id(task_id: str, sample_id: str, call_id: str) -> str:
    identity = f"{sample_id}\0{call_id}"
    return f"agent-node:{task_id}:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _final_ids(task_id: str) -> dict[str, str]:
    return {
        "assistant_item_id": f"agent-item:{task_id}:final",
        "node_id": f"agent-node:{task_id}:final",
        "artifact_id": f"agent-artifact:{task_id}:final",
        "message_id": f"agent-message:{task_id}:final",
        "event_id": f"agent-event:{task_id}:final",
        "receipt_id": f"agent-receipt:{task_id}:final",
    }


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_lease_inputs(owner_id: str, ttl_seconds: float) -> None:
    if not owner_id.strip() or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("Agent Task lease owner and positive TTL are required")


def _lease_from_row(row: AgentRunRow) -> AgentTaskLease:
    if not row.claim_owner or not row.claim_token or row.lease_expires_at is None:
        raise AgentStorageConflict("agent_task_lease_row_incomplete")
    return AgentTaskLease(
        run_id=row.run_id,
        task_id=row.task_id,
        owner_id=row.claim_owner,
        token=row.claim_token,
        revision=int(row.revision),
        expires_at=row.lease_expires_at,
    )

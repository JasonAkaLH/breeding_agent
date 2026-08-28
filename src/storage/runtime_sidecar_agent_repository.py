from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

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
from src.storage.runtime_sidecar_grpc_client import RuntimeSidecarGrpcClient


class RuntimeSidecarAgentRepository:
    """Agent repository whose atomic authority is the Rust Runtime Sidecar."""

    def __init__(
        self,
        client: RuntimeSidecarGrpcClient,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    async def create_run(self, run: AgentRun) -> AgentRun:
        existing = await self.get_run(run.run_id)
        if existing is not None:
            if (
                existing.task_id != run.task_id
                or existing.conversation_id != run.conversation_id
                or existing.binding != run.binding
            ):
                raise AgentStorageConflict("agent_run_identity_conflict")
            return existing
        task = await self._get_task(run.task_id)
        if task is None or task["conversation_id"] != run.conversation_id:
            raise AgentStorageConflict("agent_run_task_missing")
        response = await self._commit(
            operation="create_run",
            run=run,
            items=(),
            expected_revision=0,
            expected_claim_token=None,
            idempotency_key=f"agent-create:{run.run_id}",
        )
        return _run_from_wire(response["run"])

    async def get_run(self, run_id: str) -> AgentRun | None:
        response = await self._call(self._client.get_agent_run, run_id=run_id)
        return _run_from_wire(response["run"]) if response["found"] else None

    async def get_run_for_task(self, task_id: str) -> AgentRun | None:
        response = await self._call(self._client.get_agent_run_for_task, task_id=task_id)
        return _run_from_wire(response["run"]) if response["found"] else None

    async def list_recoverable_runs(self) -> tuple[AgentRun, ...]:
        response = await self._call(
            self._client.list_agent_runs,
            statuses=(
                AgentRunStatus.RUNNING.value,
                AgentRunStatus.WAITING_FOR_INPUT.value,
                AgentRunStatus.WAITING_FOR_DEPENDENCY.value,
            ),
        )
        return tuple(_run_from_wire(run) for run in response["runs"])

    async def list_items(self, run_id: str) -> tuple[AgentItem, ...]:
        response = await self._call(self._client.list_agent_items, run_id=run_id)
        return tuple(_item_from_wire(item) for item in response["items"])

    async def commit_agent_sample(self, commit: AgentSampleCommit) -> AgentSampleCommitResult:
        run = await self._require_cas_run(
            commit.run_id, commit.expected_revision, commit.expected_claim_token
        )
        if run.status is not AgentRunStatus.RUNNING:
            raise AgentStorageConflict("agent_sample_run_not_running")
        if run.binding != commit.sample.binding:
            raise AgentStorageConflict("agent_sample_binding_mismatch")
        tool_names = {call.provider_safe_name for call in commit.sample.tool_calls}
        if not tool_names.issubset(commit.capability_ids_by_tool_name):
            raise AgentStorageConflict("agent_sample_capability_mapping_missing")

        now = self._now()
        sequence = run.next_item_sequence
        assistant_id = _sample_assistant_item_id(run.run_id, commit.sample.sample_id)
        assistant = _item(
            item_id=assistant_id,
            run=run,
            sequence=sequence,
            kind=AgentItemKind.ASSISTANT_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=canonicalize_agent_payload(
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
            ),
            provider_sample_id=commit.sample.sample_id,
            created_at=now,
            committed_at=now,
        )
        sequence += 1
        calls: list[AgentItem] = []
        reservations: list[AgentItem] = []
        nodes: list[dict[str, Any]] = []
        for tool_call in commit.sample.tool_calls:
            call_id = _call_item_id(run.run_id, commit.sample.sample_id, tool_call.call_id)
            result_id = _result_item_id(run.run_id, commit.sample.sample_id, tool_call.call_id)
            node_id = _call_node_id(run.task_id, commit.sample.sample_id, tool_call.call_id)
            ordinal = run.next_batch_call_ordinal + tool_call.ordinal
            call_item = _item(
                item_id=call_id,
                run=run,
                sequence=sequence,
                kind=AgentItemKind.TOOL_CALL,
                state=AgentItemState.COMMITTED,
                payload=canonicalize_agent_payload(
                    {
                        "arguments_json": tool_call.arguments_json,
                        "call_id": tool_call.call_id,
                        "capability_id": commit.capability_ids_by_tool_name[
                            tool_call.provider_safe_name
                        ],
                        "node_id": node_id,
                        "provider_safe_name": tool_call.provider_safe_name,
                        "result_item_id": result_id,
                    }
                ),
                parent_item_id=assistant_id,
                call_ordinal=ordinal,
                created_at=now,
                committed_at=now,
            )
            sequence += 1
            reservation = _item(
                item_id=result_id,
                run=run,
                sequence=sequence,
                kind=AgentItemKind.TOOL_RESULT,
                state=AgentItemState.RESERVED,
                payload=canonicalize_agent_payload(
                    {"call_item_id": call_id, "status": "reserved"}
                ),
                parent_item_id=assistant_id,
                source_call_item_id=call_id,
                call_ordinal=ordinal,
                created_at=now,
            )
            sequence += 1
            calls.append(call_item)
            reservations.append(reservation)
            nodes.append(
                {
                    "node_id": node_id,
                    "task_id": run.task_id,
                    "capability_id": commit.capability_ids_by_tool_name[
                        tool_call.provider_safe_name
                    ],
                    "assigned_instance_id": None,
                    "status": "pending",
                    "input_refs": [call_id],
                    "output_refs": [result_id],
                    "started_at": None,
                    "finished_at": None,
                }
            )
        updated_run = replace(
            run,
            next_item_sequence=sequence,
            active_sample_item_id=assistant_id,
            next_batch_call_ordinal=run.next_batch_call_ordinal + len(calls),
            revision=run.revision + 1,
            updated_at=now,
        )
        all_items = (assistant, *calls, *reservations)
        response = await self._commit(
            operation="commit_sample",
            run=updated_run,
            items=all_items,
            expected_revision=commit.expected_revision,
            expected_claim_token=commit.expected_claim_token,
            idempotency_key=f"agent-sample:{run.run_id}:{commit.sample.sample_id}",
            task_nodes=tuple(nodes),
        )
        return AgentSampleCommitResult(
            run=_run_from_wire(response["run"]),
            assistant_item=assistant,
            call_items=tuple(calls),
            result_reservations=tuple(reservations),
            node_ids=tuple(node["node_id"] for node in nodes),
        )

    async def commit_agent_user_message(
        self, commit: AgentUserMessageCommit
    ) -> AgentUserMessageCommitResult:
        run = await self._require_cas_run(
            commit.run_id,
            commit.expected_revision,
            commit.expected_claim_token,
        )
        items = await self.list_items(run.run_id)
        item_id = f"agent-item:{run.run_id}:user-initial"
        user_payload: dict[str, Any] = {"text": commit.text}
        if commit.context_budget is not None:
            user_payload["context_budget"] = commit.context_budget.to_payload()
        payload = canonicalize_agent_payload(user_payload)
        now = self._now()
        activation_item = _initial_activation_item(run, commit, now=now)
        activation_items = tuple(
            item for item in items if item.kind is AgentItemKind.SKILL_ACTIVATION
        )
        existing = next((item for item in items if item.item_id == item_id), None)
        if existing is not None:
            if existing.payload_sha256 != payload.sha256:
                raise AgentStorageConflict("agent_user_message_conflict")
            stored_activation = _validate_sidecar_initial_activation(
                activation_items,
                expected=activation_item,
            )
            return AgentUserMessageCommitResult(run, existing, stored_activation)
        if activation_items:
            raise AgentStorageConflict("agent_initial_activation_without_user")
        if run.next_item_sequence != 1:
            raise AgentStorageConflict("agent_user_message_sequence_conflict")
        item = _item(
            item_id=item_id,
            run=run,
            sequence=1,
            kind=AgentItemKind.USER_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=payload,
            created_at=now,
            committed_at=now,
        )
        committed_items = (item,) if activation_item is None else (item, activation_item)
        updated_run = replace(
            run,
            next_item_sequence=2 if activation_item is None else 3,
            revision=run.revision + 1,
            updated_at=now,
        )
        identity = hashlib.sha256(
            "\0".join(
                item.payload_sha256 for item in committed_items
            ).encode()
        ).hexdigest()
        response = await self._commit(
            operation="commit_user_message",
            run=updated_run,
            items=committed_items,
            expected_revision=commit.expected_revision,
            expected_claim_token=commit.expected_claim_token,
            idempotency_key=f"agent-user-message:{run.run_id}:{identity}",
        )
        return AgentUserMessageCommitResult(
            _run_from_wire(response["run"]),
            item,
            activation_item,
        )

    async def commit_agent_call_outcome(self, commit: AgentCallOutcomeCommit) -> AgentItem:
        run = await self.get_run(commit.run_id)
        if run is None:
            raise AgentStorageConflict("agent_run_missing")
        items = await self.list_items(run.run_id)
        call = next(
            (
                item
                for item in items
                if item.item_id == commit.call_item_id and item.kind is AgentItemKind.TOOL_CALL
            ),
            None,
        )
        if call is None:
            raise AgentStorageConflict("agent_call_item_missing")
        result = next((item for item in items if item.source_call_item_id == call.item_id), None)
        if result is None:
            raise AgentStorageConflict("agent_result_reservation_missing")
        call_payload = json.loads(call.payload_json)
        node_id = str(call_payload["node_id"])
        node_response = await self._call(self._client.get_task_node, node_id=node_id)
        if not node_response["found"]:
            raise AgentStorageConflict("agent_call_node_missing")
        artifact_ids = [artifact.artifact_id for artifact in commit.staged_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise AgentStorageConflict("agent_outcome_duplicate_artifact_id")
        for artifact in commit.staged_artifacts:
            try:
                validate_skill_result_staged_artifact(
                    artifact,
                    run=run,
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
        if result.state is not AgentItemState.RESERVED:
            node = dict(node_response["node"])
            expected_node_status = (
                "completed"
                if commit.status is AgentCallOutcomeStatus.COMPLETED
                else "failed"
            )
            if (
                result.state is not AgentItemState.COMMITTED
                or result.payload_json != payload.json_text
                or result.payload_sha256 != payload.sha256
                or node.get("status") != expected_node_status
                or list(node.get("output_refs") or ())
                != [*artifact_ids, result.item_id]
            ):
                raise AgentStorageConflict("agent_call_already_terminal")
            return result
        run = await self._require_cas_run(
            commit.run_id,
            commit.expected_revision,
            commit.expected_claim_token,
        )
        continuation_payload = (
            canonicalize_agent_payload(commit.continuation_payload)
            if commit.continuation_payload is not None
            else None
        )
        activation_item = commit.skill_activation_item
        if activation_item is not None:
            if continuation_payload is not None:
                raise AgentStorageConflict("agent_outcome_activation_continuation_conflict")
            if (
                activation_item.run_id != run.run_id
                or activation_item.task_id != run.task_id
                or activation_item.sequence != run.next_item_sequence
                or activation_item.kind is not AgentItemKind.SKILL_ACTIVATION
                or activation_item.state is not AgentItemState.COMMITTED
                or canonicalize_agent_payload(
                    json.loads(activation_item.payload_json)
                ).sha256
                != activation_item.payload_sha256
            ):
                raise AgentStorageConflict("agent_skill_activation_identity_conflict")
        if result.state is not AgentItemState.RESERVED:
            if result.payload_sha256 == payload.sha256:
                return result
            raise AgentStorageConflict("agent_call_already_terminal")
        now = self._now()
        waiting = list(run.waiting_call_item_ids)
        is_waiting = commit.status in {
            AgentCallOutcomeStatus.WAITING_FOR_INPUT,
            AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY,
        }
        if is_waiting and call.item_id not in waiting:
            waiting.append(call.item_id)
        if not is_waiting:
            waiting = [item_id for item_id in waiting if item_id != call.item_id]
        updated_result = replace(
            result,
            payload_json=payload.json_text,
            payload_sha256=payload.sha256,
            state=AgentItemState.RESERVED if is_waiting else AgentItemState.COMMITTED,
            committed_at=None if is_waiting else now,
        )
        continuation_item = None
        if continuation_payload is not None:
            identity = hashlib.sha256(
                f"{call.item_id}\0{continuation_payload.sha256}".encode()
            ).hexdigest()[:24]
            continuation_item = _item(
                item_id=f"agent-item:continuation:{identity}",
                run=run,
                sequence=run.next_item_sequence,
                kind=AgentItemKind.CONTINUATION,
                state=AgentItemState.COMMITTED,
                payload=continuation_payload,
                parent_item_id=call.item_id,
                created_at=now,
                committed_at=now,
            )
        node = dict(node_response["node"])
        node["status"] = (
            commit.status.value
            if is_waiting
            else "completed"
            if commit.status is AgentCallOutcomeStatus.COMPLETED
            else "failed"
        )
        node["output_refs"] = [*artifact_ids, result.item_id]
        if not is_waiting:
            node["finished_at"] = _iso(now)
        updated_run = replace(
            run,
            status=_waiting_status(waiting, items, updated_result),
            waiting_call_item_ids=tuple(waiting),
            next_item_sequence=(
                run.next_item_sequence + 1
                if continuation_item is not None or activation_item is not None
                else run.next_item_sequence
            ),
            revision=run.revision + 1,
            updated_at=now,
        )
        artifacts = tuple(
            {
                "artifact_id": artifact.artifact_id,
                "task_id": run.task_id,
                "producer_node_id": node_id,
                "artifact_type": artifact.artifact_type,
                "storage_ref": artifact.storage_ref,
                "summary": artifact.summary or "",
                "is_complete": True,
                "created_at": _iso(now),
            }
            for artifact in commit.staged_artifacts
        ) if not is_waiting else ()
        await self._commit(
            operation="commit_outcome",
            run=updated_run,
            items=tuple(
                item
                for item in (updated_result, continuation_item, activation_item)
                if item is not None
            ),
            expected_revision=commit.expected_revision,
            expected_claim_token=commit.expected_claim_token,
            idempotency_key=(
                f"agent-outcome:{run.run_id}:{result.item_id}:{payload.sha256}:"
                f"{continuation_payload.sha256 if continuation_payload is not None else 'none'}:"
                f"{activation_item.payload_sha256 if activation_item is not None else 'none'}"
            ),
            task_nodes=(node,),
            artifacts=artifacts,
        )
        return updated_result

    async def commit_agent_compaction(
        self, commit: AgentCompactionCommit
    ) -> AgentCompactionResult:
        run = await self._require_cas_run(
            commit.run_id,
            commit.expected_revision,
            commit.expected_claim_token,
        )
        if (
            commit.covered_start_sequence != run.compacted_through_sequence + 1
            or commit.covered_end_sequence < commit.covered_start_sequence
            or commit.covered_end_sequence >= run.next_item_sequence
            or not commit.summary.strip()
        ):
            raise AgentStorageConflict("agent_compaction_range_invalid")
        all_items = await self.list_items(run.run_id)
        covered = tuple(
            item
            for item in all_items
            if commit.covered_start_sequence <= item.sequence <= commit.covered_end_sequence
        )
        if (
            [item.sequence for item in covered]
            != list(range(commit.covered_start_sequence, commit.covered_end_sequence + 1))
            or any(item.state is not AgentItemState.COMMITTED for item in covered)
        ):
            raise AgentStorageConflict("agent_compaction_source_incomplete")
        if not agent_compaction_range_is_closed(covered, all_items):
            raise AgentStorageConflict("agent_compaction_range_splits_sample")
        digest = agent_compaction_source_digest(covered)
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
        summary_item = _item(
            item_id=f"agent-item:{run.run_id}:summary:{identity}",
            run=run,
            sequence=run.next_item_sequence,
            kind=AgentItemKind.CONTEXT_SUMMARY,
            state=AgentItemState.COMMITTED,
            payload=payload,
            created_at=now,
            committed_at=now,
        )
        updated_run = replace(
            run,
            compacted_through_sequence=commit.covered_end_sequence,
            next_item_sequence=run.next_item_sequence + 1,
            revision=run.revision + 1,
            updated_at=now,
        )
        response = await self._commit(
            operation="commit_compaction",
            run=updated_run,
            items=(summary_item,),
            expected_revision=commit.expected_revision,
            expected_claim_token=commit.expected_claim_token,
            idempotency_key=(
                f"agent-compaction:{run.run_id}:{commit.covered_start_sequence}:"
                f"{commit.covered_end_sequence}:{digest}"
            ),
        )
        return AgentCompactionResult(
            run=_run_from_wire(response["run"]),
            summary_item=summary_item,
        )

    async def commit_agent_final_output(
        self, commit: AgentFinalOutputCommit
    ) -> AgentFinalOutputResult:
        run = await self.get_run(commit.run_id)
        if run is None:
            raise AgentStorageConflict("agent_run_missing")
        ids = _final_ids(run.task_id)
        text_payload = canonicalize_agent_payload({"text": commit.text})
        if run.status is AgentRunStatus.COMPLETED:
            items = await self.list_items(run.run_id)
            item = next((entry for entry in items if entry.item_id == ids["assistant_item_id"]), None)
            if item is None or item.payload_sha256 != text_payload.sha256:
                raise AgentStorageConflict("agent_final_output_conflict")
            return _final_result(run, item, ids)
        self._validate_cas(run, commit.expected_revision, commit.expected_claim_token)
        if run.waiting_call_item_ids:
            raise AgentStorageConflict("agent_final_output_has_waiting_calls")
        if any(
            item.kind is AgentItemKind.TOOL_RESULT and item.state is AgentItemState.RESERVED
            for item in await self.list_items(run.run_id)
        ):
            raise AgentStorageConflict("agent_final_output_has_open_calls")
        task = await self._get_task(run.task_id)
        if task is None:
            raise AgentStorageConflict("agent_final_task_missing")
        now = self._now()
        assistant = _item(
            item_id=ids["assistant_item_id"],
            run=run,
            sequence=run.next_item_sequence,
            kind=AgentItemKind.ASSISTANT_MESSAGE,
            state=AgentItemState.COMMITTED,
            payload=text_payload,
            created_at=now,
            committed_at=now,
        )
        node = {
            "node_id": ids["node_id"],
            "task_id": run.task_id,
            "capability_id": "agent.final_output",
            "assigned_instance_id": None,
            "status": "completed",
            "input_refs": [assistant.item_id],
            "output_refs": [ids["artifact_id"]],
            "started_at": _iso(now),
            "finished_at": _iso(now),
        }
        artifact = {
            "artifact_id": ids["artifact_id"],
            "task_id": run.task_id,
            "producer_node_id": ids["node_id"],
            "artifact_type": "text",
            "storage_ref": commit.text,
            "summary": "",
            "is_complete": True,
            "created_at": _iso(now),
        }
        text_sha = hashlib.sha256(commit.text.encode()).hexdigest()
        projection_payload = canonicalize_agent_payload(
            {
                "event": {
                    "event_id": ids["event_id"],
                    "event_type": "agent.final_output",
                    "message_id": ids["message_id"],
                },
                "message": {
                    "content": commit.text,
                    "conversation_id": run.conversation_id,
                    "message_id": ids["message_id"],
                    "role": "assistant",
                    "task_id": run.task_id,
                },
                "receipt": {
                    **ids,
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "text_sha256": text_sha,
                },
            }
        )
        projection = projection_payload.json_text.encode()
        updated_task = {
            **task,
            "status": "completed",
            "updated_at": _iso(now),
        }
        updated_run = replace(
            run,
            status=AgentRunStatus.COMPLETED,
            next_item_sequence=run.next_item_sequence + 1,
            active_sample_item_id=assistant.item_id,
            claim_owner=None,
            claim_token=None,
            lease_expires_at=None,
            revision=run.revision + 1,
            updated_at=now,
            terminal_at=now,
        )
        response = await self._commit(
            operation="commit_final",
            run=updated_run,
            items=(assistant,),
            expected_revision=commit.expected_revision,
            expected_claim_token=commit.expected_claim_token,
            idempotency_key=f"agent-final:{run.run_id}:{text_sha}",
            task_nodes=(node,),
            artifacts=(artifact,),
            final_projection_json=projection,
            task=updated_task,
        )
        return _final_result(_run_from_wire(response["run"]), assistant, ids)

    async def reconcile_agent_run_consistency(self, run_id: str) -> AgentRun:
        run = await self.get_run(run_id)
        if run is None:
            raise AgentStorageConflict("agent_run_missing")
        if run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            return run
        items = await self.list_items(run_id)
        waiting: set[str] = set()
        invalid = False
        for result in (
            item
            for item in items
            if item.kind is AgentItemKind.TOOL_RESULT and item.state is AgentItemState.RESERVED
        ):
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
            if not result.source_call_item_id:
                invalid = True
                continue
            waiting.add(result.source_call_item_id)
        if waiting != set(run.waiting_call_item_ids):
            invalid = True
        if not invalid:
            return run
        return await self.fail_agent_run(
            run_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            safe_error_code="agent_waiting_consistency_error",
        )

    async def fail_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_error_code: str,
    ) -> AgentRun:
        return await self._commit_terminal(
            run_id,
            expected_revision=expected_revision,
            expected_claim_token=expected_claim_token,
            status=AgentRunStatus.FAILED,
            task_status="failed",
            node_status="failed",
            reason_code=safe_error_code,
        )

    async def cancel_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_reason_code: str,
    ) -> AgentRun:
        return await self._commit_terminal(
            run_id,
            expected_revision=expected_revision,
            expected_claim_token=expected_claim_token,
            status=AgentRunStatus.CANCELLED,
            task_status="cancelled",
            node_status="cancelled",
            reason_code=safe_reason_code,
        )

    async def _commit_terminal(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        status: AgentRunStatus,
        task_status: str,
        node_status: str,
        reason_code: str,
    ) -> AgentRun:
        run = await self._require_cas_run(run_id, expected_revision, expected_claim_token)
        task = await self._get_task(run.task_id)
        if task is None:
            raise AgentStorageConflict("agent_terminal_task_missing")
        now = self._now()
        node_response = await self._call(
            self._client.list_task_nodes_for_task, task_id=run.task_id
        )
        nodes = []
        for node in node_response["nodes"]:
            if node["status"] not in {"completed", "failed", "cancelled"}:
                node = {**node, "status": node_status, "finished_at": _iso(now)}
            nodes.append(node)
        updated_run = replace(
            run,
            status=status,
            waiting_call_item_ids=(),
            claim_owner=None,
            claim_token=None,
            lease_expires_at=None,
            revision=run.revision + 1,
            terminal_reason_code=reason_code,
            updated_at=now,
            terminal_at=now,
        )
        response = await self._commit(
            operation=f"commit_{status.value}",
            run=updated_run,
            items=(),
            expected_revision=expected_revision,
            expected_claim_token=expected_claim_token,
            idempotency_key=f"agent-terminal:{run.run_id}:{status.value}:{reason_code}",
            task_nodes=tuple(nodes),
            task={**task, "status": task_status, "updated_at": _iso(now)},
        )
        return _run_from_wire(response["run"])

    async def _require_cas_run(
        self, run_id: str, revision: int, claim_token: str | None
    ) -> AgentRun:
        run = await self.get_run(run_id)
        if run is None:
            raise AgentStorageConflict("agent_run_missing")
        self._validate_cas(run, revision, claim_token)
        return run

    @staticmethod
    def _validate_cas(run: AgentRun, revision: int, claim_token: str | None) -> None:
        if run.revision != revision or run.claim_token != claim_token:
            raise AgentStorageConflict("agent_run_cas_mismatch")
        if run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            raise AgentStorageConflict("agent_run_terminal")

    async def _get_task(self, task_id: str) -> dict[str, Any] | None:
        response = await self._call(self._client.get_task, task_id=task_id)
        return response["task"] if response["found"] else None

    async def _commit(
        self,
        *,
        operation: str,
        run: AgentRun,
        items: tuple[AgentItem, ...],
        expected_revision: int,
        expected_claim_token: str | None,
        idempotency_key: str,
        task_nodes: tuple[Mapping[str, Any], ...] = (),
        artifacts: tuple[Mapping[str, Any], ...] = (),
        final_projection_json: bytes | None = None,
        task: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            self._client.commit_agent_state,
            operation=operation,
            run=_run_to_wire(run),
            items=tuple(_item_to_wire(item) for item in items),
            expected_revision=expected_revision,
            expected_claim_token=expected_claim_token,
            idempotency_key=idempotency_key,
            task_nodes=task_nodes,
            artifacts=artifacts,
            final_projection_json=final_projection_json,
            task=task,
        )

    @staticmethod
    async def _call(call: Callable[..., Any], **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(call, **kwargs)
        except (RuntimeError, ValueError) as exc:
            raise AgentStorageConflict(str(exc)) from exc


def _run_to_wire(run: AgentRun) -> dict[str, Any]:
    created_at = run.created_at or datetime.now(timezone.utc)
    updated_at = run.updated_at or created_at
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "conversation_id": run.conversation_id,
        "status": run.status.value,
        "model_edition": run.binding.model_edition,
        "reasoning_effort": run.binding.reasoning_effort,
        "thinking_enabled": run.binding.thinking_enabled,
        "binding_option_digests_json": dict(run.binding.option_digests),
        "next_item_sequence": run.next_item_sequence,
        "compacted_through_sequence": run.compacted_through_sequence,
        "active_sample_item_id": run.active_sample_item_id,
        "waiting_call_item_ids": list(run.waiting_call_item_ids),
        "next_batch_call_ordinal": run.next_batch_call_ordinal,
        "claim_owner": run.claim_owner,
        "claim_token": run.claim_token,
        "lease_expires_at_ms": _ms(run.lease_expires_at),
        "revision": run.revision,
        "terminal_reason_code": run.terminal_reason_code,
        "created_at_ms": _ms(created_at),
        "updated_at_ms": _ms(updated_at),
        "terminal_at_ms": _ms(run.terminal_at),
    }


def _run_from_wire(value: Mapping[str, Any]) -> AgentRun:
    digests = value["binding_option_digests_json"]
    if isinstance(digests, (bytes, bytearray)):
        digests = json.loads(bytes(digests) or b"{}")
    return AgentRun(
        run_id=str(value["run_id"]),
        task_id=str(value["task_id"]),
        conversation_id=str(value["conversation_id"]),
        status=AgentRunStatus(str(value["status"])),
        binding=AgentModelBinding(
            model_edition=str(value["model_edition"]),
            reasoning_effort=str(value["reasoning_effort"]),
            thinking_enabled=bool(value["thinking_enabled"]),
            option_digests=digests,
        ),
        next_item_sequence=int(value["next_item_sequence"]),
        compacted_through_sequence=int(value["compacted_through_sequence"]),
        active_sample_item_id=value.get("active_sample_item_id"),
        waiting_call_item_ids=tuple(value.get("waiting_call_item_ids", ())),
        next_batch_call_ordinal=int(value["next_batch_call_ordinal"]),
        claim_owner=value.get("claim_owner"),
        claim_token=value.get("claim_token"),
        lease_expires_at=_datetime(value.get("lease_expires_at_ms")),
        revision=int(value["revision"]),
        terminal_reason_code=value.get("terminal_reason_code"),
        created_at=_datetime(value.get("created_at_ms")),
        updated_at=_datetime(value.get("updated_at_ms")),
        terminal_at=_datetime(value.get("terminal_at_ms")),
    )


def _item(
    *,
    item_id: str,
    run: AgentRun,
    sequence: int,
    kind: AgentItemKind,
    state: AgentItemState,
    payload: CanonicalAgentPayload,
    created_at: datetime,
    parent_item_id: str | None = None,
    source_call_item_id: str | None = None,
    provider_sample_id: str | None = None,
    call_ordinal: int | None = None,
    committed_at: datetime | None = None,
) -> AgentItem:
    return AgentItem(
        item_id=item_id,
        run_id=run.run_id,
        task_id=run.task_id,
        sequence=sequence,
        kind=kind,
        state=state,
        payload_json=payload.json_text,
        payload_sha256=payload.sha256,
        parent_item_id=parent_item_id,
        source_call_item_id=source_call_item_id,
        provider_sample_id=provider_sample_id,
        call_ordinal=call_ordinal,
        created_at=created_at,
        committed_at=committed_at,
    )


def _item_to_wire(item: AgentItem) -> dict[str, Any]:
    payload = item.payload_json.encode()
    return {
        "item_id": item.item_id,
        "run_id": item.run_id,
        "task_id": item.task_id,
        "sequence": item.sequence,
        "kind": item.kind.value,
        "state": item.state.value,
        "payload_json": payload,
        "payload_size_bytes": len(payload),
        "payload_sha256": item.payload_sha256,
        "parent_item_id": item.parent_item_id,
        "source_call_item_id": item.source_call_item_id,
        "provider_sample_id": item.provider_sample_id,
        "call_ordinal": item.call_ordinal,
        "created_at_ms": _ms(item.created_at),
        "committed_at_ms": _ms(item.committed_at),
    }


def _item_from_wire(value: Mapping[str, Any]) -> AgentItem:
    payload = bytes(value["payload_json"]).decode()
    return AgentItem(
        item_id=str(value["item_id"]),
        run_id=str(value["run_id"]),
        task_id=str(value["task_id"]),
        sequence=int(value["sequence"]),
        kind=AgentItemKind(str(value["kind"])),
        state=AgentItemState(str(value["state"])),
        payload_json=payload,
        payload_sha256=str(value["payload_sha256"]),
        parent_item_id=value.get("parent_item_id"),
        source_call_item_id=value.get("source_call_item_id"),
        provider_sample_id=value.get("provider_sample_id"),
        call_ordinal=value.get("call_ordinal"),
        created_at=_datetime(value.get("created_at_ms")),
        committed_at=_datetime(value.get("committed_at_ms")),
    )


def _initial_activation_item(
    run: AgentRun,
    commit: AgentUserMessageCommit,
    *,
    now: datetime,
) -> AgentItem | None:
    payload_text = commit.skill_activation_payload_json
    if payload_text is None:
        return None
    value = json.loads(payload_text)
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
        payload.json_text != payload_text
        or payload.sha256 != commit.skill_activation_payload_sha256
    ):
        raise AgentStorageConflict("agent_initial_activation_payload_invalid")
    identity = hashlib.sha256(
        f"{run.run_id}\0{capability_id}\0{revision}".encode()
    ).hexdigest()[:24]
    return _item(
        item_id=f"agent-item:{run.run_id}:skill-activation:{identity}",
        run=run,
        sequence=2,
        kind=AgentItemKind.SKILL_ACTIVATION,
        state=AgentItemState.COMMITTED,
        payload=payload,
        created_at=now,
        committed_at=now,
    )


def _validate_sidecar_initial_activation(
    items: tuple[AgentItem, ...],
    *,
    expected: AgentItem | None,
) -> AgentItem | None:
    if expected is None:
        if items:
            raise AgentStorageConflict("agent_initial_activation_presence_conflict")
        return None
    if len(items) != 1:
        raise AgentStorageConflict("agent_initial_activation_presence_conflict")
    stored = items[0]
    if (
        stored.item_id != expected.item_id
        or stored.sequence != 2
        or stored.state is not AgentItemState.COMMITTED
        or stored.payload_json != expected.payload_json
        or stored.payload_sha256 != expected.payload_sha256
    ):
        raise AgentStorageConflict("agent_initial_activation_identity_conflict")
    return stored


def _waiting_status(
    waiting: list[str], items: tuple[AgentItem, ...], updated: AgentItem
) -> AgentRunStatus:
    if not waiting:
        return AgentRunStatus.RUNNING
    by_call = {
        item.source_call_item_id: item
        for item in (*items, updated)
        if item.kind is AgentItemKind.TOOL_RESULT and item.source_call_item_id
    }
    outcomes = {json.loads(by_call[call_id].payload_json).get("outcome") for call_id in waiting}
    return (
        AgentRunStatus.WAITING_FOR_INPUT
        if AgentCallOutcomeStatus.WAITING_FOR_INPUT.value in outcomes
        else AgentRunStatus.WAITING_FOR_DEPENDENCY
    )


def _sample_assistant_item_id(run_id: str, sample_id: str) -> str:
    return f"agent-item:{run_id}:sample:{hashlib.sha256(sample_id.encode()).hexdigest()[:24]}"


def _call_item_id(run_id: str, sample_id: str, call_id: str) -> str:
    digest = hashlib.sha256(f"{sample_id}\0{call_id}".encode()).hexdigest()[:24]
    return f"agent-item:{run_id}:call:{digest}"


def _result_item_id(run_id: str, sample_id: str, call_id: str) -> str:
    digest = hashlib.sha256(f"{sample_id}\0{call_id}".encode()).hexdigest()[:24]
    return f"agent-item:{run_id}:result:{digest}"


def _call_node_id(task_id: str, sample_id: str, call_id: str) -> str:
    digest = hashlib.sha256(f"{sample_id}\0{call_id}".encode()).hexdigest()[:24]
    return f"agent-node:{task_id}:{digest}"


def _final_ids(task_id: str) -> dict[str, str]:
    return {
        "assistant_item_id": f"agent-item:{task_id}:final",
        "node_id": f"agent-node:{task_id}:final",
        "artifact_id": f"agent-artifact:{task_id}:final",
        "message_id": f"agent-message:{task_id}:final",
        "event_id": f"agent-event:{task_id}:final",
        "receipt_id": f"agent-receipt:{task_id}:final",
    }


def _final_result(
    run: AgentRun, item: AgentItem, ids: Mapping[str, str]
) -> AgentFinalOutputResult:
    return AgentFinalOutputResult(
        run=run,
        assistant_item=item,
        node_id=ids["node_id"],
        artifact_id=ids["artifact_id"],
        message_id=ids["message_id"],
        event_id=ids["event_id"],
        receipt_id=ids["receipt_id"],
    )


def _ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _datetime(value: Any) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()

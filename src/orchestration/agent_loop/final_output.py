from __future__ import annotations

import json

from .lease import AgentLeaseController, AgentLeaseHandle
from .models import (
    AgentFinalOutputCommit,
    AgentFinalOutputResult,
    AgentItemKind,
    AgentItemState,
    AgentRunStatus,
    AgentStorageConflict,
)
from .repository import AgentAtomicWriter, AgentRunRepository


class AgentFinalOutputPublisher:
    """Publish exactly one persisted final candidate without another model call."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        lease_controller: AgentLeaseController,
        transient_result_cleaner=None,
    ) -> None:
        self._runs = runs
        self._writer = writer
        self._leases = lease_controller
        self._transient_result_cleaner = transient_result_cleaner

    async def publish(
        self,
        *,
        run_id: str,
        candidate_item_id: str,
        handle: AgentLeaseHandle,
    ) -> AgentFinalOutputResult:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AgentStorageConflict("agent_final_run_missing")
        items = await self._runs.list_items(run_id)
        candidate = next(
            (item for item in items if item.item_id == candidate_item_id),
            None,
        )
        if (
            candidate is None
            or candidate.kind is not AgentItemKind.ASSISTANT_MESSAGE
            or candidate.state is not AgentItemState.COMMITTED
            or (
                run.status is not AgentRunStatus.COMPLETED
                and run.active_sample_item_id != candidate.item_id
            )
            or any(
                item.kind is AgentItemKind.TOOL_CALL
                and item.parent_item_id == candidate.item_id
                for item in items
            )
        ):
            raise AgentStorageConflict("agent_final_candidate_invalid")
        try:
            payload = json.loads(candidate.payload_json)
        except json.JSONDecodeError as exc:
            raise AgentStorageConflict("agent_final_candidate_payload_invalid") from exc
        if not isinstance(payload, dict):
            raise AgentStorageConflict("agent_final_candidate_payload_invalid")
        text = str(payload.get("text") or "")
        if not text.strip() or bool(payload.get("mixed_text_and_tool_calls")):
            raise AgentStorageConflict("agent_final_candidate_invalid")

        async def commit(_handle: AgentLeaseHandle) -> AgentFinalOutputResult:
            latest = await self._runs.get_run(run_id)
            if latest is None:
                raise AgentStorageConflict("agent_final_run_missing")
            return await self._writer.commit_agent_final_output(
                AgentFinalOutputCommit(
                    run_id=run_id,
                    expected_revision=latest.revision,
                    expected_claim_token=(
                        _handle.current.token
                        if latest.status is not AgentRunStatus.COMPLETED
                        else None
                    ),
                    text=text,
                )
            )

        if run.status is AgentRunStatus.COMPLETED:
            result = await commit(handle)
            await self._cleanup_after_final(result)
            return result
        if run.status is not AgentRunStatus.RUNNING:
            raise AgentStorageConflict("agent_final_run_not_publishable")
        result = await self._leases.run_active_phase("final_publish", handle, commit)
        await self._cleanup_after_final(result)
        return result

    async def _cleanup_after_final(self, result: AgentFinalOutputResult) -> None:
        if self._transient_result_cleaner is None:
            return
        try:
            items = await self._runs.list_items(result.run.run_id)
            self._transient_result_cleaner.cleanup_terminal(
                run=result.run,
                items=items,
            )
        except Exception:
            pass

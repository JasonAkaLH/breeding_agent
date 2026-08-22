from __future__ import annotations

import json
import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable

from src.storage.agent_payload import agent_compaction_source_digest

from .lease import AgentLeaseController, AgentLeaseHandle
from .model_port import AgentModelPort
from .models import (
    AgentCompactionCommit,
    AgentItem,
    AgentItemState,
    AgentMessage,
    AgentModelRequest,
    AgentRun,
    AgentToolChoice,
)
from .repository import AgentAtomicWriter, AgentRunRepository
from .tool_catalog import CatalogPreflightDecision, CatalogPreflightResult


Repreflight = Callable[
    [AgentRun, tuple[AgentItem, ...]],
    CatalogPreflightResult | Awaitable[CatalogPreflightResult],
]


@dataclass(frozen=True, slots=True)
class AgentCompactionOutcome:
    run: AgentRun
    items: tuple[AgentItem, ...]
    preflight: CatalogPreflightResult


class AgentCompactionService:
    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        model: AgentModelPort,
        lease_controller: AgentLeaseController,
        minimum_suffix_items: int = 2,
    ) -> None:
        if minimum_suffix_items < 1:
            raise ValueError("minimum_suffix_items must be positive")
        self._runs = runs
        self._writer = writer
        self._model = model
        self._leases = lease_controller
        self._minimum_suffix_items = minimum_suffix_items

    async def compact_until_fit(
        self,
        *,
        run_id: str,
        handle: AgentLeaseHandle,
        preflight: CatalogPreflightResult,
        repreflight: Repreflight,
    ) -> AgentCompactionOutcome:
        current = preflight
        prior_range: tuple[int, int] | None = None
        while current.decision is CatalogPreflightDecision.HISTORY_COMPACTION_REQUIRED:
            run = await self._require_run(run_id)
            items = await self._runs.list_items(run_id)
            covered = _eligible_prefix(
                run,
                items,
                minimum_suffix_items=self._minimum_suffix_items,
            )
            if not covered:
                raise RuntimeError("agent_compaction_no_eligible_range")
            covered_range = (covered[0].sequence, covered[-1].sequence)
            if covered_range == prior_range:
                raise RuntimeError("agent_compaction_no_progress")
            prior_range = covered_range
            digest = agent_compaction_source_digest(covered)
            prompt = _compaction_prompt(covered, source_digest=digest)
            sample = await self._leases.run_active_phase(
                "compaction",
                handle,
                lambda _handle: self._model.sample_agent(
                    AgentModelRequest(
                        request_id=(
                            f"agent-compaction:{run.run_id}:"
                            f"{covered_range[0]}-{covered_range[1]}:{digest[:12]}"
                        ),
                        binding=run.binding,
                        messages=(
                            AgentMessage(
                                role="system",
                                content=(
                                    "Summarize only the supplied durable Agent items. Preserve "
                                    "facts, decisions, errors and unresolved obligations. Return "
                                    "plain summary text and do not call tools."
                                ),
                            ),
                            AgentMessage(role="user", content=prompt),
                        ),
                        tools=(),
                        tool_choice=AgentToolChoice("none"),
                    )
                ),
            )
            if sample.binding != run.binding or sample.tool_calls or not sample.visible_text.strip():
                raise RuntimeError("agent_compaction_model_output_invalid")
            latest = await self._require_run(run_id)
            committed = await self._writer.commit_agent_compaction(
                AgentCompactionCommit(
                    run_id=run_id,
                    expected_revision=latest.revision,
                    expected_claim_token=handle.current.token,
                    covered_start_sequence=covered_range[0],
                    covered_end_sequence=covered_range[1],
                    source_digest=digest,
                    summary=sample.visible_text,
                )
            )
            items = await self._runs.list_items(run_id)
            next_result = repreflight(committed.run, items)
            if inspect.isawaitable(next_result):
                next_result = await next_result
            current = next_result
            if current.decision is CatalogPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE:
                raise RuntimeError("agent_tool_catalog_too_large")
        if current.decision is not CatalogPreflightDecision.FITS:
            raise RuntimeError("agent_compaction_preflight_invalid")
        return AgentCompactionOutcome(
            run=await self._require_run(run_id),
            items=await self._runs.list_items(run_id),
            preflight=current,
        )

    async def _require_run(self, run_id: str) -> AgentRun:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise RuntimeError("agent_compaction_run_missing")
        return run


def _eligible_prefix(
    run: AgentRun,
    items: tuple[AgentItem, ...],
    *,
    minimum_suffix_items: int,
) -> tuple[AgentItem, ...]:
    uncovered = tuple(
        item for item in items if item.sequence > run.compacted_through_sequence
    )
    if len(uncovered) <= minimum_suffix_items:
        return ()
    candidates = list(uncovered[:-minimum_suffix_items])
    expected = run.compacted_through_sequence + 1
    contiguous: list[AgentItem] = []
    for item in candidates:
        if item.sequence != expected or item.state is not AgentItemState.COMMITTED:
            break
        contiguous.append(item)
        expected += 1
    while contiguous and not _prefix_is_closed(tuple(contiguous), uncovered):
        contiguous.pop()
    return tuple(contiguous)


def _prefix_is_closed(
    prefix: tuple[AgentItem, ...],
    uncovered: tuple[AgentItem, ...],
) -> bool:
    covered_ids = {item.item_id for item in prefix}
    for item in prefix:
        if item.kind.value == "assistant_message":
            if any(
                candidate.parent_item_id == item.item_id
                and candidate.item_id not in covered_ids
                for candidate in uncovered
            ):
                return False
        if item.kind.value == "tool_call":
            if any(
                candidate.source_call_item_id == item.item_id
                and candidate.item_id not in covered_ids
                for candidate in uncovered
            ):
                return False
    return True


def _compaction_prompt(
    items: tuple[AgentItem, ...],
    *,
    source_digest: str,
) -> str:
    payload = {
        "covered_end_sequence": items[-1].sequence,
        "covered_start_sequence": items[0].sequence,
        "items": [
            {
                "kind": item.kind.value,
                "payload": json.loads(item.payload_json),
                "sequence": item.sequence,
            }
            for item in items
        ],
        "source_digest": source_digest,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

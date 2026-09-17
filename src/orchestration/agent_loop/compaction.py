from __future__ import annotations

import json
import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Any

from src.storage.agent_payload import agent_compaction_source_digest

from .lease import AgentLeaseController, AgentLeaseHandle
from .model_port import AgentModelPort
from .context_preflight import (
    AgentContextCandidate,
    AgentContextCandidateBuilder,
    AgentContextPreflightDecision,
    compaction_prefix_is_closed,
    eligible_compaction_prefix,
)
from .models import (
    AgentCompactionCommit,
    AgentItem,
    AgentItemKind,
    AgentMessage,
    AgentModelRequest,
    AgentRun,
    AgentToolChoice,
)
from .repository import AgentAtomicWriter, AgentRunRepository
from .observability import AgentMetricsRecorder


Repreflight = Callable[
    [AgentRun, tuple[AgentItem, ...]],
    AgentContextCandidate | Awaitable[AgentContextCandidate],
]


@dataclass(frozen=True, slots=True)
class AgentCompactionOutcome:
    run: AgentRun
    items: tuple[AgentItem, ...]
    candidate: AgentContextCandidate

    @property
    def preflight(self):
        return self.candidate.preflight


class AgentCompactionService:
    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        model: AgentModelPort,
        lease_controller: AgentLeaseController,
        candidate_builder: AgentContextCandidateBuilder,
        transient_result_resolver: Any | None = None,
        transient_result_cleaner: Any | None = None,
        metrics_recorder: AgentMetricsRecorder | None = None,
        minimum_suffix_items: int = 2,
    ) -> None:
        if minimum_suffix_items < 1:
            raise ValueError("minimum_suffix_items must be positive")
        self._runs = runs
        self._writer = writer
        self._model = model
        self._leases = lease_controller
        self._candidate_builder = candidate_builder
        self._transient_result_resolver = transient_result_resolver
        self._transient_result_cleaner = transient_result_cleaner
        self._metrics = metrics_recorder
        self._minimum_suffix_items = minimum_suffix_items

    async def compact_until_fit(
        self,
        *,
        run_id: str,
        handle: AgentLeaseHandle,
        candidate: AgentContextCandidate,
        repreflight: Repreflight,
        force: bool = False,
    ) -> AgentCompactionOutcome:
        try:
            outcome = await self._compact_until_fit(
                run_id=run_id,
                handle=handle,
                candidate=candidate,
                repreflight=repreflight,
                force=force,
            )
        except Exception as exc:
            error_code = str(exc)
            if error_code == "agent_context_required_segments_too_large":
                metric_outcome = "required_too_large"
            elif error_code == "agent_compaction_no_progress":
                metric_outcome = "no_progress"
            else:
                metric_outcome = "failed"
            self._record_compaction(metric_outcome)
            raise
        self._record_compaction("completed")
        return outcome

    async def _compact_until_fit(
        self,
        *,
        run_id: str,
        handle: AgentLeaseHandle,
        candidate: AgentContextCandidate,
        repreflight: Repreflight,
        force: bool,
    ) -> AgentCompactionOutcome:
        current = candidate
        prior_range: tuple[int, int] | None = None
        force_next = force
        while (
            force_next
            or current.preflight.decision
            is AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED
        ):
            run = await self._require_run(run_id)
            items = await self._runs.list_items(run_id)
            covered = eligible_compaction_prefix(
                run,
                items,
                minimum_suffix_items=self._minimum_suffix_items,
            )
            if not covered:
                raise RuntimeError("agent_context_required_segments_too_large")
            request, covered = await self._largest_fitting_request(
                run=run,
                all_items=items,
                covered=covered,
                token_limit=current.preflight.total_context_limit_tokens,
            )
            covered_range = (covered[0].sequence, covered[-1].sequence)
            if covered_range == prior_range:
                raise RuntimeError("agent_compaction_no_progress")
            prior_range = covered_range
            digest = agent_compaction_source_digest(covered)
            sample = await self._leases.run_active_phase(
                "compaction",
                handle,
                lambda _handle: self._model.sample_agent(request),
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
            if self._transient_result_cleaner is not None:
                try:
                    self._transient_result_cleaner.cleanup_covered(
                        run=committed.run,
                        items=items,
                        covered_end_sequence=covered_range[1],
                    )
                except Exception:
                    pass
            items = await self._runs.list_items(run_id)
            next_result = repreflight(committed.run, items)
            if inspect.isawaitable(next_result):
                next_result = await next_result
            current = next_result
            force_next = False
            if (
                current.preflight.decision
                is AgentContextPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE
            ):
                raise RuntimeError("agent_context_required_segments_too_large")
        if current.preflight.decision is not AgentContextPreflightDecision.FITS:
            raise RuntimeError("agent_compaction_preflight_invalid")
        return AgentCompactionOutcome(
            run=await self._require_run(run_id),
            items=await self._runs.list_items(run_id),
            candidate=current,
        )

    def _record_compaction(self, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.record(
                "agent_context_compactions_total",
                outcome=outcome,
            )

    async def _largest_fitting_request(
        self,
        *,
        run: AgentRun,
        all_items: tuple[AgentItem, ...],
        covered: tuple[AgentItem, ...],
        token_limit: int,
    ) -> tuple[AgentModelRequest, tuple[AgentItem, ...]]:
        current = covered
        while current:
            source_digest = agent_compaction_source_digest(current)
            prompt = _compaction_prompt(
                run,
                current,
                source_digest=source_digest,
                transient_result_resolver=self._transient_result_resolver,
            )
            if prompt is not None:
                request = AgentModelRequest(
                    request_id=(
                        f"agent-compaction:{run.run_id}:"
                        f"{current[0].sequence}-{current[-1].sequence}:"
                        f"{source_digest[:12]}"
                    ),
                    binding=run.binding,
                    messages=(
                        AgentMessage(
                            role="system",
                            content=(
                                "Summarize only the supplied durable Agent items. "
                                "Preserve facts, decisions, errors and unresolved "
                                "obligations. Return plain summary text and do not "
                                "call tools."
                            ),
                        ),
                        AgentMessage(role="user", content=prompt),
                    ),
                    tools=(),
                    tool_choice=AgentToolChoice("none"),
                )
                if await self._candidate_builder.count_request(request) <= token_limit:
                    return request, current
            current = _previous_closed_prefix(current, all_items)
        raise RuntimeError("agent_context_required_segments_too_large")

    async def _require_run(self, run_id: str) -> AgentRun:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise RuntimeError("agent_compaction_run_missing")
        return run


def _compaction_prompt(
    run: AgentRun,
    items: tuple[AgentItem, ...],
    *,
    source_digest: str,
    transient_result_resolver: Any | None,
) -> str | None:
    source_items = []
    calls = {
        item.item_id: item
        for item in items
        if item.kind is AgentItemKind.TOOL_CALL
    }
    for item in items:
        item_payload = json.loads(item.payload_json)
        if (
            item.sequence == 1
            and item.kind is AgentItemKind.USER_MESSAGE
            and isinstance(item_payload, dict)
            and "context_budget" in item_payload
        ):
            continue
        if (
            item.kind is AgentItemKind.TOOL_RESULT
            and isinstance(item_payload, dict)
            and _is_transient_result_payload(item_payload)
        ):
            call = calls.get(str(item.source_call_item_id))
            if call is None or transient_result_resolver is None:
                raise RuntimeError("agent_transient_skill_result_unavailable")
            item_payload = transient_result_resolver.resolve_tool_result(
                run=run,
                call_item=call,
                result_item=item,
                durable_payload=item_payload,
            )
        source_items.append(
            {
                "kind": item.kind.value,
                "payload": item_payload,
                "sequence": item.sequence,
            }
        )
    if not source_items:
        return None
    payload = {
        "covered_end_sequence": items[-1].sequence,
        "covered_start_sequence": items[0].sequence,
        "items": source_items,
        "source_digest": source_digest,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _previous_closed_prefix(
    current: tuple[AgentItem, ...],
    all_items: tuple[AgentItem, ...],
) -> tuple[AgentItem, ...]:
    candidate = current[:-1]
    while candidate and not compaction_prefix_is_closed(candidate, all_items):
        candidate = candidate[:-1]
    return candidate


def _is_transient_result_payload(payload: dict[str, Any]) -> bool:
    safe_result = payload.get("safe_result")
    return bool(
        isinstance(safe_result, dict)
        and safe_result.get("projection_mode") == "transient_staged"
    )

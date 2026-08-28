from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .context import AgentContextBuilder
from .context_budget import AgentContextBudget
from .models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentMessage,
    AgentModelBinding,
    AgentModelRequest,
    AgentRun,
    AgentToolChoice,
    AgentToolDescriptor,
)
from .tool_catalog import AgentToolCatalog


class AgentContextPreflightDecision(StrEnum):
    FITS = "fits"
    HISTORY_COMPACTION_REQUIRED = "history_compaction_required"
    FATAL_REQUIRED_SEGMENTS_TOO_LARGE = "fatal_required_segments_too_large"


@dataclass(frozen=True, slots=True)
class AgentContextPreflightResult:
    decision: AgentContextPreflightDecision
    required_tokens: int
    history_tokens: int
    transient_tokens: int
    tool_tokens: int
    total_tokens: int
    total_context_limit_tokens: int
    eligible_closed_history: bool


@dataclass(frozen=True, slots=True)
class AgentContextCandidate:
    request: AgentModelRequest
    preflight: AgentContextPreflightResult


TokenCounter = Callable[
    [Sequence[str], AgentModelBinding], int | Awaitable[int]
]


class AgentContextCandidateBuilder:
    """Build one real model request, then classify and count it once."""

    def __init__(
        self,
        *,
        context_builder: AgentContextBuilder,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._context_builder = context_builder
        self._count = token_counter or _count_with_bound_model

    async def build(
        self,
        *,
        run: AgentRun,
        items: tuple[AgentItem, ...],
        catalog: AgentToolCatalog,
        trusted_facts: tuple[str, ...] = (),
        current_user_input: str | None = None,
        tool_choice: AgentToolChoice | None = None,
    ) -> AgentContextCandidate:
        budget = _run_context_budget(items)
        request = self._context_builder.build(
            run=run,
            items=items,
            catalog=catalog,
            trusted_facts=trusted_facts,
            current_user_input=current_user_input,
            tool_choice=tool_choice,
        )
        required_call_ids = _unconsumed_transient_provider_call_ids(items)
        required_fragments: list[str] = [_framing_fragment()]
        history_fragments: list[str] = []
        transient_fragments: list[str] = []
        for message in request.messages:
            fragment = _message_fragment(message)
            is_transient = _message_contains_full_transient_result(message)
            if _message_is_required(message, required_call_ids):
                required_fragments.append(fragment)
            else:
                history_fragments.append(fragment)
            if is_transient:
                transient_fragments.append(fragment)
        tool_fragments = [
            _tool_fragment(tool) for tool in request.tools
        ] + [_tool_choice_fragment(request.tool_choice)]
        required_tokens = await self._count_fragments(
            required_fragments, request.binding
        )
        history_tokens = await self._count_fragments(
            history_fragments, request.binding
        )
        transient_tokens = await self._count_fragments(
            transient_fragments, request.binding
        )
        tool_tokens = await self._count_fragments(
            tool_fragments, request.binding
        )
        total_tokens = required_tokens + history_tokens + tool_tokens
        eligible_prefix = eligible_compaction_prefix(
            run,
            items,
            minimum_suffix_items=2,
        )
        eligible_closed_history = any(
            item.kind is not AgentItemKind.USER_MESSAGE
            for item in eligible_prefix
        )
        required_total = required_tokens + tool_tokens
        limit = budget.total_context_limit_tokens
        if total_tokens <= limit:
            decision = AgentContextPreflightDecision.FITS
        elif required_total > limit or not eligible_closed_history:
            decision = (
                AgentContextPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE
            )
        else:
            decision = (
                AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED
            )
        return AgentContextCandidate(
            request=request,
            preflight=AgentContextPreflightResult(
                decision=decision,
                required_tokens=required_tokens,
                history_tokens=history_tokens,
                transient_tokens=transient_tokens,
                tool_tokens=tool_tokens,
                total_tokens=total_tokens,
                total_context_limit_tokens=limit,
                eligible_closed_history=eligible_closed_history,
            ),
        )

    async def count_request(self, request: AgentModelRequest) -> int:
        fragments = [
            _framing_fragment(),
            *(_message_fragment(message) for message in request.messages),
            *(_tool_fragment(tool) for tool in request.tools),
            _tool_choice_fragment(request.tool_choice),
        ]
        return await self._count_fragments(fragments, request.binding)

    async def _count_fragments(
        self,
        fragments: Sequence[str],
        binding: AgentModelBinding,
    ) -> int:
        if not fragments:
            return 0
        value = self._count(fragments, binding)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("agent_context_token_count_invalid")
        return value


async def _count_with_bound_model(
    fragments: Sequence[str],
    binding: AgentModelBinding,
) -> int:
    from src.integrations.llm_client import load_config
    from src.integrations.model_editions import config_for_model_edition
    from src.integrations.token_counter import (
        get_num_of_tokens_from_messages_async,
    )

    config = config_for_model_edition(load_config(), binding.model_edition)
    return await get_num_of_tokens_from_messages_async(
        fragments,
        config=config,
    )


def agent_run_context_budget(
    items: tuple[AgentItem, ...],
) -> AgentContextBudget | None:
    user = next(
        (
            item
            for item in sorted(items, key=lambda value: value.sequence)
            if item.sequence == 1
            and item.kind is AgentItemKind.USER_MESSAGE
            and item.state is AgentItemState.COMMITTED
        ),
        None,
    )
    if user is None:
        return None
    payload = json.loads(user.payload_json)
    if not isinstance(payload, Mapping):
        raise ValueError("agent_context_budget_invalid")
    if "context_budget" not in payload:
        return None
    return AgentContextBudget.from_payload(payload["context_budget"])


def _run_context_budget(items: tuple[AgentItem, ...]) -> AgentContextBudget:
    budget = agent_run_context_budget(items)
    if budget is None:
        raise ValueError("agent_context_budget_missing")
    return budget


def _unconsumed_transient_provider_call_ids(
    items: tuple[AgentItem, ...],
) -> frozenset[str]:
    ordered = tuple(sorted(items, key=lambda value: value.sequence))
    calls = {
        item.item_id: item
        for item in ordered
        if item.kind is AgentItemKind.TOOL_CALL
    }
    required: set[str] = set()
    for result in ordered:
        if (
            result.kind is not AgentItemKind.TOOL_RESULT
            or result.state is not AgentItemState.COMMITTED
            or not _item_is_transient_result(result)
            or any(
                candidate.kind is AgentItemKind.ASSISTANT_MESSAGE
                and candidate.sequence > result.sequence
                for candidate in ordered
            )
        ):
            continue
        call = calls.get(str(result.source_call_item_id))
        if call is None:
            raise ValueError("agent_context_tool_result_source_missing")
        call_payload = json.loads(call.payload_json)
        provider_call_id = (
            call_payload.get("call_id")
            if isinstance(call_payload, Mapping)
            else None
        )
        if not isinstance(provider_call_id, str) or not provider_call_id:
            raise ValueError("agent_context_provider_call_id_missing")
        required.add(provider_call_id)
    return frozenset(required)


def _item_is_transient_result(item: AgentItem) -> bool:
    payload = json.loads(item.payload_json)
    safe_result = payload.get("safe_result") if isinstance(payload, Mapping) else None
    return bool(
        isinstance(safe_result, Mapping)
        and safe_result.get("projection_revision") == "skill-result-v2"
        and safe_result.get("projection_mode") == "transient_staged"
    )


def _message_is_required(
    message: AgentMessage,
    required_call_ids: frozenset[str],
) -> bool:
    if message.role in {"system", "user"}:
        return True
    if message.role == "tool":
        return str(message.tool_call_id) in required_call_ids
    return any(call.call_id in required_call_ids for call in message.tool_calls)


def _message_contains_full_transient_result(message: AgentMessage) -> bool:
    if message.role != "tool" or not isinstance(message.content, str):
        return False
    try:
        payload = json.loads(message.content)
    except json.JSONDecodeError:
        return False
    safe_result = payload.get("safe_result") if isinstance(payload, Mapping) else None
    return bool(
        isinstance(safe_result, Mapping)
        and safe_result.get("schema") == "maf.agent.skill_result_full.v1"
    )


def _message_fragment(message: AgentMessage) -> str:
    payload: dict[str, Any] = {
        "content": message.content,
        "role": message.role,
    }
    if message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "function": {
                    "arguments": call.arguments_json,
                    "name": call.provider_safe_name,
                },
                "id": call.call_id,
                "type": "function",
            }
            for call in message.tool_calls
        ]
    return _canonical_fragment({"message": payload})


def _tool_fragment(tool: AgentToolDescriptor) -> str:
    return _canonical_fragment(
        {
            "tool": {
                "function": {
                    "description": tool.description,
                    "name": tool.provider_safe_name,
                    "parameters": dict(tool.input_schema),
                },
                "type": "function",
            }
        }
    )


def _tool_choice_fragment(choice: AgentToolChoice) -> str:
    value: object = choice.mode
    if choice.mode == "required":
        value = {
            "function": {"name": choice.required_name},
            "type": "function",
        }
    return _canonical_fragment({"tool_choice": value})


def _framing_fragment() -> str:
    return _canonical_fragment(
        {"framing": "maf.agent.model_request.preflight.v1"}
    )


def _canonical_fragment(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def eligible_compaction_prefix(
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
    while contiguous and not compaction_prefix_is_closed(
        tuple(contiguous), uncovered
    ):
        contiguous.pop()
    return tuple(contiguous)


def compaction_prefix_is_closed(
    prefix: tuple[AgentItem, ...],
    uncovered: tuple[AgentItem, ...],
) -> bool:
    covered_ids = {item.item_id for item in prefix}
    for item in prefix:
        if item.kind is AgentItemKind.ASSISTANT_MESSAGE and any(
            candidate.parent_item_id == item.item_id
            and candidate.item_id not in covered_ids
            for candidate in uncovered
        ):
            return False
        if item.kind is AgentItemKind.TOOL_CALL and any(
            candidate.source_call_item_id == item.item_id
            and candidate.item_id not in covered_ids
            for candidate in uncovered
        ):
            return False
    return True

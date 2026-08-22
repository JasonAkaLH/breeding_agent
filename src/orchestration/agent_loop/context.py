from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import (
    AgentItem,
    AgentItemKind,
    AgentMessage,
    AgentModelRequest,
    AgentRun,
    AgentToolCall,
    AgentToolChoice,
)
from .tool_catalog import AgentToolCatalog


@dataclass(frozen=True, slots=True)
class AgentContextRules:
    stable_rules: str
    safe_tool_rules: str
    final_guard: str


class AgentContextBuilder:
    def __init__(self, rules: AgentContextRules) -> None:
        self._rules = rules

    def build(
        self,
        *,
        run: AgentRun,
        items: tuple[AgentItem, ...],
        catalog: AgentToolCatalog,
        trusted_facts: tuple[str, ...] = (),
        tool_choice: AgentToolChoice | None = None,
    ) -> AgentModelRequest:
        messages: list[AgentMessage] = [
            AgentMessage(role="system", content=self._rules.stable_rules),
            AgentMessage(role="developer", content=self._rules.safe_tool_rules),
        ]
        ordered_items = tuple(sorted(items, key=lambda value: value.sequence))
        for item in ordered_items:
            message = _message_from_item(item, all_items=ordered_items)
            if message is not None:
                messages.append(message)
        if trusted_facts:
            messages.append(
                AgentMessage(
                    role="developer",
                    content=json.dumps(
                        {"trusted_facts": list(trusted_facts)},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        messages.append(AgentMessage(role="system", content=self._rules.final_guard))
        return AgentModelRequest(
            request_id=f"agent-sample:{run.run_id}:r{run.revision}",
            binding=run.binding,
            messages=tuple(messages),
            tools=catalog.tools,
            tool_choice=tool_choice or AgentToolChoice(),
        )


def _message_from_item(
    item: AgentItem,
    *,
    all_items: tuple[AgentItem, ...],
) -> AgentMessage | None:
    payload = _payload(item)
    if item.kind is AgentItemKind.USER_MESSAGE:
        return AgentMessage(role="user", content=str(payload.get("text") or ""))
    if item.kind is AgentItemKind.ASSISTANT_MESSAGE:
        text = str(payload.get("text") or "")
        calls = tuple(
            _tool_call(call)
            for call in sorted(
                (
                    candidate
                    for candidate in all_items
                    if candidate.kind is AgentItemKind.TOOL_CALL
                    and candidate.parent_item_id == item.item_id
                ),
                key=lambda candidate: candidate.call_ordinal or 0,
            )
        )
        return AgentMessage(
            role="assistant",
            content=None if calls else (text or None),
            tool_calls=calls,
        )
    if item.kind is AgentItemKind.TOOL_CALL:
        return None
    if item.kind is AgentItemKind.TOOL_RESULT and item.state.value == "committed":
        source_call = next(
            (
                candidate
                for candidate in all_items
                if candidate.item_id == item.source_call_item_id
                and candidate.kind is AgentItemKind.TOOL_CALL
            ),
            None,
        )
        if source_call is None:
            raise ValueError("agent_context_tool_result_source_missing")
        provider_call_id = str(_payload(source_call).get("call_id") or "")
        if not provider_call_id:
            raise ValueError("agent_context_provider_call_id_missing")
        return AgentMessage(
            role="tool",
            tool_call_id=provider_call_id,
            content=json.dumps(
                {
                    "outcome": payload.get("outcome"),
                    "safe_error_code": payload.get("safe_error_code"),
                    "safe_result": payload.get("safe_result"),
                    "artifact_refs": payload.get("artifact_refs", []),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if item.kind in {AgentItemKind.SKILL_ACTIVATION, AgentItemKind.CONTEXT_SUMMARY}:
        return AgentMessage(role="developer", content=item.payload_json.rstrip("\n"))
    if item.kind is AgentItemKind.CONTINUATION:
        return AgentMessage(role="user", content=item.payload_json.rstrip("\n"))
    return None


def _payload(item: AgentItem) -> dict[str, Any]:
    value = json.loads(item.payload_json)
    return value if isinstance(value, dict) else {}


def _tool_call(item: AgentItem) -> AgentToolCall:
    payload = _payload(item)
    arguments = payload.get("arguments_json")
    return AgentToolCall(
        call_id=str(payload.get("call_id") or item.item_id),
        provider_safe_name=str(payload.get("provider_safe_name") or ""),
        arguments_json=(
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments or {}, separators=(",", ":"), sort_keys=True)
        ),
        ordinal=item.call_ordinal or 0,
    )

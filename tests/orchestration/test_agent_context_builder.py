from __future__ import annotations

import hashlib
import json
import unittest

from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentToolDescriptor,
)
from src.orchestration.agent_loop.tool_catalog import AgentToolCatalog


def _item(
    item_id: str,
    sequence: int,
    kind: AgentItemKind,
    payload: dict,
    **values,
) -> AgentItem:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return AgentItem(
        item_id,
        "run-1",
        "task-1",
        sequence,
        kind,
        values.pop("state", AgentItemState.COMMITTED),
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        **values,
    )


class AgentContextBuilderTest(unittest.TestCase):
    def test_recovered_summary_and_skill_activation_keep_order_and_become_system_messages(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=4,
            compacted_through_sequence=1,
            revision=3,
        )
        old_user = _item("user", 1, AgentItemKind.USER_MESSAGE, {"text": "old"})
        summary = _item(
            "summary",
            2,
            AgentItemKind.CONTEXT_SUMMARY,
            {"covered_end_sequence": 1, "summary": "compressed"},
        )
        activation = _item(
            "activation",
            3,
            AgentItemKind.SKILL_ACTIVATION,
            {"skill_id": "skill.one"},
        )

        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(activation, old_user, summary),
            catalog=AgentToolCatalog((), {}),
        )

        self.assertEqual(
            [(message.role, message.content) for message in request.messages],
            [
                ("system", "stable"),
                ("system", "tool rules"),
                ("system", '{"covered_end_sequence":1,"summary":"compressed"}'),
                ("system", '{"skill_id":"skill.one"}'),
                ("system", "final guard"),
            ],
        )

    def test_multi_call_sample_is_rebuilt_as_one_assistant_message_then_ordered_results(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
            next_item_sequence=6,
            revision=3,
        )
        assistant = _item("assistant", 1, AgentItemKind.ASSISTANT_MESSAGE, {"text": ""})
        call_two = _item(
            "call-2",
            4,
            AgentItemKind.TOOL_CALL,
            {"call_id": "provider-2", "provider_safe_name": "tool_two", "arguments_json": "{}"},
            parent_item_id="assistant",
            call_ordinal=1,
        )
        call_one = _item(
            "call-1",
            2,
            AgentItemKind.TOOL_CALL,
            {"call_id": "provider-1", "provider_safe_name": "tool_one", "arguments_json": "{}"},
            parent_item_id="assistant",
            call_ordinal=0,
        )
        result_one = _item(
            "result-1",
            3,
            AgentItemKind.TOOL_RESULT,
            {"outcome": "completed", "safe_result": {"value": 1}},
            parent_item_id="assistant",
            source_call_item_id="call-1",
        )
        result_two = _item(
            "result-2",
            5,
            AgentItemKind.TOOL_RESULT,
            {"outcome": "failed", "safe_error_code": "ordinary"},
            parent_item_id="assistant",
            source_call_item_id="call-2",
        )
        catalog = AgentToolCatalog(
            (
                AgentToolDescriptor.for_capability(
                    "skill.one", description="one", input_schema={"type": "object"}
                ),
            ),
            {},
        )
        request = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=run,
            items=(assistant, call_two, call_one, result_one, result_two),
            catalog=catalog,
            trusted_facts=("fact",),
        )

        assistant_messages = [message for message in request.messages if message.role == "assistant"]
        self.assertEqual(len(assistant_messages), 1)
        self.assertEqual(
            [call.call_id for call in assistant_messages[0].tool_calls],
            ["provider-1", "provider-2"],
        )
        tool_messages = [message for message in request.messages if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["provider-1", "provider-2"],
        )
        self.assertEqual(request.messages[0].content, "stable")
        self.assertEqual(request.messages[-1].content, "final guard")
        self.assertEqual(
            [(message.role, message.content) for message in request.messages if message.role == "system"],
            [
                ("system", "stable"),
                ("system", "tool rules"),
                ("system", '{"trusted_facts":["fact"]}'),
                ("system", "final guard"),
            ],
        )
        self.assertNotIn("developer", {message.role for message in request.messages})
        self.assertEqual(request.binding, run.binding)

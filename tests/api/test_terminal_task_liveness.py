from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from src.api.runtime import ApiRuntime
from src.core.enums import InterruptStatus, TaskStatus
from src.core.models import Interrupt, Task


class TerminalTaskLivenessTest(IsolatedAsyncioTestCase):
    async def test_list_interrupts_reads_history_without_slot_recovery(self) -> None:
        task = Task(
            task_id="task-terminal",
            conversation_id="conv-terminal",
            root_message_id="msg-terminal",
            status=TaskStatus.FAILED,
        )
        interrupt = Interrupt(
            interrupt_id="interrupt-terminal",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id="node-terminal",
            source_agent="skill.legacy",
            source_message_id=task.root_message_id,
            question="legacy question",
            reason_code="missing_input",
            status=InterruptStatus.OPEN,
        )
        storage = SimpleNamespace(
            get_task=AsyncMock(return_value=task),
            list_interrupts_for_task=AsyncMock(return_value=[interrupt]),
            list_slot_collections_for_task=AsyncMock(
                side_effect=AssertionError("terminal Task must not inspect Slot state")
            ),
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime._skill_runtime_state = SimpleNamespace(
            catalog_for_revision=lambda _revision: (_ for _ in ()).throw(
                AssertionError("terminal Task must not resolve Skill revision")
            )
        )
        runtime._schedule_v2_slot_resume = AsyncMock(
            side_effect=AssertionError("terminal Task must not schedule resume")
        )

        result = await runtime.list_interrupts(task.task_id)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["interrupt_id"], interrupt.interrupt_id)
        self.assertEqual(result[0]["status"], "open")
        storage.list_slot_collections_for_task.assert_not_awaited()
        runtime._schedule_v2_slot_resume.assert_not_awaited()

    async def test_all_terminal_statuses_reject_answers_before_first_mutation(self) -> None:
        for status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            with self.subTest(status=status):
                task = Task(
                    task_id=f"task-{status}",
                    conversation_id="conv-terminal",
                    root_message_id="msg-terminal",
                    status=status,
                )
                storage = SimpleNamespace(
                    get_task=AsyncMock(return_value=task),
                    get_interrupt=AsyncMock(
                        side_effect=AssertionError(
                            "terminal Task must reject before reading continuation authority"
                        )
                    ),
                    reserve_message_identity=AsyncMock(
                        side_effect=AssertionError(
                            "terminal Task must not reserve answer messages"
                        )
                    ),
                    save_interrupt_answer=AsyncMock(
                        side_effect=AssertionError(
                            "terminal Task must not save answers"
                        )
                    ),
                    accept_mcp_tool_approval=AsyncMock(
                        side_effect=AssertionError(
                            "terminal Task must not write MCP grants"
                        )
                    ),
                )
                runtime = object.__new__(ApiRuntime)
                runtime.storage = storage

                with self.assertRaisesRegex(ValueError, "Task is terminal"):
                    await runtime._answer_interrupt_unlocked(
                        task.task_id,
                        "interrupt-terminal",
                        {"mcp_tool_approval": "allow_once"},
                        source_message_id="answer-message",
                    )

                storage.get_interrupt.assert_not_awaited()
                storage.reserve_message_identity.assert_not_awaited()
                storage.save_interrupt_answer.assert_not_awaited()
                storage.accept_mcp_tool_approval.assert_not_awaited()

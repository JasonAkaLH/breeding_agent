from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.enums import (
    InterruptStatus,
    MessageRole,
    NodeStatus,
    TaskStatus,
)
from src.core.models import (
    Conversation,
    Interrupt,
    Message,
    SubmissionHandoffState,
    Task,
    TaskNode,
)
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from tests.api.support import APITestCase


class SkillRevisionRecoveryAPITest(APITestCase):
    async def _seed_task(
        self,
        suffix: str,
        *,
        status: TaskStatus = TaskStatus.RUNNING,
        with_run: bool = True,
    ) -> tuple[Task, AgentRun | None]:
        task = Task(
            task_id=f"task-{suffix}",
            conversation_id=f"conv-{suffix}",
            root_message_id=f"msg-{suffix}",
            status=status,
        )
        await self.runtime.storage.save_conversation(
            Conversation(
                conversation_id=task.conversation_id,
                username="alice",
                current_task_id=task.task_id,
            )
        )
        await self.runtime.storage.save_message(
            Message(
                message_id=task.root_message_id,
                conversation_id=task.conversation_id,
                role=MessageRole.USER,
                content="recover this task",
                task_id=task.task_id,
            )
        )
        await self.runtime.storage.save_task(task)
        run = None
        if with_run:
            run = await self.runtime.agent_run_repository.create_run(
                AgentRun(
                    run_id=f"agent-run:{task.task_id}",
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    status=AgentRunStatus.RUNNING,
                    binding=AgentModelBinding("api-test"),
                )
            )
        return task, run

    @staticmethod
    def _authority(
        task: Task,
        revision: object,
        *,
        kind: str = "agent_run",
        handoff_state: SubmissionHandoffState = SubmissionHandoffState.HANDED_OFF,
        identity: str | None = None,
    ) -> SimpleNamespace:
        if identity is None and handoff_state is SubmissionHandoffState.HANDED_OFF:
            identity = f"agent-run:{task.task_id}"
        return SimpleNamespace(
            preparation=SimpleNamespace(
                handoff_state=handoff_state,
                handoff_identity=identity,
            ),
            prepared={
                "planned_handoff_kind": kind,
                "bundle_revisions": {"skill_bundle_revision": revision},
            },
            agent_context=(object() if kind == "agent_run" else None),
        )

    async def test_recoverable_runs_with_non_v2_revision_are_terminalized(self) -> None:
        cases = (
            (
                "legacy",
                "skillrev-000001-aaaaaaaaaaaa",
                "agent_skill_bundle_revision_retired",
            ),
            (
                "invalid",
                "skillrev-forged",
                "agent_skill_bundle_revision_invalid",
            ),
            (
                "unavailable",
                "skillrev-v2-" + ("a" * 64),
                "agent_skill_bundle_revision_unavailable",
            ),
        )
        for suffix, revision, expected_reason in cases:
            with self.subTest(suffix=suffix):
                task, _run = await self._seed_task(suffix)
                self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
                    load_authority=AsyncMock(
                        return_value=self._authority(task, revision)
                    )
                )

                await self.runtime._reconcile_skill_recovery_task(task.task_id)

                stored_run = await self.runtime.agent_run_repository.get_run_for_task(
                    task.task_id
                )
                stored_task = await self.runtime.storage.get_task(task.task_id)
                assert stored_run is not None and stored_task is not None
                self.assertEqual(stored_run.status, AgentRunStatus.FAILED)
                self.assertEqual(stored_run.terminal_reason_code, expected_reason)
                self.assertEqual(stored_task.status, TaskStatus.FAILED)
                conversation = await self.runtime.storage.get_conversation(
                    task.conversation_id
                )
                assert conversation is not None
                self.assertIsNone(conversation.current_task_id)

    async def test_cancelling_legacy_run_is_cancelled(self) -> None:
        task, _run = await self._seed_task(
            "cancelling",
            status=TaskStatus.CANCELLING,
        )
        self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load_authority=AsyncMock(
                return_value=self._authority(
                    task,
                    "skillrev-000001-aaaaaaaaaaaa",
                )
            )
        )

        await self.runtime._reconcile_skill_recovery_task(task.task_id)

        stored_run = await self.runtime.agent_run_repository.get_run_for_task(
            task.task_id
        )
        stored_task = await self.runtime.storage.get_task(task.task_id)
        assert stored_run is not None and stored_task is not None
        self.assertEqual(stored_run.status, AgentRunStatus.CANCELLED)
        self.assertEqual(stored_task.status, TaskStatus.CANCELLED)

    async def test_exact_available_v2_run_remains_recoverable(self) -> None:
        task, original = await self._seed_task("exact")
        self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load_authority=AsyncMock(
                return_value=self._authority(
                    task,
                    self.runtime._skill_runtime_state.active_revision,
                )
            )
        )

        await self.runtime._reconcile_skill_recovery_task(task.task_id)

        self.assertEqual(
            await self.runtime.agent_run_repository.get_run_for_task(task.task_id),
            original,
        )
        self.assertEqual(
            (await self.runtime.storage.get_task(task.task_id)).status,
            TaskStatus.RUNNING,
        )

    async def test_handed_off_legacy_interrupt_becomes_inactive_history(self) -> None:
        task, _run = await self._seed_task("interrupt", with_run=False)
        identity = f"submission-interrupt:v1:{task.task_id}:{'b' * 64}"
        node = TaskNode(
            node_id=f"node-{task.task_id}",
            task_id=task.task_id,
            capability_id="agent.file_selection",
            status=NodeStatus.WAITING_FOR_INPUT,
        )
        interrupt = Interrupt(
            interrupt_id=identity,
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            node_id=node.node_id,
            source_agent=node.capability_id,
            source_message_id=task.root_message_id,
            question="choose a file",
            reason_code="file_selection_required",
            status=InterruptStatus.OPEN,
        )
        await self.runtime.storage.save_task_node(node)
        await self.runtime.storage.save_interrupt(interrupt)
        self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load_authority=AsyncMock(
                return_value=self._authority(
                    task,
                    "skillrev-000001-aaaaaaaaaaaa",
                    kind="interrupt",
                    identity=identity,
                )
            )
        )

        await self.runtime._reconcile_skill_recovery_task(task.task_id)

        stored_task = await self.runtime.storage.get_task(task.task_id)
        self.assertEqual(stored_task.status, TaskStatus.FAILED)
        self.assertEqual(await self.runtime.storage.get_interrupt(identity), interrupt)
        self.assertEqual(await self.runtime.storage.get_task_node(node.node_id), node)

    async def test_pending_legacy_without_run_is_left_for_handoff_recovery(self) -> None:
        task, _run = await self._seed_task("pending", with_run=False)
        self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load_authority=AsyncMock(
                return_value=self._authority(
                    task,
                    "skillrev-000001-aaaaaaaaaaaa",
                    handoff_state=SubmissionHandoffState.PENDING,
                    identity=None,
                )
            )
        )

        await self.runtime._reconcile_skill_recovery_task(task.task_id)

        self.assertEqual(
            (await self.runtime.storage.get_task(task.task_id)).status,
            TaskStatus.RUNNING,
        )

    async def test_handed_off_agent_authority_without_run_blocks_startup(self) -> None:
        task, _run = await self._seed_task("missing-run", with_run=False)
        self.runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load_authority=AsyncMock(
                return_value=self._authority(
                    task,
                    self.runtime._skill_runtime_state.active_revision,
                )
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "skill_recovery_handed_off_agent_run_missing",
        ):
            await self.runtime._reconcile_skill_recovery_task(task.task_id)

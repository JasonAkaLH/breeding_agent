from __future__ import annotations

import unittest
import asyncio
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.integrations.mcp.agent_recovery import (
    MCPAgentAuthorityKind,
    MCPAgentAuthoritySnapshot,
    MCPAgentAuthorityState,
    MCPAgentRecoveryAdapter,
)
from src.lifecycle.agent_run_recovery import (
    AgentAuthorityResolution,
    AgentRecoveryState,
    AgentRunRecoveryCoordinator,
)
from src.lifecycle.cancellation_service import CancellationService
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocatorService,
    AgentResumeKind,
)
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentFinishMetadata,
    AgentItemKind,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStorageConflict,
    AgentTaskLease,
    AgentToolCall,
    AgentUsage,
)
from src.storage.sqlite import (
    SQLiteAgentRepository,
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import TaskRow


class _WaitingLeaseStore:
    def __init__(self) -> None:
        self.renewals = 0
        self.releases = 0

    async def acquire_task_lease(self, run_id, *, owner_id, ttl_seconds):
        return AgentTaskLease(
            run_id=run_id,
            task_id="task-1",
            owner_id=owner_id,
            token="token-1",
            revision=1,
            expires_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )

    async def renew_task_lease(self, *args, **kwargs):
        self.renewals += 1
        raise AssertionError("waiting lease must not heartbeat")

    async def release_waiting_task_lease(self, run_id, *, owner_id, token):
        self.releases += 1
        return AgentRun(
            run_id,
            "task-1",
            "conv-1",
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentModelBinding("edition-a"),
        )


class AgentRunRecoveryLeaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_release_uses_no_heartbeat_or_worker_phase(self) -> None:
        store = _WaitingLeaseStore()
        sleep_calls = 0

        async def sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1

        controller = AgentLeaseController(store, ttl_seconds=30, sleep=sleep)
        handle = await controller.acquire("run-1", owner_id="worker-1")

        released = await controller.release_waiting("run-1", handle=handle)

        self.assertEqual(released.status, AgentRunStatus.WAITING_FOR_INPUT)
        self.assertEqual(store.releases, 1)
        self.assertEqual(store.renewals, 0)
        self.assertEqual(sleep_calls, 0)


class _RecordingResumer:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.run_ids = []

    async def run_claimed(self, run_id, *, handle, **_kwargs):
        self.run_ids.append(run_id)
        run = await self.repository.get_run(run_id)
        return SimpleNamespace(run=run, state="resumed", handle=handle)


class AgentRunRecoveryCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "recovery.sqlite")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.repository = SQLiteAgentRepository(self.sessions)
        self.storage = SQLiteStorage(self.sessions)
        self.binding = AgentModelBinding(
            "edition-fixed",
            reasoning_effort="high",
            option_digests={"prompt": "a" * 64},
        )
        self.locator_service = AgentContinuationLocatorService()
        self.resumer = _RecordingResumer(self.repository)
        self.coordinator = AgentRunRecoveryCoordinator(
            runs=self.repository,
            writer=self.repository,
            lease_store=self.repository,
            resumer=self.resumer,
            locator_service=self.locator_service,
            owner_id="recovery-worker",
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()
        await super().asyncTearDown()

    async def _seed_waiting(self, suffix: str, kind: AgentResumeKind):
        task_id = f"task-{suffix}"
        run_id = f"run-{suffix}"
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id=task_id,
                    conversation_id=f"conv-{suffix}",
                    root_message_id=f"message-{suffix}",
                    status="running",
                    routing_mode="auto",
                )
            )
        await self.repository.create_run(
            AgentRun(
                run_id,
                task_id,
                f"conv-{suffix}",
                AgentRunStatus.RUNNING,
                self.binding,
            )
        )
        lease = await self.repository.acquire_task_lease(
            run_id,
            owner_id="seed-worker",
            ttl_seconds=30,
        )
        run = await self.repository.get_run(run_id)
        assert run is not None
        sample = AgentSample(
            sample_id=f"sample-{suffix}",
            binding=self.binding,
            visible_text="",
            tool_calls=(AgentToolCall(f"call-{suffix}", "tool_safe", "{}", 0),),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata("tool_calls", 1),
        )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run_id,
                expected_revision=run.revision,
                expected_claim_token=lease.token,
                sample=sample,
                capability_ids_by_tool_name={"tool_safe": "skill.safe"},
            )
        )
        authority_digest = hashlib.sha256(f"authority-{suffix}".encode()).hexdigest()
        locator = self.locator_service.build(
            run=committed.run,
            call_item=committed.call_items[0],
            owner_scope="owner-1",
            resume_kind=kind,
            authority_digest=authority_digest,
            pinned_bundle_revision=(
                "bundle-r1" if kind is AgentResumeKind.SKILL_INPUT else None
            ),
        )
        waiting = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run_id=run_id,
                expected_revision=committed.run.revision,
                expected_claim_token=lease.token,
                call_item_id=committed.call_items[0].item_id,
                safe_result_payload={"continuation_locator": locator.to_safe_dict()},
                status=kind.waiting_status,
            )
        )
        run = await self.repository.get_run(run_id)
        assert run is not None
        released = await self.repository.release_waiting_task_lease(
            run_id,
            owner_id="seed-worker",
            token=lease.token,
        )
        return locator, waiting, released

    async def test_skill_continuation_commits_before_ack_and_duplicate_is_idempotent(self) -> None:
        locator, _, _ = await self._seed_waiting("skill", AgentResumeKind.SKILL_INPUT)
        resolver_calls = 0
        ack_observations = []

        async def resolve(_locator):
            nonlocal resolver_calls
            resolver_calls += 1
            return AgentAuthorityResolution(
                authority_digest=locator.authority_digest,
                status=AgentCallOutcomeStatus.COMPLETED,
                safe_result_payload={"answer_digest": "b" * 64},
                safe_continuation_facts={"answer_digest": "b" * 64},
            )

        async def acknowledge():
            items = await self.repository.list_items(locator.run_id)
            ack_observations.append(
                (
                    any(
                        item.kind is AgentItemKind.TOOL_RESULT
                        and item.state.value == "committed"
                        for item in items
                    ),
                    any(item.kind is AgentItemKind.CONTINUATION for item in items),
                )
            )

        first = await self.coordinator.continue_waiting_call(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=resolve,
            acknowledge=acknowledge,
        )
        restarted = AgentRunRecoveryCoordinator(
            runs=self.repository,
            writer=self.repository,
            lease_store=self.repository,
            resumer=self.resumer,
            owner_id="restarted-recovery",
        )
        second = await restarted.continue_waiting_call(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=lambda _locator: (_ for _ in ()).throw(
                AssertionError("duplicate must not resolve authority again")
            ),
            acknowledge=acknowledge,
        )

        self.assertEqual(first.state, AgentRecoveryState.RESUMED)
        self.assertEqual(second.state, AgentRecoveryState.DUPLICATE)
        self.assertEqual(resolver_calls, 1)
        self.assertEqual(ack_observations, [(True, True), (True, True)])
        self.assertEqual(self.resumer.run_ids, [locator.run_id])
        items = await self.repository.list_items(locator.run_id)
        self.assertEqual(sum(item.kind is AgentItemKind.CONTINUATION for item in items), 1)
        self.assertEqual((await self.repository.get_run(locator.run_id)).binding, self.binding)

    async def test_mcp_approval_elicitation_and_remote_use_original_identity(self) -> None:
        adapter = MCPAgentRecoveryAdapter()
        cases = (
            ("approval", AgentResumeKind.MCP_APPROVAL, MCPAgentAuthorityKind.APPROVAL),
            ("mrtr", AgentResumeKind.MCP_ELICITATION, MCPAgentAuthorityKind.ELICITATION),
            ("remote", AgentResumeKind.MCP_REMOTE_TASK, MCPAgentAuthorityKind.REMOTE_TASK),
        )
        for suffix, resume_kind, authority_kind in cases:
            with self.subTest(kind=suffix):
                locator, _, _ = await self._seed_waiting(suffix, resume_kind)
                snapshot = MCPAgentAuthoritySnapshot(
                    kind=authority_kind,
                    state=MCPAgentAuthorityState.TERMINAL_COMPLETED,
                    locator_digest=locator.digest,
                    authority_digest=locator.authority_digest,
                    safe_result_payload={"projection": suffix},
                    result_receipt_ref=f"receipt-{suffix}",
                )
                result = await self.coordinator.continue_waiting_call(
                    locator,
                    owner_scope="owner-1",
                    authority_digest=locator.authority_digest,
                    resolve_authority=lambda current, snapshot=snapshot: adapter.project(
                        current, snapshot
                    ),
                )
                self.assertEqual(result.result_item.source_call_item_id, locator.call_item_id)
                self.assertEqual(result.run.run_id, locator.run_id)
                self.assertEqual(result.run.binding, locator.model_binding)

    async def test_unknown_side_effect_aborts_without_executor_replay(self) -> None:
        locator, _, _ = await self._seed_waiting("unknown", AgentResumeKind.MCP_REMOTE_TASK)
        executor_calls = 0

        result = await self.coordinator.converge_unknown_side_effect(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
        )

        self.assertEqual(executor_calls, 0)
        payload = json.loads(result.result_item.payload_json)
        self.assertEqual(payload["outcome"], "aborted")
        self.assertEqual(payload["safe_error_code"], "side_effect_unknown_no_replay")

    async def test_authoritative_result_repairs_reserved_item_and_ack_loss_retries(self) -> None:
        locator, _, _ = await self._seed_waiting("repair", AgentResumeKind.MCP_REMOTE_TASK)
        resolution = AgentAuthorityResolution(
            locator.authority_digest,
            AgentCallOutcomeStatus.COMPLETED,
            {"projection": "durable"},
            {"result_receipt_ref": "receipt-repair"},
        )

        first = await self.coordinator.recover_authoritative_result(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=lambda _locator: resolution,
            acknowledge=lambda: (_ for _ in ()).throw(RuntimeError("response lost")),
        )
        second = await self.coordinator.recover_authoritative_result(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=lambda _locator: (_ for _ in ()).throw(
                AssertionError("durable result must not be recovered twice")
            ),
            acknowledge=lambda: None,
        )

        self.assertFalse(first.acknowledged)
        self.assertTrue(second.acknowledged)
        self.assertEqual(second.state, AgentRecoveryState.DUPLICATE)
        self.assertEqual(first.result_item.item_id, second.result_item.item_id)

    async def test_commit_fault_does_not_ack_or_expose_continuation(self) -> None:
        locator, _, _ = await self._seed_waiting("fault", AgentResumeKind.MCP_APPROVAL)

        def fail(stage: str) -> None:
            if stage == "outcome_after_result":
                raise RuntimeError("injected outcome fault")

        fault_repository = SQLiteAgentRepository(self.sessions, fault_injector=fail)
        coordinator = AgentRunRecoveryCoordinator(
            runs=fault_repository,
            writer=fault_repository,
            lease_store=fault_repository,
            resumer=self.resumer,
            owner_id="fault-recovery",
        )
        ack_calls = 0

        async def acknowledge():
            nonlocal ack_calls
            ack_calls += 1

        with self.assertRaisesRegex(RuntimeError, "injected outcome fault"):
            await coordinator.continue_waiting_call(
                locator,
                owner_scope="owner-1",
                authority_digest=locator.authority_digest,
                resolve_authority=lambda _locator: AgentAuthorityResolution(
                    locator.authority_digest,
                    AgentCallOutcomeStatus.COMPLETED,
                    {"projection": "hidden"},
                    {"result_receipt_ref": "receipt-hidden"},
                ),
                acknowledge=acknowledge,
            )
        items = await self.repository.list_items(locator.run_id)
        result = next(
            item for item in items if item.source_call_item_id == locator.call_item_id
        )
        self.assertEqual(result.state.value, "reserved")
        self.assertFalse(any(item.kind is AgentItemKind.CONTINUATION for item in items))
        self.assertEqual(ack_calls, 0)

    async def test_lease_conflict_keeps_waiting_authority_unchanged(self) -> None:
        locator, _, before = await self._seed_waiting("held", AgentResumeKind.SKILL_INPUT)
        await self.repository.acquire_task_lease(
            locator.run_id,
            owner_id="current-owner",
            ttl_seconds=30,
        )
        resolver_calls = 0

        async def resolve(_locator):
            nonlocal resolver_calls
            resolver_calls += 1

        with self.assertRaisesRegex(AgentStorageConflict, "lease_held"):
            await self.coordinator.continue_waiting_call(
                locator,
                owner_scope="owner-1",
                authority_digest=locator.authority_digest,
                resolve_authority=resolve,
            )
        current = await self.repository.get_run(locator.run_id)
        self.assertEqual(current.waiting_call_item_ids, before.waiting_call_item_ids)
        self.assertEqual(resolver_calls, 0)

    async def test_identity_mismatch_rejects_without_mutating_waiting_set(self) -> None:
        locator, _, before = await self._seed_waiting("mismatch", AgentResumeKind.MCP_APPROVAL)
        for owner, digest in (
            ("wrong-owner", locator.authority_digest),
            ("owner-1", "0" * 64),
        ):
            with self.subTest(owner=owner, digest=digest):
                with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                    await self.coordinator.continue_waiting_call(
                        locator,
                        owner_scope=owner,
                        authority_digest=digest,
                        resolve_authority=lambda _locator: None,
                    )
                current = await self.repository.get_run(locator.run_id)
                self.assertEqual(current.waiting_call_item_ids, before.waiting_call_item_ids)
                self.assertIsNone(current.claim_token)

    async def test_cancel_prevents_resume_and_fences_late_result(self) -> None:
        locator, _, _ = await self._seed_waiting("cancel", AgentResumeKind.MCP_REMOTE_TASK)
        cancellation = CancellationService(
            self.storage,
            agent_runs=self.repository,
        )

        cancelled_task = await cancellation.cancel_task_context(locator.task_id)
        resolver_calls = 0

        async def resolve(_locator):
            nonlocal resolver_calls
            resolver_calls += 1
            return AgentAuthorityResolution(
                locator.authority_digest,
                AgentCallOutcomeStatus.COMPLETED,
                {"projection": "late"},
                {"receipt": "late"},
            )

        result = await self.coordinator.continue_waiting_call(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=resolve,
        )

        self.assertEqual(str(cancelled_task.status), "cancelled")
        self.assertEqual(result.state, AgentRecoveryState.TERMINAL)
        self.assertEqual(result.run.status, AgentRunStatus.CANCELLED)
        self.assertEqual(resolver_calls, 0)
        self.assertNotIn(locator.run_id, self.resumer.run_ids)

    async def test_cancel_wins_against_inflight_remote_completion(self) -> None:
        locator, _, _ = await self._seed_waiting("cancel-race", AgentResumeKind.MCP_REMOTE_TASK)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def resolve(_locator):
            entered.set()
            await release.wait()
            return AgentAuthorityResolution(
                locator.authority_digest,
                AgentCallOutcomeStatus.COMPLETED,
                {"projection": "late-remote"},
                {"result_receipt_ref": "receipt-late"},
            )

        recovery_task = asyncio.create_task(
            self.coordinator.continue_waiting_call(
                locator,
                owner_scope="owner-1",
                authority_digest=locator.authority_digest,
                resolve_authority=resolve,
            )
        )
        await entered.wait()
        await CancellationService(
            self.storage,
            agent_runs=self.repository,
        ).cancel_task_context(locator.task_id)
        release.set()
        result = await recovery_task

        self.assertEqual(result.state, AgentRecoveryState.TERMINAL)
        self.assertEqual(result.run.status, AgentRunStatus.CANCELLED)
        items = await self.repository.list_items(locator.run_id)
        call_result = next(
            item for item in items if item.source_call_item_id == locator.call_item_id
        )
        self.assertEqual(call_result.state.value, "reserved")
        self.assertFalse(any(item.kind is AgentItemKind.CONTINUATION for item in items))

    async def test_stale_owner_cannot_commit_after_takeover(self) -> None:
        locator, _, _ = await self._seed_waiting("fence", AgentResumeKind.SKILL_INPUT)
        old = await self.repository.acquire_task_lease(
            locator.run_id,
            owner_id="old-worker",
            ttl_seconds=30,
        )
        run = await self.repository.get_run(locator.run_id)
        assert run is not None
        cancelled = await self.repository.cancel_agent_run(
            locator.run_id,
            expected_revision=run.revision,
            expected_claim_token=old.token,
            safe_reason_code="takeover_cancel",
        )
        with self.assertRaisesRegex(AgentStorageConflict, "cas_mismatch|terminal"):
            await self.repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    run_id=locator.run_id,
                    expected_revision=old.revision,
                    expected_claim_token=old.token,
                    call_item_id=locator.call_item_id,
                    safe_result_payload={"status": "late"},
                    status=AgentCallOutcomeStatus.COMPLETED,
                )
            )
        self.assertEqual(cancelled.status, AgentRunStatus.CANCELLED)

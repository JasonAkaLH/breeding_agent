from __future__ import annotations

import unittest
import asyncio
import hashlib
import inspect
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
    AgentTransientRecoveryOutcome,
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
    AgentUserMessageCommit,
)
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
    AgentCallResultProjector,
    build_model_result_envelope,
)
from src.orchestration.agent_loop.transient_results import (
    AgentTransientSkillResultStore,
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
        self.recovery_arguments = []

    async def run_claimed(
        self,
        run_id,
        *,
        handle,
        initial_required_tool_name=None,
        trusted_facts=(),
        visibility_context=None,
        cancellation=None,
    ):
        self.run_ids.append(run_id)
        self.recovery_arguments.append(
            {
                "initial_required_tool_name": initial_required_tool_name,
                "trusted_facts": trusted_facts,
                "visibility_context": visibility_context,
                "cancellation": cancellation,
            }
        )
        run = await self.repository.get_run(run_id)
        return SimpleNamespace(run=run, state="resumed", handle=handle)


class _TracingRepository:
    def __init__(self, repository, trace: list[str]) -> None:
        self._repository = repository
        self._trace = trace

    def __getattr__(self, name: str):
        attribute = getattr(self._repository, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute

        async def traced(*args, **kwargs):
            self._trace.append(f"repository.{name}")
            return await attribute(*args, **kwargs)

        return traced


class _TracingResumer:
    def __init__(self, repository, trace: list[str], *, state: str = "resumed") -> None:
        self._repository = repository
        self._trace = trace
        self._state = state
        self.calls = 0

    async def run_claimed(self, run_id, *, handle, **_kwargs):
        self.calls += 1
        self._trace.append("resumer.run_claimed")
        run = await self._repository.get_run(run_id)
        return SimpleNamespace(run=run, state=self._state, handle=handle)


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

    def _tracing_coordinator(
        self,
        trace: list[str],
        *,
        resumer_state: str = "resumed",
    ):
        repository = _TracingRepository(self.repository, trace)
        resumer = _TracingResumer(
            self.repository,
            trace,
            state=resumer_state,
        )
        coordinator = AgentRunRecoveryCoordinator(
            runs=repository,
            writer=repository,
            lease_store=repository,
            resumer=resumer,
            locator_service=self.locator_service,
            owner_id="trace-recovery-worker",
        )
        return coordinator, resumer

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()
        await super().asyncTearDown()

    async def _seed_waiting(
        self,
        suffix: str,
        kind: AgentResumeKind,
        *,
        extra_waiting: bool = False,
    ):
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
        tool_calls = [AgentToolCall(f"call-{suffix}", "tool_safe", "{}", 0)]
        capability_ids = {"tool_safe": "skill.safe"}
        if extra_waiting:
            tool_calls.append(
                AgentToolCall(
                    f"call-{suffix}-extra",
                    "tool_safe_extra",
                    "{}",
                    1,
                )
            )
            capability_ids["tool_safe_extra"] = "skill.safe"
        sample = AgentSample(
            sample_id=f"sample-{suffix}",
            binding=self.binding,
            visible_text="",
            tool_calls=tuple(tool_calls),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata("tool_calls", 1),
        )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run_id,
                expected_revision=run.revision,
                expected_claim_token=lease.token,
                sample=sample,
                capability_ids_by_tool_name=capability_ids,
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
        if extra_waiting:
            current = await self.repository.get_run(run_id)
            assert current is not None
            extra_locator = self.locator_service.build(
                run=current,
                call_item=committed.call_items[1],
                owner_scope="owner-1",
                resume_kind=kind,
                authority_digest=hashlib.sha256(
                    f"authority-{suffix}-extra".encode()
                ).hexdigest(),
                pinned_bundle_revision=(
                    "bundle-r1" if kind is AgentResumeKind.SKILL_INPUT else None
                ),
            )
            await self.repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    run_id=run_id,
                    expected_revision=current.revision,
                    expected_claim_token=lease.token,
                    call_item_id=committed.call_items[1].item_id,
                    safe_result_payload={
                        "continuation_locator": extra_locator.to_safe_dict()
                    },
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

    async def test_recovery_logical_call_sites_have_exact_normal_and_remaining_waiting_traces(
        self,
    ) -> None:
        expected_prefix = [
            "repository.get_run",
            "repository.list_items",
            "repository.acquire_task_lease",
            "repository.get_run",
            "repository.list_items",
            "authority.resolve",
            "repository.get_run",
            "repository.list_items",
            "repository.commit_agent_call_outcome",
            "authority.ack",
            "repository.get_run",
        ]
        for suffix, extra_waiting, resumer_state, expected_tail, expected_state in (
            (
                "trace-clear",
                False,
                "resumed",
                ["resumer.run_claimed"],
                AgentRecoveryState.RESUMED,
            ),
            (
                "trace-final-candidate",
                False,
                "final_candidate",
                ["resumer.run_claimed"],
                AgentRecoveryState.FINAL_CANDIDATE,
            ),
            (
                "trace-remaining",
                True,
                "resumed",
                ["repository.release_waiting_task_lease"],
                AgentRecoveryState.WAITING,
            ),
        ):
            with self.subTest(suffix=suffix):
                locator, _, _ = await self._seed_waiting(
                    suffix,
                    AgentResumeKind.SKILL_INPUT,
                    extra_waiting=extra_waiting,
                )
                trace: list[str] = []
                coordinator, resumer = self._tracing_coordinator(
                    trace,
                    resumer_state=resumer_state,
                )

                async def resolve(_locator):
                    trace.append("authority.resolve")
                    return AgentAuthorityResolution(
                        locator.authority_digest,
                        AgentCallOutcomeStatus.COMPLETED,
                        {"answer": "safe"},
                        {"answer_digest": "b" * 64},
                    )

                async def acknowledge():
                    trace.append("authority.ack")

                result = await coordinator.continue_waiting_call(
                    locator,
                    owner_scope="owner-1",
                    authority_digest=locator.authority_digest,
                    resolve_authority=resolve,
                    acknowledge=acknowledge,
                )

                self.assertEqual(result.state, expected_state)
                self.assertEqual(trace, expected_prefix + expected_tail)
                self.assertEqual(resumer.calls, 0 if extra_waiting else 1)

    async def test_duplicate_and_terminal_preload_ack_before_lease_acquire(self) -> None:
        cases = []
        duplicate_locator, _, _ = await self._seed_waiting(
            "trace-duplicate",
            AgentResumeKind.MCP_APPROVAL,
        )
        await self.coordinator.continue_waiting_call(
            duplicate_locator,
            owner_scope="owner-1",
            authority_digest=duplicate_locator.authority_digest,
            resolve_authority=lambda _locator: AgentAuthorityResolution(
                duplicate_locator.authority_digest,
                AgentCallOutcomeStatus.COMPLETED,
                {"answer": "safe"},
                {"answer_digest": "c" * 64},
            ),
        )
        cases.append((duplicate_locator, AgentRecoveryState.DUPLICATE))

        terminal_locator, _, _ = await self._seed_waiting(
            "trace-terminal",
            AgentResumeKind.MCP_REMOTE_TASK,
        )
        current = await self.repository.get_run(terminal_locator.run_id)
        assert current is not None
        lease = await self.repository.acquire_task_lease(
            terminal_locator.run_id,
            owner_id="terminal-worker",
            ttl_seconds=30,
        )
        await self.repository.cancel_agent_run(
            terminal_locator.run_id,
            expected_revision=lease.revision,
            expected_claim_token=lease.token,
            safe_reason_code="trace_terminal",
        )
        cases.append((terminal_locator, AgentRecoveryState.TERMINAL))

        for locator, expected_state in cases:
            with self.subTest(state=expected_state):
                trace: list[str] = []
                coordinator, resumer = self._tracing_coordinator(trace)

                async def acknowledge():
                    trace.append("authority.ack")

                result = await coordinator.continue_waiting_call(
                    locator,
                    owner_scope="owner-1",
                    authority_digest=locator.authority_digest,
                    resolve_authority=lambda _locator: (_ for _ in ()).throw(
                        AssertionError("preload branch must not resolve")
                    ),
                    acknowledge=acknowledge,
                )
                self.assertEqual(result.state, expected_state)
                self.assertEqual(
                    trace,
                    [
                        "repository.get_run",
                        "repository.list_items",
                        "authority.ack",
                    ],
                )
                self.assertEqual(resumer.calls, 0)

    async def test_post_resolve_committed_and_terminal_barriers_ack_without_commit_or_reload(
        self,
    ) -> None:
        for suffix, barrier, expected_state in (
            (
                "trace-concurrent-commit",
                "commit",
                AgentRecoveryState.DUPLICATE,
            ),
            (
                "trace-concurrent-terminal",
                "terminal",
                AgentRecoveryState.TERMINAL,
            ),
        ):
            with self.subTest(barrier=barrier):
                locator, _, _ = await self._seed_waiting(
                    suffix,
                    AgentResumeKind.MCP_REMOTE_TASK,
                )
                trace: list[str] = []
                coordinator, resumer = self._tracing_coordinator(trace)

                async def resolve(_locator):
                    trace.append("authority.resolve")
                    current = await self.repository.get_run(locator.run_id)
                    assert current is not None
                    if barrier == "commit":
                        await self.repository.commit_agent_call_outcome(
                            AgentCallOutcomeCommit(
                                run_id=current.run_id,
                                expected_revision=current.revision,
                                expected_claim_token=current.claim_token,
                                call_item_id=locator.call_item_id,
                                safe_result_payload={"winner": "concurrent"},
                                status=AgentCallOutcomeStatus.COMPLETED,
                            )
                        )
                    else:
                        await self.repository.cancel_agent_run(
                            current.run_id,
                            expected_revision=current.revision,
                            expected_claim_token=current.claim_token,
                            safe_reason_code="concurrent_terminal",
                        )
                    return AgentAuthorityResolution(
                        locator.authority_digest,
                        AgentCallOutcomeStatus.COMPLETED,
                        {"loser": "resolved"},
                        {"answer_digest": "d" * 64},
                    )

                async def acknowledge():
                    trace.append("authority.ack")

                result = await coordinator.continue_waiting_call(
                    locator,
                    owner_scope="owner-1",
                    authority_digest=locator.authority_digest,
                    resolve_authority=resolve,
                    acknowledge=acknowledge,
                )

                self.assertEqual(result.state, expected_state)
                self.assertEqual(
                    trace,
                    [
                        "repository.get_run",
                        "repository.list_items",
                        "repository.acquire_task_lease",
                        "repository.get_run",
                        "repository.list_items",
                        "authority.resolve",
                        "repository.get_run",
                        "repository.list_items",
                        "authority.ack",
                    ],
                )
                self.assertEqual(resumer.calls, 0)

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

    async def test_startup_recovery_aborts_reserved_call_without_replay_then_resumes(self) -> None:
        task_id = "task-crashed"
        run_id = "run-crashed"
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id=task_id,
                    conversation_id="conv-crashed",
                    root_message_id="message-crashed",
                    status="running",
                    routing_mode="auto",
                )
            )
        run = await self.repository.create_run(
            AgentRun(
                run_id,
                task_id,
                "conv-crashed",
                AgentRunStatus.RUNNING,
                self.binding,
            )
        )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run_id,
                expected_revision=run.revision,
                expected_claim_token=None,
                sample=AgentSample(
                    sample_id="sample-crashed",
                    binding=self.binding,
                    visible_text="",
                    tool_calls=(
                        AgentToolCall("call-crashed", "tool_safe", "{}", 0),
                    ),
                    usage=AgentUsage(status="usage_unavailable"),
                    finish=AgentFinishMetadata("tool_calls", 1),
                ),
                capability_ids_by_tool_name={"tool_safe": "skill.safe"},
            )
        )

        recoverable = await self.repository.list_recoverable_runs()
        self.assertIn(run_id, {item.run_id for item in recoverable})
        trace: list[str] = []
        coordinator, resumer = self._tracing_coordinator(trace)
        result = await coordinator.recover_crashed_run(run_id)

        self.assertEqual(result.state, AgentRecoveryState.RESUMED)
        self.assertEqual(resumer.calls, 1)
        self.assertEqual(
            trace,
            [
                "repository.reconcile_agent_run_consistency",
                "repository.acquire_task_lease",
                "repository.get_run",
                "repository.list_items",
                "repository.get_run",
                "repository.commit_agent_call_outcome",
                "resumer.run_claimed",
            ],
        )
        items = await self.repository.list_items(run_id)
        tool_result = next(
            item
            for item in items
            if item.source_call_item_id == committed.call_items[0].item_id
        )
        payload = json.loads(tool_result.payload_json)
        self.assertEqual(payload["outcome"], "aborted")
        self.assertEqual(payload["safe_error_code"], "side_effect_unknown_no_replay")

    async def test_startup_recovery_commits_matching_transient_stage_without_replay(
        self,
    ) -> None:
        task_id = "task-transient-crashed"
        run_id = "run-transient-crashed"
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id=task_id,
                    conversation_id="conv-transient-crashed",
                    root_message_id="message-transient-crashed",
                    status="running",
                    routing_mode="auto",
                )
            )
        run = await self.repository.create_run(
            AgentRun(
                run_id,
                task_id,
                "conv-transient-crashed",
                AgentRunStatus.RUNNING,
                self.binding,
            )
        )
        sampled = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run_id,
                expected_revision=run.revision,
                expected_claim_token=None,
                sample=AgentSample(
                    sample_id="sample-transient-crashed",
                    binding=self.binding,
                    visible_text="",
                    tool_calls=(
                        AgentToolCall("call-transient", "tool_safe", "{}", 0),
                    ),
                    usage=AgentUsage(status="usage_unavailable"),
                    finish=AgentFinishMetadata("tool_calls", 1),
                ),
                capability_ids_by_tool_name={"tool_safe": "skill.safe"},
            )
        )
        call = sampled.call_items[0]
        reservation = sampled.result_reservations[0]
        projection = AgentCallResultProjector().project(
            capability_id="skill.safe",
            output_payload={"rows": ["x" * 150_000]},
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
        )
        store = AgentTransientSkillResultStore(
            Path(self.temp_dir.name) / "transient-results"
        )
        store.stage(
            run=sampled.run,
            call_item=call,
            result_item_id=reservation.item_id,
            node_id=sampled.node_ids[0],
            capability_id="skill.safe",
            canonical_raw_bytes=projection.canonical_raw_bytes,
            raw_sha256=projection.raw_sha256,
            projection_revision=projection.projection_revision,
            expected_stage_ref=projection.transient_stage_ref,
        )

        def recover_stage(current_run, current_call, current_result):
            recovered = store.recover_stage(
                run=current_run,
                call_item=current_call,
                result_item=current_result,
            )
            if recovered is None:
                return None
            return AgentTransientRecoveryOutcome(
                safe_result_payload=build_model_result_envelope(
                    projection_revision=recovered.projection_revision,
                    projection_mode="transient_staged",
                    model_view={
                        "complete_result_pending_context_injection": True,
                        "schema": (
                            "maf.agent.transient_skill_result_receipt.v1"
                        ),
                        "stage_ref": recovered.stage_ref,
                    },
                    original_size_bytes=recovered.raw_size_bytes,
                    raw_sha256=recovered.raw_sha256,
                    projection_truncated=True,
                )
            )

        resumer = _RecordingResumer(self.repository)
        coordinator = AgentRunRecoveryCoordinator(
            runs=self.repository,
            writer=self.repository,
            lease_store=self.repository,
            resumer=resumer,
            owner_id="transient-recovery-worker",
            transient_result_recoverer=recover_stage,
        )

        recovered = await coordinator.recover_crashed_run(run_id)

        self.assertEqual(recovered.state, AgentRecoveryState.RESUMED)
        self.assertEqual(resumer.run_ids, [run_id])
        result = next(
            item
            for item in await self.repository.list_items(run_id)
            if item.item_id == reservation.item_id
        )
        payload = json.loads(result.payload_json)
        self.assertEqual(payload["outcome"], "completed")
        self.assertEqual(
            payload["safe_result"]["projection_mode"], "transient_staged"
        )
        self.assertEqual(payload["artifact_refs"], [])

    async def test_startup_recovery_only_forwards_initial_tool_for_pristine_run(self) -> None:
        initialized_runs = {}
        for suffix in ("pristine", "assistant", "tool", "default"):
            with self.sessions.begin() as session:
                session.add(
                    TaskRow(
                        task_id=f"task-{suffix}",
                        conversation_id=f"conv-{suffix}",
                        root_message_id=f"message-{suffix}",
                        status="running",
                        routing_mode="auto",
                    )
                )
            run = await self.repository.create_run(
                AgentRun(
                    f"run-{suffix}",
                    f"task-{suffix}",
                    f"conv-{suffix}",
                    AgentRunStatus.RUNNING,
                    self.binding,
                )
            )
            initialized_runs[suffix] = await self.repository.commit_agent_user_message(
                AgentUserMessageCommit(
                    run_id=run.run_id,
                    expected_revision=run.revision,
                    expected_claim_token=None,
                    text=f"input-{suffix}",
                )
            )

        await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id="run-assistant",
                expected_revision=initialized_runs["assistant"].run.revision,
                expected_claim_token=None,
                sample=AgentSample(
                    sample_id="sample-assistant",
                    binding=self.binding,
                    visible_text="answer",
                    tool_calls=(),
                    usage=AgentUsage(status="usage_unavailable"),
                    finish=AgentFinishMetadata("stop", 1),
                ),
                capability_ids_by_tool_name={},
            )
        )
        await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id="run-tool",
                expected_revision=initialized_runs["tool"].run.revision,
                expected_claim_token=None,
                sample=AgentSample(
                    sample_id="sample-tool",
                    binding=self.binding,
                    visible_text="",
                    tool_calls=(AgentToolCall("call-tool", "tool_safe", "{}", 0),),
                    usage=AgentUsage(status="usage_unavailable"),
                    finish=AgentFinishMetadata("tool_calls", 1),
                ),
                capability_ids_by_tool_name={"tool_safe": "skill.safe"},
            )
        )

        visibility_context = object()
        cancellation = object()
        await self.coordinator.recover_crashed_run(
            "run-pristine",
            initial_required_tool_name="tool_required",
            trusted_facts=("fact-a", "fact-b"),
            visibility_context=visibility_context,
            cancellation=cancellation,
        )
        await self.coordinator.recover_crashed_run(
            "run-assistant",
            initial_required_tool_name="tool_must_not_repeat",
        )
        await self.coordinator.recover_crashed_run(
            "run-tool",
            initial_required_tool_name="tool_must_not_repeat",
        )
        await self.coordinator.recover_crashed_run("run-default")

        self.assertEqual(
            self.resumer.recovery_arguments[0],
            {
                "initial_required_tool_name": "tool_required",
                "trusted_facts": ("fact-a", "fact-b"),
                "visibility_context": visibility_context,
                "cancellation": cancellation,
            },
        )
        self.assertEqual(
            self.resumer.recovery_arguments[1],
            {
                "initial_required_tool_name": None,
                "trusted_facts": (),
                "visibility_context": None,
                "cancellation": None,
            },
        )
        self.assertEqual(
            self.resumer.recovery_arguments[2:],
            [
                {
                    "initial_required_tool_name": None,
                    "trusted_facts": (),
                    "visibility_context": None,
                    "cancellation": None,
                },
                {
                    "initial_required_tool_name": None,
                    "trusted_facts": (),
                    "visibility_context": None,
                    "cancellation": None,
                },
            ],
        )

    async def test_startup_recovery_terminal_and_waiting_stop_after_reconcile(self) -> None:
        waiting_locator, _, _ = await self._seed_waiting(
            "crash-waiting",
            AgentResumeKind.SKILL_INPUT,
        )
        terminal_locator, _, _ = await self._seed_waiting(
            "crash-terminal",
            AgentResumeKind.MCP_REMOTE_TASK,
        )
        lease = await self.repository.acquire_task_lease(
            terminal_locator.run_id,
            owner_id="crash-terminal-worker",
            ttl_seconds=30,
        )
        await self.repository.cancel_agent_run(
            terminal_locator.run_id,
            expected_revision=lease.revision,
            expected_claim_token=lease.token,
            safe_reason_code="crash_terminal",
        )

        for run_id, expected_state in (
            (waiting_locator.run_id, AgentRecoveryState.WAITING),
            (terminal_locator.run_id, AgentRecoveryState.TERMINAL),
        ):
            with self.subTest(state=expected_state):
                trace: list[str] = []
                coordinator, resumer = self._tracing_coordinator(trace)
                result = await coordinator.recover_crashed_run(run_id)
                self.assertEqual(result.state, expected_state)
                self.assertEqual(
                    trace,
                    ["repository.reconcile_agent_run_consistency"],
                )
                self.assertEqual(resumer.calls, 0)

    async def test_authoritative_result_repairs_reserved_item_and_ack_loss_retries(self) -> None:
        locator, _, _ = await self._seed_waiting("repair", AgentResumeKind.MCP_REMOTE_TASK)
        resolution = AgentAuthorityResolution(
            locator.authority_digest,
            AgentCallOutcomeStatus.COMPLETED,
            {"projection": "durable"},
            {"result_receipt_ref": "receipt-repair"},
        )

        trace: list[str] = []
        coordinator, resumer = self._tracing_coordinator(trace)

        def resolve_authority(_locator):
            trace.append("authority.resolve")
            return resolution

        def lose_ack():
            trace.append("authority.ack_lost")
            raise RuntimeError("response lost")

        first = await coordinator.recover_authoritative_result(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=resolve_authority,
            acknowledge=lose_ack,
        )
        first_trace = list(trace)
        trace.clear()

        def acknowledge_retry():
            trace.append("authority.ack_retry")

        second = await coordinator.recover_authoritative_result(
            locator,
            owner_scope="owner-1",
            authority_digest=locator.authority_digest,
            resolve_authority=lambda _locator: (_ for _ in ()).throw(
                AssertionError("durable result must not be recovered twice")
            ),
            acknowledge=acknowledge_retry,
        )

        self.assertFalse(first.acknowledged)
        self.assertTrue(second.acknowledged)
        self.assertEqual(second.state, AgentRecoveryState.DUPLICATE)
        self.assertEqual(first.result_item.item_id, second.result_item.item_id)
        self.assertEqual(resumer.calls, 1)
        self.assertEqual(
            first_trace,
            [
                "repository.get_run",
                "repository.list_items",
                "repository.acquire_task_lease",
                "repository.get_run",
                "repository.list_items",
                "authority.resolve",
                "repository.get_run",
                "repository.list_items",
                "repository.commit_agent_call_outcome",
                "authority.ack_lost",
                "repository.get_run",
                "resumer.run_claimed",
            ],
        )
        self.assertEqual(
            trace,
            [
                "repository.get_run",
                "repository.list_items",
                "authority.ack_retry",
            ],
        )

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

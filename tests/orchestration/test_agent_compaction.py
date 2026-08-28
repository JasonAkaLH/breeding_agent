from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.orchestration.agent_loop.compaction import AgentCompactionService
from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.context_preflight import (
    AgentContextCandidate,
    AgentContextCandidateBuilder,
    AgentContextPreflightDecision,
    AgentContextPreflightResult,
)
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentCompactionCommit,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentUsage,
)
from src.orchestration.agent_loop.tool_catalog import AgentToolCatalog
from src.storage.sqlite import (
    SQLiteAgentRepository,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import TaskRow


def _candidate(
    decision: AgentContextPreflightDecision,
    *,
    limit: int = 500,
) -> AgentContextCandidate:
    return AgentContextCandidate(
        request=AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=AgentRun(
                "candidate-run",
                "candidate-task",
                "candidate-conversation",
                AgentRunStatus.RUNNING,
                AgentModelBinding("edition-a"),
            ),
            items=(),
            catalog=AgentToolCatalog((), {}),
        ),
        preflight=AgentContextPreflightResult(
            decision=decision,
            required_tokens=10,
            history_tokens=100,
            transient_tokens=0,
            tool_tokens=0,
            total_tokens=110,
            total_context_limit_tokens=limit,
            eligible_closed_history=True,
        ),
    )


class _SummaryModel:
    def __init__(self, binding: AgentModelBinding) -> None:
        self.binding = binding
        self.requests = []

    async def sample_agent(self, request):
        self.requests.append(request)
        return AgentSample(
            f"summary-{len(self.requests)}",
            self.binding,
            "facts and unresolved obligations",
            (),
            AgentUsage(status="usage_unavailable"),
            AgentFinishMetadata("stop", 1),
        )


class AgentCompactionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "compact.sqlite")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id="task-1",
                    conversation_id="conv-1",
                    root_message_id="message-1",
                    status="running",
                    routing_mode="auto",
                )
            )
        self.repository = SQLiteAgentRepository(self.sessions)
        self.binding = AgentModelBinding("edition-a")
        await self.repository.create_run(
            AgentRun("run-1", "task-1", "conv-1", AgentRunStatus.RUNNING, self.binding)
        )
        self.leases = AgentLeaseController(self.repository, ttl_seconds=30)
        self.handle = await self.leases.acquire("run-1", owner_id="worker")
        self.candidate_builder = AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard")
            ),
            token_counter=lambda fragments, _binding: len(fragments),
        )
        for index in range(5):
            run = await self.repository.get_run("run-1")
            await self.repository.commit_agent_sample(
                AgentSampleCommit(
                    "run-1",
                    run.revision,
                    self.handle.current.token,
                    AgentSample(
                        f"sample-{index}",
                        self.binding,
                        f"safe-{index}",
                        (),
                        AgentUsage(status="usage_unavailable"),
                        AgentFinishMetadata("stop", 1),
                    ),
                    {},
                )
            )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_same_binding_summary_advances_boundary_without_deleting_sources(self) -> None:
        model = _SummaryModel(self.binding)
        cleaned_boundaries = []

        class Cleaner:
            def cleanup_covered(
                _self, *, run, items, covered_end_sequence
            ):
                cleaned_boundaries.append(
                    (run.compacted_through_sequence, covered_end_sequence)
                )

        service = AgentCompactionService(
            runs=self.repository,
            writer=self.repository,
            model=model,
            lease_controller=self.leases,
            candidate_builder=self.candidate_builder,
            transient_result_cleaner=Cleaner(),
            minimum_suffix_items=2,
        )
        calls = 0

        def repreflight(_run, _items):
            nonlocal calls
            calls += 1
            return _candidate(AgentContextPreflightDecision.FITS)

        outcome = await service.compact_until_fit(
            run_id="run-1",
            handle=self.handle,
            candidate=_candidate(
                AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED
            ),
            repreflight=repreflight,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].binding, self.binding)
        self.assertEqual(outcome.run.compacted_through_sequence, 3)
        self.assertEqual(cleaned_boundaries, [(3, 3)])
        self.assertEqual([item.sequence for item in outcome.items], [1, 2, 3, 4, 5, 6])
        context = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        ).build(
            run=outcome.run,
            items=outcome.items,
            catalog=AgentToolCatalog((), {}),
        )
        rendered = "\n".join(message.content or "" for message in context.messages)
        self.assertIn("facts and unresolved obligations", rendered)
        self.assertNotIn("safe-0", rendered)
        self.assertIn("safe-3", rendered)

    async def test_compaction_shrinks_only_at_closed_boundaries_until_request_fits(
        self,
    ) -> None:
        model = _SummaryModel(self.binding)

        counter_calls = 0

        def bounded_counter(_fragments, _binding):
            nonlocal counter_calls
            counter_calls += 1
            return 1_000 if counter_calls == 1 else 300

        candidate_builder = AgentContextCandidateBuilder(
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard")
            ),
            token_counter=bounded_counter,
        )
        service = AgentCompactionService(
            runs=self.repository,
            writer=self.repository,
            model=model,
            lease_controller=self.leases,
            candidate_builder=candidate_builder,
            minimum_suffix_items=2,
        )

        outcome = await service.compact_until_fit(
            run_id="run-1",
            handle=self.handle,
            candidate=_candidate(
                AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED,
                limit=650,
            ),
            repreflight=lambda _run, _items: _candidate(
                AgentContextPreflightDecision.FITS,
                limit=650,
            ),
        )

        self.assertLess(outcome.run.compacted_through_sequence, 3)
        self.assertLessEqual(
            await candidate_builder.count_request(model.requests[0]),
            650,
        )

    async def test_required_segments_fatal_after_repreflight_converges_without_retry(self) -> None:
        model = _SummaryModel(self.binding)
        service = AgentCompactionService(
            runs=self.repository,
            writer=self.repository,
            model=model,
            lease_controller=self.leases,
            candidate_builder=self.candidate_builder,
        )
        with self.assertRaisesRegex(
            RuntimeError, "agent_context_required_segments_too_large"
        ):
            await service.compact_until_fit(
                run_id="run-1",
                handle=self.handle,
                candidate=_candidate(
                    AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED
                ),
                repreflight=lambda _run, _items: _candidate(
                    AgentContextPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE
                ),
            )
        self.assertEqual(len(model.requests), 1)

    async def test_digest_mismatch_and_no_eligible_range_fail_without_progress(self) -> None:
        run = await self.repository.get_run("run-1")
        with self.assertRaisesRegex(RuntimeError, "source_digest_mismatch"):
            await self.repository.commit_agent_compaction(
                AgentCompactionCommit(
                    "run-1",
                    run.revision,
                    self.handle.current.token,
                    1,
                    3,
                    "0" * 64,
                    "must not commit",
                )
            )
        unchanged = await self.repository.get_run("run-1")
        self.assertEqual(unchanged.compacted_through_sequence, 0)
        self.assertEqual(len(await self.repository.list_items("run-1")), 5)

        model = _SummaryModel(self.binding)
        service = AgentCompactionService(
            runs=self.repository,
            writer=self.repository,
            model=model,
            lease_controller=self.leases,
            candidate_builder=self.candidate_builder,
            minimum_suffix_items=5,
        )
        with self.assertRaisesRegex(
            RuntimeError, "agent_context_required_segments_too_large"
        ):
            await service.compact_until_fit(
                run_id="run-1",
                handle=self.handle,
                candidate=_candidate(
                    AgentContextPreflightDecision.HISTORY_COMPACTION_REQUIRED
                ),
                repreflight=lambda _run, _items: _candidate(
                    AgentContextPreflightDecision.FITS
                ),
            )
        self.assertEqual(model.requests, [])

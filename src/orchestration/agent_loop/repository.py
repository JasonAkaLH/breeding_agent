from __future__ import annotations

from typing import Protocol

from .models import (
    AgentCallOutcomeCommit,
    AgentCompactionCommit,
    AgentCompactionResult,
    AgentFinalOutputCommit,
    AgentFinalOutputResult,
    AgentItem,
    AgentRun,
    AgentSampleCommit,
    AgentSampleCommitResult,
    AgentTaskLease,
)


class AgentRunRepository(Protocol):
    async def create_run(self, run: AgentRun) -> AgentRun: ...

    async def get_run(self, run_id: str) -> AgentRun | None: ...

    async def get_run_for_task(self, task_id: str) -> AgentRun | None: ...

    async def list_items(self, run_id: str) -> tuple[AgentItem, ...]: ...


class AgentAtomicWriter(Protocol):
    async def commit_agent_sample(self, commit: AgentSampleCommit) -> AgentSampleCommitResult: ...

    async def commit_agent_call_outcome(self, commit: AgentCallOutcomeCommit) -> AgentItem: ...

    async def commit_agent_compaction(
        self, commit: AgentCompactionCommit
    ) -> AgentCompactionResult: ...

    async def commit_agent_final_output(self, commit: AgentFinalOutputCommit) -> AgentFinalOutputResult: ...

    async def reconcile_agent_run_consistency(self, run_id: str) -> AgentRun: ...

    async def fail_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_error_code: str,
    ) -> AgentRun: ...


class AgentTaskLeaseStore(Protocol):
    async def acquire_task_lease(
        self, run_id: str, *, owner_id: str, ttl_seconds: float
    ) -> AgentTaskLease: ...

    async def renew_task_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        token: str,
        ttl_seconds: float,
    ) -> AgentTaskLease: ...

    async def release_waiting_task_lease(
        self, run_id: str, *, owner_id: str, token: str
    ) -> AgentRun: ...

    async def cancel_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_reason_code: str,
    ) -> AgentRun: ...

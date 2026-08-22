from __future__ import annotations

from typing import Protocol

from .models import (
    AgentCallOutcomeCommit,
    AgentFinalOutputCommit,
    AgentFinalOutputResult,
    AgentItem,
    AgentRun,
    AgentSampleCommit,
    AgentSampleCommitResult,
)


class AgentRunRepository(Protocol):
    async def create_run(self, run: AgentRun) -> AgentRun: ...

    async def get_run(self, run_id: str) -> AgentRun | None: ...

    async def get_run_for_task(self, task_id: str) -> AgentRun | None: ...

    async def list_items(self, run_id: str) -> tuple[AgentItem, ...]: ...


class AgentAtomicWriter(Protocol):
    async def commit_agent_sample(self, commit: AgentSampleCommit) -> AgentSampleCommitResult: ...

    async def commit_agent_call_outcome(self, commit: AgentCallOutcomeCommit) -> AgentItem: ...

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

    async def cancel_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_reason_code: str,
    ) -> AgentRun: ...

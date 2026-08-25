from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.api.runtime import ApiRuntime
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocator,
    AgentContinuationLocatorService,
    AgentResumeKind,
)
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)


class _DurableLocatorStorage:
    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    async def list_interrupts_for_task(self, task_id: str):
        self._trace.append("storage.list_interrupts_for_task")
        return []


class _DurableLocatorRepository:
    def __init__(self, run: AgentRun, items: tuple[AgentItem, ...], trace: list[str]) -> None:
        self._run = run
        self._items = items
        self._trace = trace

    async def get_run_for_task(self, task_id: str):
        self._trace.append("repository.get_run_for_task")
        return self._run

    async def list_items(self, run_id: str):
        self._trace.append("repository.list_items")
        return self._items


class _ContinuationRequest(BaseModel):
    conversation_id: str
    task_id: str
    call_item_id: str | None = None


class AgentContinuationAPIFixtureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        binding = AgentModelBinding("edition-fixed")
        first = AgentContinuationLocator(
            run_id="run-1",
            sample_item_id="sample-item-1",
            call_item_id="call-item-1",
            provider_call_id="provider-call-1",
            capability_id="skill.one",
            task_id="task-1",
            node_id="node-1",
            owner_scope="owner-1",
            conversation_id="conv-1",
            resume_kind=AgentResumeKind.SKILL_INPUT,
            authority_digest=hashlib.sha256(b"authority-1").hexdigest(),
            pinned_bundle_revision="bundle-r1",
            model_binding=binding,
        )
        self.locators = (first, replace(first, call_item_id="call-item-2", provider_call_id="provider-call-2", node_id="node-2"))
        service = AgentContinuationLocatorService()
        app = FastAPI()

        @app.post("/test-only/agent-continuation")
        async def continue_agent(
            request: _ContinuationRequest,
            x_owner_scope: str = Header(),
        ):
            if x_owner_scope != "owner-1":
                raise HTTPException(status_code=403, detail="agent_continuation_owner_mismatch")
            try:
                selected = service.resolve_unique(
                    self.locators,
                    owner_scope=x_owner_scope,
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    call_item_id=request.call_item_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"call_item_id": selected.call_item_id, "state": "selected"}

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_multiple_waiting_requires_explicit_call_identity(self) -> None:
        ambiguous = await self.client.post(
            "/test-only/agent-continuation",
            headers={"x-owner-scope": "owner-1"},
            json={"conversation_id": "conv-1", "task_id": "task-1"},
        )
        selected = await self.client.post(
            "/test-only/agent-continuation",
            headers={"x-owner-scope": "owner-1"},
            json={
                "conversation_id": "conv-1",
                "task_id": "task-1",
                "call_item_id": "call-item-2",
            },
        )

        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(
            ambiguous.json()["detail"],
            "agent_continuation_locator_ambiguous",
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["call_item_id"], "call-item-2")

    async def test_owner_mismatch_is_rejected_before_locator_selection(self) -> None:
        response = await self.client.post(
            "/test-only/agent-continuation",
            headers={"x-owner-scope": "wrong-owner"},
            json={
                "conversation_id": "conv-1",
                "task_id": "task-1",
                "call_item_id": "call-item-1",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "agent_continuation_owner_mismatch")

    async def test_locator_cache_miss_rebuilds_from_durable_agent_items(self) -> None:
        locator = self.locators[0]
        run = AgentRun(
            run_id=locator.run_id,
            task_id=locator.task_id,
            conversation_id=locator.conversation_id,
            status=AgentRunStatus.WAITING_FOR_INPUT,
            binding=locator.model_binding,
            waiting_call_item_ids=(locator.call_item_id,),
        )
        call = AgentItem(
            item_id=locator.call_item_id,
            run_id=locator.run_id,
            task_id=locator.task_id,
            sequence=1,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json=json.dumps({"node_id": locator.node_id}),
            payload_sha256="0" * 64,
        )
        result = AgentItem(
            item_id="result-item-1",
            run_id=locator.run_id,
            task_id=locator.task_id,
            sequence=2,
            kind=AgentItemKind.TOOL_RESULT,
            state=AgentItemState.RESERVED,
            payload_json=json.dumps(
                {
                    "safe_result": {
                        "continuation_locator": locator.to_safe_dict(),
                    }
                }
            ),
            payload_sha256="1" * 64,
            source_call_item_id=locator.call_item_id,
        )
        trace: list[str] = []
        runtime = SimpleNamespace(
            storage=_DurableLocatorStorage(trace),
            agent_run_repository=_DurableLocatorRepository(
                run,
                (call, result),
                trace,
            ),
        )

        rebuilt = await ApiRuntime._agent_locator_for_node(
            runtime,
            task_id=locator.task_id,
            node_id=locator.node_id,
        )

        self.assertEqual(rebuilt, locator)
        self.assertEqual(
            trace,
            [
                "storage.list_interrupts_for_task",
                "repository.get_run_for_task",
                "repository.list_items",
            ],
        )

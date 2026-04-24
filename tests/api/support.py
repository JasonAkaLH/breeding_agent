from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from src.api.app import create_app
from src.api.runtime import ApiRuntime, build_api_runtime
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


def blocking_mysql_adapter() -> tuple[MySQLReadonlyAdapter, threading.Event]:
    release = threading.Event()

    def _runner(sql: str) -> ReadonlyQueryResult:
        if not release.wait(timeout=10):
            raise TimeoutError(f"Timed out waiting to release blocking SQL runner for {sql!r}.")
        return ReadonlyQueryResult(
            columns=("variety_name",),
            rows=({"variety_name": "龙粳33"},),
            row_count=1,
        )

    return MySQLReadonlyAdapter(runner=_runner), release


class APITestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)
        self.runtime = self.build_runtime()
        await self._bind_client()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.runtime.shutdown()
        self._tmpdir.cleanup()
        await super().asyncTearDown()

    def build_runtime(
        self,
        *,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        sql_generator=None,
        summarizer=None,
        llm_text_generator=None,
        main_agent_stream_generator=None,
        skill_roots=(),
    ) -> ApiRuntime:
        adapter = mysql_adapter or MySQLReadonlyAdapter(
            runner=lambda sql: ReadonlyQueryResult(
                columns=("variety_name",),
                rows=({"variety_name": "龙粳33"},),
                row_count=1,
            )
        )
        return build_api_runtime(
            database_path=self.workspace / "phase6-api.sqlite3",
            audit_log_path=self.workspace / "audit.jsonl",
            mysql_adapter=adapter,
            sql_generator=sql_generator,
            summarizer=summarizer,
            llm_text_generator=llm_text_generator,
            main_agent_stream_generator=main_agent_stream_generator,
            skill_roots=skill_roots,
        )

    async def _bind_client(self) -> None:
        self.app = create_app(runtime=self.runtime)
        self.transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def reconfigure_runtime(
        self,
        *,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        sql_generator=None,
        summarizer=None,
        llm_text_generator=None,
        main_agent_stream_generator=None,
        skill_roots=(),
    ) -> None:
        await self.client.aclose()
        await self.runtime.shutdown()
        self.runtime = self.build_runtime(
            mysql_adapter=mysql_adapter,
            sql_generator=sql_generator,
            summarizer=summarizer,
            llm_text_generator=llm_text_generator,
            main_agent_stream_generator=main_agent_stream_generator,
            skill_roots=skill_roots,
        )
        await self._bind_client()

    async def submit_message(
        self,
        *,
        conversation_id: str = "conv-1",
        account_id: str = "acc-1",
        content: str = "查询某个品种的基因型信息",
        capability_id: str | None = "sql_query.query",
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "account_id": account_id,
                "content": content,
                "routing_mode": "auto",
                "capability_id": capability_id,
                "metadata": {},
            },
        )

    async def wait_for_terminal_task(self, task_id: str, *, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            response = await self.client.get(f"/api/v1/tasks/{task_id}")
            response.raise_for_status()
            payload = response.json()
            if payload["status"] in {"completed", "failed", "cancelled"}:
                return payload
            if asyncio.get_running_loop().time() >= deadline:
                nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
                interrupts = await self.runtime.list_interrupts(task_id)
                raise AssertionError(
                    f"Task {task_id} did not reach a terminal state within {timeout} seconds. "
                    f"latest={payload!r}, "
                    f"nodes={[(node.node_id, str(node.status), node.capability_id) for node in nodes]!r}, "
                    f"interrupts={interrupts!r}"
                )
            await asyncio.sleep(0.02)

    async def wait_for_condition(self, predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if await predicate():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("Condition was not satisfied before timeout.")
            await asyncio.sleep(interval)

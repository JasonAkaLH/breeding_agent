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
        llm_text_generator=None,
        sql_query_llm_config=None,
        sql_query_llm_config_path=None,
        sql_query_llm_client_factory=None,
        sql_query_reasoning_effort=None,
        enable_sql_query_llm: bool | None = None,
        planner_text_generator=None,
        planner_llm_config=None,
        planner_llm_config_path=None,
        planner_llm_client_factory=None,
        planner_reasoning_effort="minimal",
        enable_llm_planner: bool | None = None,
        planner_payload_policies=None,
        main_agent_stream_generator=None,
        main_agent_llm_config=None,
        main_agent_llm_config_path=None,
        main_agent_llm_client_factory=None,
        main_agent_reasoning_effort="minimal",
        conversation_title_generator=None,
        enable_conversation_title_llm: bool | None = None,
        conversation_memory_builder=None,
        enable_conversation_memory: bool = True,
        skill_roots=(),
        auth_captcha_code_generator=lambda: "1234",
    ) -> ApiRuntime:
        adapter = mysql_adapter or MySQLReadonlyAdapter(
            runner=lambda sql: ReadonlyQueryResult(
                columns=("variety_name",),
                rows=({"variety_name": "龙粳33"},),
                row_count=1,
            )
        )
        planner_configured = any(
            value is not None
            for value in (planner_text_generator, planner_llm_config, planner_llm_config_path, planner_llm_client_factory)
        )
        sql_query_llm_configured = any(
            value is not None
            for value in (
                llm_text_generator,
                sql_query_llm_config,
                sql_query_llm_config_path,
                sql_query_llm_client_factory,
            )
        )
        conversation_title_configured = conversation_title_generator is not None
        return build_api_runtime(
            database_path=self.workspace / "phase6-api.sqlite3",
            audit_log_path=self.workspace / "audit.jsonl",
            mysql_adapter=adapter,
            sql_generator=sql_generator,
            llm_text_generator=llm_text_generator,
            sql_query_llm_config=sql_query_llm_config,
            sql_query_llm_config_path=sql_query_llm_config_path,
            sql_query_llm_client_factory=sql_query_llm_client_factory,
            sql_query_reasoning_effort=sql_query_reasoning_effort,
            enable_sql_query_llm=sql_query_llm_configured if enable_sql_query_llm is None else enable_sql_query_llm,
            planner_text_generator=planner_text_generator,
            planner_llm_config=planner_llm_config,
            planner_llm_config_path=planner_llm_config_path,
            planner_llm_client_factory=planner_llm_client_factory,
            planner_reasoning_effort=planner_reasoning_effort,
            enable_llm_planner=planner_configured if enable_llm_planner is None else enable_llm_planner,
            planner_payload_policies=planner_payload_policies,
            main_agent_stream_generator=main_agent_stream_generator,
            main_agent_llm_config=main_agent_llm_config,
            main_agent_llm_config_path=main_agent_llm_config_path,
            main_agent_llm_client_factory=main_agent_llm_client_factory,
            main_agent_reasoning_effort=main_agent_reasoning_effort,
            conversation_title_generator=conversation_title_generator,
            enable_conversation_title_llm=conversation_title_configured if enable_conversation_title_llm is None else enable_conversation_title_llm,
            conversation_memory_builder=conversation_memory_builder,
            enable_conversation_memory=enable_conversation_memory,
            skill_roots=skill_roots,
            auth_captcha_code_generator=auth_captcha_code_generator,
        )

    async def _bind_client(self) -> None:
        self.app = create_app(runtime=self.runtime)
        self.transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")
        await self.runtime.create_user("acc-1", "password1")
        await self.login("acc-1", "password1")

    async def login(self, username: str, password: str) -> httpx.Response:
        captcha = await self.client.post("/api/v1/auth/captcha")
        captcha.raise_for_status()
        response = await self.client.post(
            "/api/v1/auth/login",
            json={
                "username": username,
                "password": password,
                "captcha_id": captcha.json()["captcha_id"],
                "captcha_code": "1234",
            },
        )
        response.raise_for_status()
        return response

    async def logout(self) -> httpx.Response:
        response = await self.client.post("/api/v1/auth/logout")
        self.client.cookies.clear()
        return response

    async def reconfigure_runtime(
        self,
        *,
        mysql_adapter: MySQLReadonlyAdapter | None = None,
        sql_generator=None,
        llm_text_generator=None,
        sql_query_llm_config=None,
        sql_query_llm_config_path=None,
        sql_query_llm_client_factory=None,
        sql_query_reasoning_effort=None,
        enable_sql_query_llm: bool | None = None,
        planner_text_generator=None,
        planner_llm_config=None,
        planner_llm_config_path=None,
        planner_llm_client_factory=None,
        planner_reasoning_effort="minimal",
        enable_llm_planner: bool | None = None,
        planner_payload_policies=None,
        main_agent_stream_generator=None,
        main_agent_llm_config=None,
        main_agent_llm_config_path=None,
        main_agent_llm_client_factory=None,
        main_agent_reasoning_effort="minimal",
        conversation_title_generator=None,
        enable_conversation_title_llm: bool | None = None,
        conversation_memory_builder=None,
        enable_conversation_memory: bool = True,
        skill_roots=(),
        auth_captcha_code_generator=lambda: "1234",
    ) -> None:
        await self.client.aclose()
        await self.runtime.shutdown()
        self.runtime = self.build_runtime(
            mysql_adapter=mysql_adapter,
            sql_generator=sql_generator,
            llm_text_generator=llm_text_generator,
            sql_query_llm_config=sql_query_llm_config,
            sql_query_llm_config_path=sql_query_llm_config_path,
            sql_query_llm_client_factory=sql_query_llm_client_factory,
            sql_query_reasoning_effort=sql_query_reasoning_effort,
            enable_sql_query_llm=enable_sql_query_llm,
            planner_text_generator=planner_text_generator,
            planner_llm_config=planner_llm_config,
            planner_llm_config_path=planner_llm_config_path,
            planner_llm_client_factory=planner_llm_client_factory,
            planner_reasoning_effort=planner_reasoning_effort,
            enable_llm_planner=enable_llm_planner,
            planner_payload_policies=planner_payload_policies,
            main_agent_stream_generator=main_agent_stream_generator,
            main_agent_llm_config=main_agent_llm_config,
            main_agent_llm_config_path=main_agent_llm_config_path,
            main_agent_llm_client_factory=main_agent_llm_client_factory,
            main_agent_reasoning_effort=main_agent_reasoning_effort,
            conversation_title_generator=conversation_title_generator,
            enable_conversation_title_llm=enable_conversation_title_llm,
            conversation_memory_builder=conversation_memory_builder,
            enable_conversation_memory=enable_conversation_memory,
            skill_roots=skill_roots,
            auth_captcha_code_generator=auth_captcha_code_generator,
        )
        await self._bind_client()

    async def submit_message(
        self,
        *,
        conversation_id: str = "conv-1",
        account_id: str = "acc-1",
        content: str = "查询某个品种的基因型信息",
        capability_id: str | None = "sql_query.query",
        metadata: dict | None = None,
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "account_id": account_id,
                "content": content,
                "routing_mode": "auto",
                "capability_id": capability_id,
                "metadata": dict(metadata or {}),
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

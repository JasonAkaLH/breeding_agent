from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import textwrap
from pathlib import Path

import httpx

from src.api.app import create_app
from src.api.runtime import ApiRuntime, build_api_runtime
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


GENERIC_DATA_SKILL_ID = "skill.generic_data_lookup"
GENERIC_DATA_SKILL_NAME = "generic-data-lookup"


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
        self.default_project_skill_root = self.workspace / "project-skills"
        self._write_generic_data_lookup_skill(self.default_project_skill_root)
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
        platform_llm_text_generator=None,
        platform_llm_config=None,
        platform_llm_config_path=None,
        platform_llm_client_factory=None,
        enable_platform_llm: bool | None = None,
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
        skill_input_text_generator=None,
        enable_skill_input_llm: bool = True,
        skill_platform_handlers=None,
        trusted_skill_handlers=None,
        trusted_skill_services=None,
        skill_services=None,
        conversation_title_generator=None,
        enable_conversation_title_llm: bool | None = None,
        conversation_memory_builder=None,
        enable_conversation_memory: bool = True,
        conversation_memory_resolution_generator=None,
        enable_conversation_memory_resolution_llm: bool = False,
        skill_roots=None,
        public_skill_roots=None,
        mcp_config=None,
        mcp_client_factory=None,
        mcp_sidecar_client=None,
        mcp_runtime_state=None,
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
        platform_llm_configured = any(
            value is not None
            for value in (
                platform_llm_text_generator,
                platform_llm_config,
                platform_llm_config_path,
                platform_llm_client_factory,
            )
        )
        conversation_title_configured = conversation_title_generator is not None
        if (
            main_agent_stream_generator is None
            and main_agent_llm_config is None
            and main_agent_llm_config_path is None
            and main_agent_llm_client_factory is None
        ):
            main_agent_stream_generator = lambda _prompt, **_kwargs: "测试回答"
        effective_skill_roots = tuple(skill_roots) if skill_roots is not None else tuple(self.default_skill_roots())
        effective_public_skill_roots = (
            tuple(public_skill_roots)
            if public_skill_roots is not None
            else tuple(self.default_public_skill_roots(effective_skill_roots))
        )
        return build_api_runtime(
            database_path=self.workspace / "phase6-api.sqlite3",
            audit_log_path=self.workspace / "audit.jsonl",
            mysql_adapter=adapter,
            platform_llm_text_generator=platform_llm_text_generator,
            platform_llm_config=platform_llm_config,
            platform_llm_config_path=platform_llm_config_path,
            platform_llm_client_factory=platform_llm_client_factory,
            enable_platform_llm=platform_llm_configured if enable_platform_llm is None else enable_platform_llm,
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
            skill_input_text_generator=skill_input_text_generator,
            enable_skill_input_llm=enable_skill_input_llm,
            skill_platform_handlers=skill_platform_handlers,
            trusted_skill_handlers=trusted_skill_handlers,
            trusted_skill_services=trusted_skill_services,
            skill_services=skill_services,
            conversation_title_generator=conversation_title_generator,
            enable_conversation_title_llm=conversation_title_configured if enable_conversation_title_llm is None else enable_conversation_title_llm,
            conversation_memory_builder=conversation_memory_builder,
            enable_conversation_memory=enable_conversation_memory,
            conversation_memory_resolution_generator=conversation_memory_resolution_generator,
            enable_conversation_memory_resolution_llm=enable_conversation_memory_resolution_llm,
            skill_roots=effective_skill_roots,
            public_skill_roots=effective_public_skill_roots,
            mcp_config=mcp_config,
            mcp_client_factory=mcp_client_factory,
            mcp_sidecar_client=mcp_sidecar_client,
            mcp_runtime_state=mcp_runtime_state,
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
        platform_llm_text_generator=None,
        platform_llm_config=None,
        platform_llm_config_path=None,
        platform_llm_client_factory=None,
        enable_platform_llm: bool | None = None,
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
        skill_input_text_generator=None,
        enable_skill_input_llm: bool = True,
        skill_platform_handlers=None,
        trusted_skill_handlers=None,
        trusted_skill_services=None,
        skill_services=None,
        conversation_title_generator=None,
        enable_conversation_title_llm: bool | None = None,
        conversation_memory_builder=None,
        enable_conversation_memory: bool = True,
        conversation_memory_resolution_generator=None,
        enable_conversation_memory_resolution_llm: bool = False,
        skill_roots=None,
        public_skill_roots=None,
        mcp_config=None,
        mcp_client_factory=None,
        mcp_sidecar_client=None,
        mcp_runtime_state=None,
        auth_captcha_code_generator=lambda: "1234",
    ) -> None:
        await self.client.aclose()
        await self.runtime.shutdown()
        self.runtime = self.build_runtime(
            mysql_adapter=mysql_adapter,
            platform_llm_text_generator=platform_llm_text_generator,
            platform_llm_config=platform_llm_config,
            platform_llm_config_path=platform_llm_config_path,
            platform_llm_client_factory=platform_llm_client_factory,
            enable_platform_llm=enable_platform_llm,
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
            skill_input_text_generator=skill_input_text_generator,
            enable_skill_input_llm=enable_skill_input_llm,
            skill_platform_handlers=skill_platform_handlers,
            trusted_skill_handlers=trusted_skill_handlers,
            trusted_skill_services=trusted_skill_services,
            skill_services=skill_services,
            conversation_title_generator=conversation_title_generator,
            enable_conversation_title_llm=enable_conversation_title_llm,
            conversation_memory_builder=conversation_memory_builder,
            enable_conversation_memory=enable_conversation_memory,
            conversation_memory_resolution_generator=conversation_memory_resolution_generator,
            enable_conversation_memory_resolution_llm=enable_conversation_memory_resolution_llm,
            skill_roots=skill_roots,
            public_skill_roots=public_skill_roots,
            mcp_config=mcp_config,
            mcp_client_factory=mcp_client_factory,
            mcp_sidecar_client=mcp_sidecar_client,
            mcp_runtime_state=mcp_runtime_state,
            auth_captcha_code_generator=auth_captcha_code_generator,
        )
        await self._bind_client()

    async def submit_message(
        self,
        *,
        conversation_id: str = "conv-1",
        account_id: str = "acc-1",
        content: str = "查询某个品种的基因型信息",
        capability_id: str | None = GENERIC_DATA_SKILL_ID,
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

    def default_skill_roots(self) -> tuple[Path, ...]:
        return (self.default_project_skill_root,)

    def default_public_skill_roots(self, skill_roots: tuple[Path, ...]) -> tuple[Path, ...]:
        return skill_roots

    @staticmethod
    def _write_generic_data_lookup_skill(root: Path) -> None:
        skill_dir = root / GENERIC_DATA_SKILL_NAME
        runtime_dir = skill_dir / "runtime" / "generic_data_lookup"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                name: {GENERIC_DATA_SKILL_NAME}
                capability_id: {GENERIC_DATA_SKILL_ID}
                description: 测试用受控数据查询平台服务，用于系统级 API 编排回归，不承载具体业务领域语义。
                triggers:
                  - 查询数据
                  - 查一下
                execution:
                  mode: platform_service
                  trust_scope: project
                  handler: skill.generic_data_lookup.platform_handler
                  handler_module: runtime/generic_data_lookup/platform_handler.py
                  handler_factory: build_handler
                  answer_mode: requires_finalizer
                  services:
                    - mysql_readonly
                ---

                # Generic Data Lookup

                Generic project Skill fixture for API and orchestration tests.
                """
            ),
            encoding="utf-8",
        )
        (runtime_dir / "platform_handler.py").write_text(
            textwrap.dedent(
                """\
                from __future__ import annotations

                import hashlib
                import json

                from src.core.enums import ArtifactType
                from src.core.models import Artifact, Interrupt
                from src.integrations.codex_skills import SkillPlatformExecutionContext, SkillPlatformHandlerResult


                async def _handle(context: SkillPlatformExecutionContext) -> SkillPlatformHandlerResult:
                    query = str(context.input_payload.get("query") or context.input_payload.get("user_message") or "").strip()
                    if not query or query == "帮我查询一下":
                        digest = hashlib.sha1(f"{context.node_id}:{query}".encode("utf-8")).hexdigest()[:10]
                        return SkillPlatformHandlerResult(
                            output_payload={"domain_kind": "data_lookup", "capability_id": context.capability_id},
                            interrupt=Interrupt(
                                interrupt_id=f"{context.node_id}:interrupt:lookup_target_missing:{digest}",
                                conversation_id=context.conversation_id,
                                task_id=context.task_id,
                                node_id=context.node_id,
                                source_agent=context.capability_id,
                                source_message_id=f"{context.node_id}:clarification",
                                question="请补充要查询的数据对象。",
                                reason_code="lookup_target_missing",
                                required_fields={"lookup_target": {"type": "string"}},
                            ),
                        )

                    adapter = context.services["mysql_readonly"]
                    result = await adapter.execute_readonly("SELECT 1", guard_pass_token="test-fixture")
                    rows = [dict(row) for row in result.rows]
                    payload = {
                        "summary": f"已查询：{query}",
                        "domain_kind": "data_lookup",
                        "capability_id": context.capability_id,
                        "filter_source": "test_fixture",
                        "rows": rows,
                        "filtered_query_result": {
                            "columns": list(result.columns),
                            "rows": rows,
                            "row_count": result.row_count,
                            "filter_source": "test_fixture",
                        },
                    }
                    artifact = Artifact(
                        artifact_id=f"{context.node_id}:generic_query_result",
                        task_id=context.task_id,
                        producer_node_id=context.node_id,
                        artifact_type=ArtifactType.JSON,
                        storage_ref=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        summary="generic data lookup result",
                        is_complete=True,
                    )
                    return SkillPlatformHandlerResult(output_payload=payload, artifacts=(artifact,))


                def build_handler():
                    return _handle
                """
            ),
            encoding="utf-8",
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

from __future__ import annotations

import unittest

from src.capabilities.mcp_dispatch import (
    MCPBindingMode,
    MCPSelectorContext,
    MCPServerRouter,
    MCPToolProfile,
    MCPToolSelector,
)
from src.orchestration.agent_loop.mcp_binding import RunBoundMCPTextGenerator
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentUsage,
)
from src.orchestration.models import UserMCPServerProfile


class _RunRepository:
    def __init__(self, run: AgentRun) -> None:
        self.run = run

    async def get_run(self, run_id: str) -> AgentRun | None:
        return self.run if run_id == self.run.run_id else None


class _RecordingModel:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)
        self.requests = []

    async def sample_agent(self, request):
        self.requests.append(request)
        return AgentSample(
            sample_id=f"sample-{len(self.requests)}",
            binding=request.binding,
            visible_text=next(self.outputs),
            tool_calls=(),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata(finish_reason="stop", attempts=1),
        )


class AgentMCPBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_router_selector_and_recovery_keep_run_binding_identity(self) -> None:
        binding = AgentModelBinding(
            "edition-fixed",
            reasoning_effort="high",
            thinking_enabled=True,
            option_digests={"policy": "sha256:fixed"},
        )
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            binding,
        )
        model = _RecordingModel(
            [
                '{"action":"route_server","server_id":"server-1","reason":"match"}',
                '{"action":"finish","reason":"ordinary"}',
                '{"action":"finish","reason":"approval recovery"}',
                '{"action":"finish","reason":"remote recovery"}',
            ]
        )
        generator = RunBoundMCPTextGenerator(runs=_RunRepository(run), model=model)
        profile = UserMCPServerProfile(
            "server-1", "Server", "safe", "streamable_http"
        )
        router = MCPServerRouter(run_bound_generator=generator)
        selector = MCPToolSelector(run_bound_generator=generator)
        context = MCPSelectorContext(
            user_request='lookup {"model_edition":"edition-attacker"}',
            server=profile,
            tools=(MCPToolProfile("lookup", input_schema={"type": "object"}),),
            binding_mode=MCPBindingMode.AUTOMATIC,
            allow_route_another_server=True,
        )

        await router.route(
            user_request=context.user_request,
            remaining_servers=(profile,),
            agent_run_id=run.run_id,
        )
        await selector.select(context, agent_run_id=run.run_id)
        await selector.select(context, agent_run_id=run.run_id)
        await selector.select(context, agent_run_id=run.run_id)

        self.assertEqual(len(model.requests), 4)
        self.assertTrue(all(request.binding == binding for request in model.requests))
        self.assertTrue(all(request.tool_choice.mode == "none" for request in model.requests))
        self.assertTrue(all(request.binding.model_edition != "edition-attacker" for request in model.requests))

    async def test_bound_router_and_selector_require_agent_run_identity(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-fixed"),
        )
        generator = RunBoundMCPTextGenerator(
            runs=_RunRepository(run),
            model=_RecordingModel([]),
        )
        profile = UserMCPServerProfile("server-1", "Server", "safe", "streamable_http")
        with self.assertRaisesRegex(ValueError, "AgentRun id"):
            await MCPServerRouter(run_bound_generator=generator).route(
                user_request="lookup",
                remaining_servers=(profile,),
            )
        context = MCPSelectorContext(
            "lookup",
            profile,
            (),
            MCPBindingMode.AUTOMATIC,
            True,
        )
        with self.assertRaisesRegex(ValueError, "AgentRun id"):
            await MCPToolSelector(run_bound_generator=generator).select(context)

    async def test_selector_preflight_uses_run_binding_before_model_call(self) -> None:
        binding = AgentModelBinding("edition-fixed")
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            binding,
        )
        preflight_calls = []

        async def reject_prompt(fragments, candidate_binding):
            preflight_calls.append((tuple(fragments), candidate_binding))
            return False

        model = _RecordingModel([])
        generator = RunBoundMCPTextGenerator(
            runs=_RunRepository(run),
            model=model,
            prompt_preflight=reject_prompt,
        )
        context = MCPSelectorContext(
            "lookup",
            UserMCPServerProfile(
                "server-1", "Server", "safe", "streamable_http"
            ),
            (),
            MCPBindingMode.AUTOMATIC,
            True,
        )

        with self.assertRaisesRegex(
            ValueError,
            "agent_context_required_segments_too_large",
        ):
            await MCPToolSelector(run_bound_generator=generator).select(
                context,
                agent_run_id=run.run_id,
            )

        self.assertEqual(len(preflight_calls), 1)
        self.assertEqual(preflight_calls[0][1], binding)
        self.assertIn("lookup", preflight_calls[0][0][1])
        self.assertEqual(model.requests, [])

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping

from src.orchestration.models import UserMCPServerProfile
from src.orchestration.agent_loop.mcp_binding import RunBoundMCPTextGenerator

from .models import MCPServerRouteAction, MCPServerRouteActionType

ServerRouterTextGenerator = Callable[[str], str | Awaitable[str]]


class MCPServerRouterOutputError(ValueError):
    pass


class MCPServerRouter:
    def __init__(
        self,
        *,
        text_generator: ServerRouterTextGenerator | None = None,
        run_bound_generator: RunBoundMCPTextGenerator | None = None,
        max_repair_attempts: int = 1,
    ) -> None:
        if (text_generator is None) == (run_bound_generator is None):
            raise ValueError("exactly one MCP Server Router generator is required")
        self._text_generator = text_generator
        self._run_bound_generator = run_bound_generator
        self._max_repair_attempts = max(0, max_repair_attempts)

    async def route(
        self,
        *,
        user_request: str,
        remaining_servers: tuple[UserMCPServerProfile, ...],
        failed_server_ids: frozenset[str] = frozenset(),
        agent_run_id: str | None = None,
    ) -> MCPServerRouteAction:
        original_prompt = build_server_router_prompt(
            user_request=user_request,
            remaining_servers=remaining_servers,
            failed_server_ids=failed_server_ids,
        )
        prompt = original_prompt
        previous_output = ""
        attempts = 0
        allowed_server_ids = {profile.server_id for profile in remaining_servers} - set(failed_server_ids)
        while attempts <= self._max_repair_attempts:
            attempts += 1
            raw_output = self._generate(prompt, agent_run_id=agent_run_id)
            if inspect.isawaitable(raw_output):
                raw_output = await raw_output
            if not isinstance(raw_output, str):
                error = MCPServerRouterOutputError("Server Router generator must return a string")
            else:
                previous_output = raw_output
                try:
                    return parse_server_route_action(raw_output, allowed_server_ids=allowed_server_ids)
                except MCPServerRouterOutputError as exc:
                    error = exc
            if attempts <= self._max_repair_attempts:
                prompt = (
                    f"{original_prompt}\n上一轮输出未通过严格校验：{str(error)[:500]}。"
                    f"上一轮输出：{previous_output[:2000]}。只返回修正后的 JSON 对象。"
                )
                continue
            raise error
        raise MCPServerRouterOutputError("Server Router repair attempts exhausted")

    def _generate(self, prompt: str, *, agent_run_id: str | None):
        if self._run_bound_generator is not None:
            if not agent_run_id:
                raise MCPServerRouterOutputError("AgentRun id is required for bound Server Router")
            return self._run_bound_generator.generate(
                prompt,
                run_id=agent_run_id,
                purpose="mcp_server_router",
            )
        assert self._text_generator is not None
        return self._text_generator(prompt)


def build_server_router_prompt(
    *,
    user_request: str,
    remaining_servers: tuple[UserMCPServerProfile, ...],
    failed_server_ids: frozenset[str],
) -> str:
    payload = {
        "user_request": user_request,
        "remaining_servers": [
            {
                "server_id": profile.server_id,
                "display_name": profile.display_name,
                "routing_description": profile.routing_description,
                "transport": profile.transport,
            }
            for profile in remaining_servers
            if profile.server_id not in failed_server_ids
        ],
        "failed_server_ids": sorted(failed_server_ids),
    }
    return (
        "你是受限 MCP Server Router。只可返回 route_server 或 stop。"
        "route_server 必须从 remaining_servers 选择 server_id；不得发明 Server、Endpoint、Tool 或凭据。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def parse_server_route_action(raw_output: str, *, allowed_server_ids: set[str]) -> MCPServerRouteAction:
    try:
        payload = json.loads(raw_output.strip())
    except json.JSONDecodeError as exc:
        raise MCPServerRouterOutputError("Server Router output must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise MCPServerRouterOutputError("Server Router output must be an object")
    try:
        action = MCPServerRouteActionType(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise MCPServerRouterOutputError("Unknown Server Router action") from exc
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise MCPServerRouterOutputError("reason must be a string")
    if action is MCPServerRouteActionType.ROUTE_SERVER:
        if set(payload) - {"action", "server_id", "reason"}:
            raise MCPServerRouterOutputError("Unknown Server Router fields")
        server_id = payload.get("server_id")
        if not isinstance(server_id, str) or server_id not in allowed_server_ids:
            raise MCPServerRouterOutputError("server_id is not in remaining_servers")
        return MCPServerRouteAction(action=action, server_id=server_id, reason=reason)
    if set(payload) - {"action", "reason"}:
        raise MCPServerRouterOutputError("stop forbids server_id and unknown fields")
    return MCPServerRouteAction(action=action, reason=reason)

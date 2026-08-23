from __future__ import annotations

from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
)

from .executor import MCP_DISPATCH_CAPABILITY_ID


MCP_DISPATCH_CAPABILITY_DESCRIPTOR = CapabilityDescriptor(
    capability_id=MCP_DISPATCH_CAPABILITY_ID,
    name="mcp.dispatch",
    description="按当前用户可用的安全 Server Profile 路由到一个 MCP Server；具体 Tool 在执行期按需发现和选择。",
    display_name="MCP Server Dispatch",
    kind="mcp_dispatch",
    source="builtin",
)


def build_local_mcp_dispatch_instance(
    *,
    instance_id: str = "inst-mcp-dispatch-local",
) -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=(MCP_DISPATCH_CAPABILITY_ID,),
        state=InstanceState.ONLINE,
        load_score=0,
    )

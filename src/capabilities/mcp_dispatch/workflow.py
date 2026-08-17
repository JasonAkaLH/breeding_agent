from __future__ import annotations

from src.core.enums import NodeCriticality
from src.orchestration.answer_roles import ANSWER_SCOPE_METADATA_KEY, RESPONSE_ROLE_FINAL, RESPONSE_ROLE_METADATA_KEY
from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
    OrchestrationRequest,
    WorkflowNodePlan,
    WorkflowPlan,
)
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy

from .executor import MCP_DISPATCH_CAPABILITY_ID
from .models import MCPBindingMode

MCP_DISPATCH_CAPABILITY_DESCRIPTOR = CapabilityDescriptor(
    capability_id=MCP_DISPATCH_CAPABILITY_ID,
    name="mcp.dispatch",
    description="按当前用户可用的安全 Server Profile 路由到一个 MCP Server；具体 Tool 在执行期按需发现和选择。",
    display_name="MCP Server Dispatch",
    kind="mcp_dispatch",
    source="builtin",
)

MCP_DISPATCH_PLANNER_PAYLOAD_POLICY = CapabilityPayloadPolicy(
    planner_allowed_fields=("server_id",),
)


def build_local_mcp_dispatch_instance(*, instance_id: str = "inst-mcp-dispatch-local") -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=(MCP_DISPATCH_CAPABILITY_ID,),
        state=InstanceState.ONLINE,
        load_score=0,
    )


class MCPDispatchWorkflowProvider:
    """Rebuilds the original dispatch node after an MCP approval/input interrupt."""

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        server_id = str(request.metadata.get("mcp_dispatch_server_id") or "").strip()
        if not server_id:
            raise ValueError("mcp_dispatch_server_id is required to resume mcp.dispatch")
        try:
            binding_mode = MCPBindingMode(
                str(request.metadata.get("mcp_binding_mode") or MCPBindingMode.AUTOMATIC.value)
            )
        except ValueError as exc:
            raise ValueError("mcp_binding_mode is invalid") from exc
        dispatch_node_id = str(
            request.metadata.get("resume_interrupted_node_id")
            or f"{request.task_id}:mcp_dispatch"
        )
        finalizer_node_id = str(
            request.metadata.get("resume_finalizer_node_id")
            or f"{request.task_id}:main_agent.respond"
        )
        return WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id=dispatch_node_id,
                    capability_id=MCP_DISPATCH_CAPABILITY_ID,
                    input_payload={"server_id": server_id},
                    metadata={
                        "mcp_binding_mode": binding_mode.value,
                        "user_message": request.effective_user_message,
                    },
                    criticality=NodeCriticality.REQUIRED,
                    retry_policy={"max_attempts": 1},
                    timeout_policy={},
                ),
                WorkflowNodePlan(
                    node_id=finalizer_node_id,
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.effective_user_message},
                    metadata={
                        RESPONSE_ROLE_METADATA_KEY: RESPONSE_ROLE_FINAL,
                        ANSWER_SCOPE_METADATA_KEY: "task",
                        "finalizer_source": "mcp_dispatch_workflow_provider",
                    },
                    depends_on=(dispatch_node_id,),
                    criticality=NodeCriticality.REQUIRED,
                    retry_policy={"max_attempts": 1},
                    timeout_policy={"seconds": 60},
                ),
            ),
            metadata={
                "route": "mcp_dispatch",
                "server_id": server_id,
                "mcp_binding_mode": binding_mode.value,
            },
        )

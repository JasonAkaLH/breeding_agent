from __future__ import annotations

from src.core.enums import NodeCriticality
from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


MAIN_AGENT_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="main_agent.respond",
        name="main_agent.respond",
        description="Default LLM-backed main agent response with optional skill injection.",
    ),
)


class MainAgentWorkflowProvider:
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        node_id = f"{request.task_id}:main_agent.respond"
        return WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id=node_id,
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.user_message},
                    criticality=NodeCriticality.REQUIRED,
                    retry_policy={"max_attempts": 1},
                    timeout_policy={"seconds": 60},
                ),
            ),
            metadata={"route": "main_agent"},
            max_replans=0,
            max_dynamic_nodes=0,
        )


def build_local_main_agent_instance(*, instance_id: str = "inst-main-agent-local") -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=tuple(descriptor.capability_id for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS),
        state=InstanceState.ONLINE,
        load_score=0,
    )

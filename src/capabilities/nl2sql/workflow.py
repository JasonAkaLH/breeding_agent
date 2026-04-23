from __future__ import annotations

from src.core.enums import NodeCriticality
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


NL2SQL_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(capability_id="nl2sql.intent_route", name="intent_route", description="Resolve route for NL2SQL."),
    CapabilityDescriptor(
        capability_id="nl2sql.schema_context_prepare",
        name="schema_context_prepare",
        description="Build schema context for NL2SQL generation.",
    ),
    CapabilityDescriptor(capability_id="nl2sql.sql_generate", name="sql_generate", description="Generate candidate SQL."),
    CapabilityDescriptor(capability_id="nl2sql.sql_guard", name="sql_guard", description="Validate readonly SQL guard."),
    CapabilityDescriptor(
        capability_id="nl2sql.sql_execute_readonly",
        name="sql_execute_readonly",
        description="Execute readonly SQL via adapter.",
    ),
    CapabilityDescriptor(
        capability_id="nl2sql.result_summarize",
        name="result_summarize",
        description="Summarize readonly SQL result for end user.",
    ),
)


class NL2SQLWorkflowProvider:
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        task_id = request.task_id
        node_intent = f"{task_id}:intent_route"
        node_schema = f"{task_id}:schema_context_prepare"
        node_generate = f"{task_id}:sql_generate"
        node_guard = f"{task_id}:sql_guard"
        node_execute = f"{task_id}:sql_execute_readonly"
        node_summary = f"{task_id}:result_summarize"

        nodes = (
            WorkflowNodePlan(
                node_id=node_intent,
                capability_id="nl2sql.intent_route",
                input_payload={"user_question": request.user_message},
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 10},
            ),
            WorkflowNodePlan(
                node_id=node_schema,
                capability_id="nl2sql.schema_context_prepare",
                depends_on=(node_intent,),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 15},
            ),
            WorkflowNodePlan(
                node_id=node_generate,
                capability_id="nl2sql.sql_generate",
                depends_on=(node_intent, node_schema),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 30},
            ),
            WorkflowNodePlan(
                node_id=node_guard,
                capability_id="nl2sql.sql_guard",
                depends_on=(node_generate,),
                retry_policy={"max_attempts": 0},
                timeout_policy={"seconds": 5},
            ),
            WorkflowNodePlan(
                node_id=node_execute,
                capability_id="nl2sql.sql_execute_readonly",
                depends_on=(node_guard,),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 60},
            ),
            WorkflowNodePlan(
                node_id=node_summary,
                capability_id="nl2sql.result_summarize",
                depends_on=(node_execute,),
                criticality=NodeCriticality.REQUIRED,
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 20},
            ),
        )
        return WorkflowPlan(task_id=task_id, nodes=nodes, metadata={"route": "nl2sql"}, max_replans=1, max_dynamic_nodes=3)

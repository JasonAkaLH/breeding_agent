from __future__ import annotations

from src.core.enums import NodeCriticality
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="sql_query.query",
        name="SQLQuery",
        description="Safely answer a natural-language data question through the fixed SQLQuery workflow.",
        public=True,
    ),
)


SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="sql_query.intent_route",
        name="intent_route",
        description="Resolve route for SQLQuery.",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.schema_context_prepare",
        name="schema_context_prepare",
        description="Build schema context for SQLQuery generation.",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_generate",
        name="sql_generate",
        description="Generate candidate SQL.",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_guard",
        name="sql_guard",
        description="Validate readonly SQL guard.",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_execute_readonly",
        name="sql_execute_readonly",
        description="Execute readonly SQL via adapter.",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.result_summarize",
        name="result_summarize",
        description="Summarize readonly SQL result for end user.",
        public=False,
    ),
)


class SQLQueryWorkflowProvider:
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
                capability_id="sql_query.intent_route",
                input_payload={"user_question": request.user_message},
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 10},
            ),
            WorkflowNodePlan(
                node_id=node_schema,
                capability_id="sql_query.schema_context_prepare",
                depends_on=(node_intent,),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 15},
            ),
            WorkflowNodePlan(
                node_id=node_generate,
                capability_id="sql_query.sql_generate",
                depends_on=(node_intent, node_schema),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 30},
            ),
            WorkflowNodePlan(
                node_id=node_guard,
                capability_id="sql_query.sql_guard",
                depends_on=(node_generate,),
                retry_policy={"max_attempts": 0},
                timeout_policy={"seconds": 5},
            ),
            WorkflowNodePlan(
                node_id=node_execute,
                capability_id="sql_query.sql_execute_readonly",
                depends_on=(node_guard,),
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 60},
            ),
            WorkflowNodePlan(
                node_id=node_summary,
                capability_id="sql_query.result_summarize",
                depends_on=(node_execute, node_generate),
                criticality=NodeCriticality.REQUIRED,
                retry_policy={"max_attempts": 1},
                timeout_policy={"seconds": 20},
            ),
        )
        return WorkflowPlan(
            task_id=task_id,
            nodes=nodes,
            metadata={
                "route": "sql_query",
                "public_capability_id": "sql_query.query",
            },
            max_replans=1,
            max_dynamic_nodes=3,
        )

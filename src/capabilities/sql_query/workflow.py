from __future__ import annotations

from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy


SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="sql_query.query",
        name="SQLQuery",
        description="通过固定 SQLQuery 工作流安全回答自然语言数据查询。",
        public=True,
    ),
)

SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES = {
    "sql_query.query": CapabilityPayloadPolicy(
        system_payload_factory=lambda request: {"user_question": request.user_message},
    ),
}


SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="sql_query.intent_route",
        name="intent_route",
        description="解析 SQLQuery 路由。",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.schema_context_prepare",
        name="schema_context_prepare",
        description="为 SQLQuery 生成准备 schema 上下文。",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_generate",
        name="sql_generate",
        description="生成候选 SQL。",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_guard",
        name="sql_guard",
        description="校验只读 SQL 安全规则。",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.sql_execute_readonly",
        name="sql_execute_readonly",
        description="通过适配器执行只读 SQL。",
        public=False,
    ),
    CapabilityDescriptor(
        capability_id="sql_query.result_filtering",
        name="result_filtering",
        description="从宽召回的只读 SQL 结果候选中筛选真正符合用户需求的行。",
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
        node_filtering = f"{task_id}:result_filtering"

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
                node_id=node_filtering,
                capability_id="sql_query.result_filtering",
                depends_on=(node_execute, node_generate),
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
            max_dynamic_nodes=24,
        )

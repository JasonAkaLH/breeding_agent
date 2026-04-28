from __future__ import annotations

import asyncio
import json
import unittest

from src.capabilities.main_agent.runtime_replanner import MainAgentRuntimeReplanner
from src.capabilities.main_agent.workflow import MAIN_AGENT_CAPABILITY_DESCRIPTORS, MAIN_AGENT_PLANNER_PAYLOAD_POLICIES
from src.capabilities.sql_query.workflow import SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS, SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES, SQLQueryWorkflowProvider
from src.core.enums import NodeStatus
from src.core.models import TaskNode
from src.orchestration.completion_policy import CompletionStatus
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.runtime_replanner import RuntimeReplanContext


class MainAgentRuntimeReplannerTest(unittest.TestCase):
    def _registry(self) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        for descriptor in (*MAIN_AGENT_CAPABILITY_DESCRIPTORS, *SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS):
            if descriptor.capability_id.startswith("main_agent"):
                registry.register(descriptor, planner_payload_policy=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id))
            else:
                registry.register(descriptor, planner_payload_policy=SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id))
        # internal descriptors are validated after macro expansion in production; the
        # unit test focuses on public replan construction.
        from src.capabilities.sql_query.workflow import SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS

        for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS:
            registry.register(descriptor)
        return registry

    def test_llm_runtime_replanner_returns_expanded_revised_plan_from_unsatisfied_output(self) -> None:
        calls: list[dict] = []

        async def text_generator(prompt: str, *, request=None, stage: str | None = None) -> str:
            calls.append({"prompt": prompt, "request": request, "stage": stage})
            return json.dumps(
                {
                    "action": "replan",
                    "reason": "split query after empty result",
                    "nodes": [
                        {"node_id": "query_again", "capability_id": "sql_query.query"},
                        {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_again"]},
                    ],
                },
                ensure_ascii=False,
            )

        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33",
        )
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(
                task_id="task-1",
                nodes=(WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),),
                max_replans=1,
                max_dynamic_nodes=24,
            ),
            nodes={
                "filter": TaskNode(
                    node_id="filter",
                    task_id="task-1",
                    capability_id="sql_query.result_filtering",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                "filter": {
                    "row_count": 0,
                    "satisfaction": {"satisfied": False, "reason_code": "empty_result", "replan_recommended": True},
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "split query after empty result")
        self.assertEqual(decision.metadata["replan_source"], "main_agent_llm_runtime")
        self.assertEqual(calls[0]["stage"], "orchestration_replan")
        capabilities = [node.capability_id for node in decision.plan.nodes]
        self.assertIn("sql_query.intent_route", capabilities)
        self.assertEqual(decision.plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertEqual(decision.plan.metadata["runtime_replan_source"], "main_agent_llm_runtime")

    def test_does_not_call_llm_when_outputs_are_satisfied(self) -> None:
        calls: list[str] = []

        async def text_generator(prompt: str, **_: object) -> str:
            calls.append(prompt)
            return json.dumps({"action": "none"})

        request = OrchestrationRequest(task_id="task-2", conversation_id="conv-1", root_message_id="msg-1", user_message="你好")
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(task_id="task-2", nodes=(), max_replans=1, max_dynamic_nodes=1),
            nodes={},
            node_outputs={"answer": {"satisfaction": {"satisfied": True, "replan_recommended": False}}},
            completion_status=CompletionStatus.COMPLETED,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNone(decision)
        self.assertEqual(calls, [])

    def test_replan_prompt_uses_sanitized_observation_without_sensitive_outputs(self) -> None:
        prompts: list[str] = []

        async def text_generator(prompt: str, **_: object) -> str:
            prompts.append(prompt)
            return json.dumps({"action": "none"})

        request = OrchestrationRequest(task_id="task-sensitive", conversation_id="conv-1", root_message_id="msg-1", user_message="查询龙粳33")
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(task_id="task-sensitive", nodes=(), max_replans=1, max_dynamic_nodes=1),
            nodes={
                "filter": TaskNode(
                    node_id="filter",
                    task_id="task-sensitive",
                    capability_id="sql_query.result_filtering",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                "filter": {
                    "sql": "SELECT * FROM secret_table",
                    "guard_pass_token": "SECRET_TOKEN_SHOULD_NOT_LEAK",
                    "schema_ddl": "CREATE TABLE secret_table(secret text)",
                    "rows": [
                        {"variety_name": "龙粳33", "very_long_detail": "x" * 500},
                        {"variety_name": "龙粳34", "very_long_detail": "y" * 500},
                        {"variety_name": "龙粳35", "very_long_detail": "z" * 500},
                    ],
                    "row_count": 3,
                    "route_id": "genotype_db",
                    "satisfaction": {"satisfied": False, "reason_code": "no_relevant_rows_after_filtering", "replan_recommended": True},
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNone(decision)
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("row_sample", prompt)
        self.assertIn("龙粳33", prompt)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_LEAK", prompt)
        self.assertNotIn("SELECT * FROM secret_table", prompt)
        self.assertNotIn("CREATE TABLE secret_table", prompt)
        self.assertNotIn("龙粳35", prompt)
        self.assertNotIn("x" * 500, prompt)


if __name__ == "__main__":
    unittest.main()

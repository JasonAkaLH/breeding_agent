from __future__ import annotations

import unittest

from src.capabilities.sql_query import SQLQueryWorkflowProvider
from src.capabilities.sql_query.runtime_replanner import SQLQueryRuntimeReplanner
from src.core.enums import NodeStatus
from src.core.models import TaskNode
from src.orchestration.completion_policy import CompletionStatus
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.runtime_replanner import RuntimeReplanContext


class SQLQueryRuntimeReplannerTest(unittest.TestCase):
    def test_splits_multi_crop_region_sql_query_into_public_macro_branches(self) -> None:
        replanner = SQLQueryRuntimeReplanner(macro_providers={"sql_query.query": SQLQueryWorkflowProvider()})
        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="你帮我查一下适合河南种的水稻和适合浙江中的玉米\n补充信息：route_id=审定品种库",
        )
        current_plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),),
            metadata={"auto_strategy": "deterministic_sql_query_then_main_agent"},
            max_replans=1,
            max_dynamic_nodes=24,
        )
        context = RuntimeReplanContext(
            request=request,
            plan=current_plan,
            nodes={
                "task-1:query_data:sql_generate": TaskNode(
                    node_id="task-1:query_data:sql_generate",
                    task_id="task-1",
                    capability_id="sql_query.sql_generate",
                    status=NodeStatus.COMPLETED,
                ),
                "task-1:query_data:sql_execute_readonly": TaskNode(
                    node_id="task-1:query_data:sql_execute_readonly",
                    task_id="task-1",
                    capability_id="sql_query.sql_execute_readonly",
                    status=NodeStatus.COMPLETED,
                ),
                "task-1:query_data:result_filtering": TaskNode(
                    node_id="task-1:query_data:result_filtering",
                    task_id="task-1",
                    capability_id="sql_query.result_filtering",
                    status=NodeStatus.COMPLETED,
                ),
            },
            node_outputs={
                "task-1:query_data:sql_generate": {"sql": "SELECT ..."},
                "task-1:query_data:sql_execute_readonly": {"row_count": 25947},
                "task-1:query_data:result_filtering": {
                    "row_count": 0,
                    "filter_reason": "没有水稻",
                    "satisfaction": {
                        "satisfied": False,
                        "reason_code": "no_relevant_rows_after_filtering",
                        "replan_recommended": True,
                    },
                },
            },
            completion_status=CompletionStatus.COMPLETED,
        )

        decision = replanner.build_replan(context)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "split_multi_intent_sql_query")
        intent_questions = [
            node.input_payload.get("user_question")
            for node in decision.plan.nodes
            if node.capability_id == "sql_query.intent_route"
        ]
        self.assertEqual(
            intent_questions,
            [
                "查询适合河南种植的水稻\n补充信息：route_id=审定品种库",
                "查询适合浙江种植的玉米\n补充信息：route_id=审定品种库",
            ],
        )
        self.assertEqual(sum(1 for node in decision.plan.nodes if node.capability_id == "sql_query.result_filtering"), 2)
        final_node = decision.plan.nodes[-1]
        self.assertEqual(final_node.capability_id, "main_agent.respond")
        self.assertEqual(len(final_node.depends_on), 2)
        self.assertEqual(decision.plan.metadata["runtime_replan_strategy"], "split_sql_query_subquestions")

    def test_does_not_split_after_multi_branch_results_already_exist(self) -> None:
        replanner = SQLQueryRuntimeReplanner(macro_providers={"sql_query.query": SQLQueryWorkflowProvider()})
        request = OrchestrationRequest(
            task_id="task-2",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查适合河南种植的水稻和适合浙江种植的玉米",
        )
        nodes = {
            f"filter_{index}": TaskNode(
                node_id=f"filter_{index}",
                task_id="task-2",
                capability_id="sql_query.result_filtering",
                status=NodeStatus.COMPLETED,
            )
            for index in (1, 2)
        }
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(task_id="task-2", nodes=(), max_replans=1, max_dynamic_nodes=24),
            nodes=nodes,
            node_outputs={node_id: {"row_count": 1} for node_id in nodes},
            completion_status=CompletionStatus.COMPLETED,
        )

        self.assertIsNone(replanner.build_replan(context))

    def test_does_not_split_when_single_macro_output_reports_satisfied(self) -> None:
        replanner = SQLQueryRuntimeReplanner(macro_providers={"sql_query.query": SQLQueryWorkflowProvider()})
        request = OrchestrationRequest(
            task_id="task-satisfied",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查适合河南种植的水稻和适合浙江种植的玉米",
        )
        result_node_id = "task-satisfied:query_data:result_filtering"
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(
                task_id="task-satisfied",
                nodes=(WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),),
                metadata={
                    "auto_strategy": "deterministic_sql_query_then_main_agent",
                    "expanded_macro_nodes": {
                        "query_data": {
                            "capability_id": "sql_query.query",
                            "tail_node_ids": (result_node_id,),
                        }
                    },
                },
                max_replans=1,
                max_dynamic_nodes=24,
            ),
            nodes={
                result_node_id: TaskNode(
                    node_id=result_node_id,
                    task_id="task-satisfied",
                    capability_id="sql_query.result_filtering",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                result_node_id: {
                    "row_count": 2,
                    "satisfaction": {"satisfied": True, "replan_recommended": False},
                }
            },
            completion_status=CompletionStatus.COMPLETED,
        )

        self.assertIsNone(replanner.build_replan(context))


if __name__ == "__main__":
    unittest.main()

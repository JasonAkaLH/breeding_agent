from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from src.core.enums import NodeStatus
from src.orchestration.completion_policy import CompletionStatus
from src.orchestration.models import WorkflowNodePlan, WorkflowPlan
from src.orchestration.runtime_replanner import RuntimeReplanContext, RuntimeReplanDecision
from src.orchestration.workflow_expander import WorkflowExpander


_CROP_TERMS = ("水稻", "玉米", "棉花", "小麦", "大豆")
_ROUTE_ID_PATTERN = re.compile(r"route_id\s*[=:：]\s*([^\s，,。；;]+)", re.IGNORECASE)
_REGION_CROP_PATTERN = re.compile(
    r"适合(?:在)?(?P<region>[\u4e00-\u9fff]{2,8}?)(?:种植|种的|种|栽培|中的|中)?(?:的)?(?P<crop>水稻|玉米|棉花|小麦|大豆)"
)


@dataclass(slots=True, frozen=True)
class SplitSubquestion:
    region: str
    crop: str
    question: str


class SQLQueryRuntimeReplanner:
    """SQLQuery-specific runtime replan advisor.

    This class is intentionally located in the SQLQuery capability package: it
    knows SQLQuery's public macro id, tail-node output shape, and agriculture
    query decomposition rules. The orchestration layer only applies the returned
    revised WorkflowPlan under generic DAG/budget validation.
    """

    def __init__(self, *, macro_providers: Mapping[str, Any]) -> None:
        self._expander = WorkflowExpander(macro_providers)

    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.unresolved_interrupt:
            return None
        if context.replan_count > 0:
            return None
        if context.completion_status not in {CompletionStatus.RUNNING, CompletionStatus.COMPLETED}:
            return None
        if context.plan.metadata.get("runtime_replan_strategy") == "split_sql_query_subquestions":
            return None

        subquestions = self._extract_crop_region_subquestions(context.request.user_message)
        if len(subquestions) < 2:
            return None
        if self._completed_sql_query_result_count(context) >= len(subquestions):
            return None
        if not self._has_single_sql_query_macro_result(context):
            return None
        if not self._node_outputs_recommend_split(context):
            return None

        high_level = self._build_split_sql_query_plan(context, subquestions)
        expanded = self._expander.expand(high_level, request=context.request)
        return RuntimeReplanDecision(
            plan=expanded,
            reason="split_multi_intent_sql_query",
            metadata={
                "subquestion_count": len(subquestions),
                "subquestions": tuple(item.question for item in subquestions),
            },
        )

    @classmethod
    def _extract_crop_region_subquestions(cls, user_message: str) -> tuple[SplitSubquestion, ...]:
        route_suffix = cls._route_suffix(user_message)
        seen: set[tuple[str, str]] = set()
        subquestions: list[SplitSubquestion] = []
        for match in _REGION_CROP_PATTERN.finditer(user_message):
            region = cls._clean_region(match.group("region"))
            crop = match.group("crop")
            if not region or crop not in _CROP_TERMS:
                continue
            key = (region, crop)
            if key in seen:
                continue
            seen.add(key)
            question = f"查询适合{region}种植的{crop}"
            if route_suffix:
                question = f"{question}\n{route_suffix}"
            subquestions.append(SplitSubquestion(region=region, crop=crop, question=question))
        return tuple(subquestions)

    @staticmethod
    def _clean_region(region: str) -> str:
        return region.strip(" 的在和及与、，,。；;\n\t")

    @staticmethod
    def _route_suffix(user_message: str) -> str:
        explicit = _ROUTE_ID_PATTERN.search(user_message)
        if explicit:
            return f"补充信息：route_id={explicit.group(1).strip()}"
        if "审定品种库" in user_message or "审定库" in user_message or "品种库" in user_message:
            return "补充信息：route_id=审定品种库"
        if "基因型数据库" in user_message or "基因型库" in user_message:
            return "补充信息：route_id=基因型数据库"
        return ""

    @staticmethod
    def _completed_sql_query_result_count(context: RuntimeReplanContext) -> int:
        macro_nodes = context.plan.metadata.get("expanded_macro_nodes")
        if isinstance(macro_nodes, Mapping):
            count = 0
            for macro_info in macro_nodes.values():
                if not isinstance(macro_info, Mapping) or macro_info.get("capability_id") != "sql_query.query":
                    continue
                tail_node_ids = tuple(str(node_id) for node_id in macro_info.get("tail_node_ids", ()))
                if tail_node_ids and all(
                    context.nodes.get(node_id) is not None
                    and context.nodes[node_id].status == NodeStatus.COMPLETED
                    for node_id in tail_node_ids
                ):
                    count += 1
            return count

        return sum(
            1
            for node in context.nodes.values()
            if node.status == NodeStatus.COMPLETED and str(node.capability_id) == "sql_query.result_filtering"
        )

    @staticmethod
    def _has_single_sql_query_macro_result(context: RuntimeReplanContext) -> bool:
        macro_nodes = context.plan.metadata.get("expanded_macro_nodes")
        if isinstance(macro_nodes, Mapping):
            completed_count = SQLQueryRuntimeReplanner._completed_sql_query_result_count(context)
            return completed_count == 1

        filtering_nodes = [
            node
            for node in context.nodes.values()
            if str(node.capability_id) == "sql_query.result_filtering" and node.status == NodeStatus.COMPLETED
        ]
        if len(filtering_nodes) == 1:
            return True
        return context.plan.metadata.get("auto_strategy") == "deterministic_sql_query_then_main_agent"

    @staticmethod
    def _node_outputs_recommend_split(context: RuntimeReplanContext) -> bool:
        tail_outputs = SQLQueryRuntimeReplanner._sql_query_tail_outputs(context)
        if not tail_outputs:
            return False
        return any(SQLQueryRuntimeReplanner._output_recommends_replan(output) for output in tail_outputs)

    @staticmethod
    def _sql_query_tail_outputs(context: RuntimeReplanContext) -> tuple[Mapping[str, Any], ...]:
        macro_nodes = context.plan.metadata.get("expanded_macro_nodes")
        outputs: list[Mapping[str, Any]] = []
        if isinstance(macro_nodes, Mapping):
            for macro_info in macro_nodes.values():
                if not isinstance(macro_info, Mapping) or macro_info.get("capability_id") != "sql_query.query":
                    continue
                for node_id in tuple(str(node_id) for node_id in macro_info.get("tail_node_ids", ())):
                    node = context.nodes.get(node_id)
                    output = context.node_outputs.get(node_id)
                    if node is not None and node.status == NodeStatus.COMPLETED and isinstance(output, Mapping):
                        outputs.append(output)
            return tuple(outputs)

        for node_id, node in context.nodes.items():
            output = context.node_outputs.get(node_id)
            if (
                str(node.capability_id) == "sql_query.result_filtering"
                and node.status == NodeStatus.COMPLETED
                and isinstance(output, Mapping)
            ):
                outputs.append(output)
        return tuple(outputs)

    @staticmethod
    def _output_recommends_replan(output: Mapping[str, Any]) -> bool:
        satisfaction = output.get("satisfaction")
        if isinstance(satisfaction, Mapping):
            if satisfaction.get("replan_recommended") is True:
                return True
            if satisfaction.get("satisfied") is False and satisfaction.get("reason_code") in {
                "empty_result",
                "no_relevant_rows_after_filtering",
            }:
                return True
            return False
        row_count = output.get("row_count")
        try:
            return int(row_count) == 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _build_split_sql_query_plan(context: RuntimeReplanContext, subquestions: tuple[SplitSubquestion, ...]) -> WorkflowPlan:
        query_nodes: list[WorkflowNodePlan] = []
        for index, subquestion in enumerate(subquestions, start=1):
            query_nodes.append(
                WorkflowNodePlan(
                    node_id=f"runtime_query_{context.replan_count + 1}_{index}",
                    capability_id="sql_query.query",
                    input_payload={"user_question": subquestion.question},
                )
            )
        answer_node = WorkflowNodePlan(
            node_id=f"runtime_answer_{context.replan_count + 1}",
            capability_id="main_agent.respond",
            input_payload={"user_message": context.request.user_message},
            depends_on=tuple(node.node_id for node in query_nodes),
        )
        return WorkflowPlan(
            task_id=context.plan.task_id,
            nodes=(*query_nodes, answer_node),
            metadata={
                **dict(context.plan.metadata),
                "runtime_replan_strategy": "split_sql_query_subquestions",
                "runtime_replan_source": "sql_query_runtime_replanner",
                "runtime_replan_subquestions": tuple(item.question for item in subquestions),
            },
            max_replans=context.plan.max_replans,
            max_dynamic_nodes=context.plan.max_dynamic_nodes,
        )

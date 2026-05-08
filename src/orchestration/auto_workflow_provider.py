from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

from src.sql_query.route_understanding import QuerySubquestion, QueryUnderstandingResult, QueryUnderstandingService

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from .workflow_expander import WorkflowExpander


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        ...


class AutoWorkflowProvider:
    """Build a user-facing workflow without requiring manual capability selection.

    Database-like agricultural questions are understood through the same
    configuration-backed route service used by SQLQuery itself.  Simple
    database questions expand to one public SQLQuery macro plus a main-agent
    finalizer; composite database questions expand to multiple public SQLQuery
    macro branches and a single finalizer.  Plain chat remains a main-agent
    only path, keeping the UI capability-free while preserving public capability
    boundaries.
    """

    def __init__(
        self,
        *,
        main_agent_provider: WorkflowProvider,
        macro_providers: Mapping[str, WorkflowProvider],
        query_understanding: QueryUnderstandingService | None = None,
    ) -> None:
        self._main_agent_provider = main_agent_provider
        self._macro_providers = dict(macro_providers)
        self._expander = WorkflowExpander(self._macro_providers)
        self._query_understanding = query_understanding or QueryUnderstandingService.from_yaml_file(
            Path(__file__).resolve().parents[2] / "configs/sql_query/routing_rules.yaml"
        )

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        effective_question = request.effective_user_message
        understanding = self._query_understanding.understand(effective_question)
        if not understanding.should_use_sql_query:
            return self._main_agent_provider.build_plan(request)
        if understanding.needs_decomposition and understanding.subquestions:
            return self._build_decomposed_sql_query_then_main_agent_plan(request, understanding=understanding)
        return self._build_sql_query_then_main_agent_plan(request)

    def _build_sql_query_then_main_agent_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        high_level_plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id="query_data",
                    capability_id="sql_query.query",
                    input_payload={"user_question": request.effective_user_message},
                ),
                WorkflowNodePlan(
                    node_id=f"{request.task_id}:main_agent.respond",
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.effective_user_message},
                    depends_on=("query_data",),
                ),
            ),
            metadata={
                "route": "auto",
                "auto_strategy": "deterministic_sql_query_then_main_agent",
                "public_capability_ids": ("sql_query.query", "main_agent.respond"),
            },
            max_replans=0,
            max_dynamic_nodes=0,
        )
        expanded = self._expander.expand(high_level_plan, request=request)
        return WorkflowPlan(
            task_id=expanded.task_id,
            nodes=expanded.nodes,
            metadata={
                **dict(expanded.metadata),
                "route": "auto",
                "auto_strategy": "deterministic_sql_query_then_main_agent",
            },
            max_replans=expanded.max_replans,
            max_dynamic_nodes=expanded.max_dynamic_nodes,
        )

    def _build_decomposed_sql_query_then_main_agent_plan(
        self,
        request: OrchestrationRequest,
        *,
        understanding: QueryUnderstandingResult,
    ) -> WorkflowPlan:
        query_nodes = tuple(
            WorkflowNodePlan(
                node_id=self._subquestion_node_id(subquestion, index),
                capability_id="sql_query.query",
                input_payload=subquestion.as_dict(),
            )
            for index, subquestion in enumerate(understanding.subquestions, start=1)
        )
        high_level_plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                *query_nodes,
                WorkflowNodePlan(
                    node_id=f"{request.task_id}:main_agent.respond",
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.effective_user_message},
                    depends_on=tuple(node.node_id for node in query_nodes),
                ),
            ),
            metadata={
                "route": "auto",
                "auto_strategy": "deterministic_sql_query_decomposed_then_main_agent",
                "public_capability_ids": ("sql_query.query", "main_agent.respond"),
                "decomposition_count": len(query_nodes),
                "route_candidates": [candidate.as_dict() for candidate in understanding.candidate_routes],
            },
            max_replans=0,
            max_dynamic_nodes=0,
        )
        expanded = self._expander.expand(high_level_plan, request=request)
        return WorkflowPlan(
            task_id=expanded.task_id,
            nodes=expanded.nodes,
            metadata={
                **dict(expanded.metadata),
                "route": "auto",
                "auto_strategy": "deterministic_sql_query_decomposed_then_main_agent",
                "decomposition_count": len(query_nodes),
            },
            max_replans=expanded.max_replans,
            max_dynamic_nodes=expanded.max_dynamic_nodes,
        )

    @staticmethod
    def _subquestion_node_id(subquestion: QuerySubquestion, index: int) -> str:
        if subquestion.route_hint == "approval_variety_db":
            return "query_approval_info"
        if subquestion.route_hint == "genotype_db":
            return "query_genotype_info"
        return f"query_data_{index}"

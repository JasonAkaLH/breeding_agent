from __future__ import annotations

import re
from typing import Mapping, Protocol

from .models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from .workflow_expander import WorkflowExpander


class WorkflowProvider(Protocol):
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        ...


_DATA_ACTION_KEYWORDS = (
    "查",
    "查询",
    "检索",
    "搜索",
    "统计",
    "找",
    "列出",
    "看一下",
    "多少",
    "有哪些",
)
_EXPLICIT_SQL_QUERY_SOURCE_KEYWORDS = (
    "sqlquery",
    "审定库",
    "品种库",
    "基因型库",
    "基因型数据库",
    "审定品种库",
)
_GENERIC_DATA_SOURCE_KEYWORDS = ("sql", "数据库", "数据表")
_SQL_QUERY_DOMAIN_KEYWORDS = (
    "品种",
    "审定",
    "基因型",
    "基因",
    "籼粳",
    "籼",
    "粳",
    "水稻",
    "稻",
    "玉米",
    "小麦",
    "棉花",
    "大豆",
    "qtn",
    "snp",
    "位点",
    "变异",
    "材料",
    "亲本",
)
_VARIETY_NAME_PATTERN = re.compile(r"龙[粳稻]\s*\d+", re.IGNORECASE)


class AutoWorkflowProvider:
    """Build a user-facing workflow without requiring manual capability selection.

    The first production slice is intentionally deterministic: use the public
    SQLQuery macro for database-like agricultural questions, then let the main
    agent turn the upstream result into a conversational final answer.  Plain
    chat remains a single main-agent node.  This keeps the UI capability-free
    while preserving the existing public capability boundary.
    """

    def __init__(
        self,
        *,
        main_agent_provider: WorkflowProvider,
        macro_providers: Mapping[str, WorkflowProvider],
    ) -> None:
        self._main_agent_provider = main_agent_provider
        self._macro_providers = dict(macro_providers)
        self._expander = WorkflowExpander(self._macro_providers)

    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        if not self._should_use_sql_query(request.user_message):
            return self._main_agent_provider.build_plan(request)
        return self._build_sql_query_then_main_agent_plan(request)

    def _build_sql_query_then_main_agent_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        high_level_plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id="query_data",
                    capability_id="sql_query.query",
                    input_payload={"user_question": request.user_message},
                ),
                WorkflowNodePlan(
                    node_id=f"{request.task_id}:main_agent.respond",
                    capability_id="main_agent.respond",
                    input_payload={"user_message": request.user_message},
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

    @classmethod
    def _should_use_sql_query(cls, user_message: str) -> bool:
        normalized = user_message.strip().lower()
        if not normalized:
            return False
        if _VARIETY_NAME_PATTERN.search(normalized):
            return True
        has_action = any(keyword in normalized for keyword in _DATA_ACTION_KEYWORDS)
        has_sql_query_domain = any(keyword in normalized for keyword in _SQL_QUERY_DOMAIN_KEYWORDS)
        has_explicit_sql_query_source = any(keyword in normalized for keyword in _EXPLICIT_SQL_QUERY_SOURCE_KEYWORDS)
        has_generic_data_source = any(keyword in normalized for keyword in _GENERIC_DATA_SOURCE_KEYWORDS)
        if has_explicit_sql_query_source and (has_action or has_sql_query_domain):
            return True
        return has_action and (has_sql_query_domain or has_explicit_sql_query_source or has_generic_data_source)

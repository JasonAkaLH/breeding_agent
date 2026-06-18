from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from typing import Any

from src.core.enums import NodeStatus
from src.orchestration.completion_policy import CompletionStatus
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.planner_contract import PlannerOutputError, TextGenerator, parse_planner_output
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy, PlannerPayloadPolicy
from src.orchestration.prompt_envelope import PromptSegment
from src.orchestration.prompt_profiles import (
    PROMPT_PROFILE_TEMPLATE_VERSION,
    coerce_profile_trim_max_tokens,
    resolve_profile_prompt_for_mode,
)
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.runtime_replanner import RuntimeReplanContext, RuntimeReplanDecision
from src.orchestration.workflow_expander import WorkflowExpander, WorkflowExpansionError
from src.orchestration.workflow_plan_validator import WorkflowPlanValidationError, WorkflowPlanValidator
from src.integrations.token_counter import get_num_of_tokens_from_messages


_OBSERVATION_ALLOWLIST = {
    "satisfaction",
    "row_count",
    "preview_row_count",
    "source_row_count",
    "source_preview_row_count",
    "candidate_row_count",
    "removed_row_count",
    "filter_source",
    "filter_reason",
    "fallback_used",
    "fallback_reason",
    "route_id",
    "schema_profile_id",
    "truncated",
    "summary",
    "summary_source",
    "response_text",
    "highlights",
    "caveats",
    "error",
    "error_code",
}
_SENSITIVE_OUTPUT_KEYS = {
    "sql",
    "guard_pass_token",
    "schema_context",
    "schema_ddl",
    "selected_tables",
    "selected_columns",
    "prompt",
    "raw_output",
    "candidate_rows",
    "source_rows",
}
_MAX_OBSERVATION_TOKENS = 2000
_MAX_SAMPLE_ROWS = 2
_MAX_SAMPLE_COLUMNS = 8
_MAX_STRING_LENGTH = 240
_RUNTIME_REPLAN_TEMPLATE_ID = "runtime_replan"


class MainAgentRuntimeReplanner:
    """Main-agent LLM advisor for Observe -> Replan decisions.

    The orchestration service still owns budgets, graph mutation and lifecycle.
    This advisor only asks the shared main-agent LLM runtime to produce a revised
    public DAG when completed node outputs expose a machine-readable signal that
    the current result does not satisfy the user request.
    """

    def __init__(
        self,
        *,
        capability_registry: CapabilityRegistry,
        macro_providers: Mapping[str, Any],
        macro_provider_resolver: Callable[[str], Any | None] | None = None,
        text_generator: TextGenerator | None = None,
        payload_policies: Mapping[str, CapabilityPayloadPolicy] | None = None,
    ) -> None:
        self._capability_registry = capability_registry
        self._macro_providers = dict(macro_providers)
        self._text_generator = text_generator
        self._expander = WorkflowExpander(
            self._macro_providers,
            macro_provider_resolver=macro_provider_resolver,
        )
        self._public_validator = WorkflowPlanValidator(capability_registry, public_only=True)
        self._internal_validator = WorkflowPlanValidator(capability_registry, public_only=False)
        self._payload_policy_overrides = dict(payload_policies or {})

    async def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if self._text_generator is None:
            return None
        if context.unresolved_interrupt:
            return None
        if context.replan_count >= context.plan.max_replans:
            return None
        if not self._should_observe_for_replan(context):
            return None

        prompt_resolution = self._build_prompt_resolution(context)
        raw_output = self._call_text_generator(
            prompt_resolution.prompt,
            request=context.request,
            stage="orchestration_replan",
            prompt_profile=prompt_resolution.llm_call_payload,
        )
        if inspect.isawaitable(raw_output):
            raw_output = await raw_output
        try:
            decision_payload = self._parse_decision(str(raw_output or ""))
            if decision_payload.get("action") != "replan":
                return None
            reason = str(decision_payload.get("reason") or "main_agent_runtime_replan")
            public_plan = parse_planner_output(
                json.dumps({"nodes": decision_payload.get("nodes")}, ensure_ascii=False),
                task_id=context.plan.task_id,
            )
            public_plan = self._enrich_public_plan(public_plan, request=context.request)
            self._public_validator.validate(public_plan)
            expanded = self._expander.expand(public_plan, request=context.request)
            self._internal_validator.validate(expanded)
        except (PlannerOutputError, WorkflowPlanValidationError, WorkflowExpansionError, ValueError, TypeError):
            return None

        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=expanded.task_id,
                nodes=expanded.nodes,
                metadata={
                    **dict(expanded.metadata),
                    "runtime_replan_source": "main_agent_llm_runtime",
                    "runtime_replan_reason": reason,
                },
                max_replans=max(context.plan.max_replans, expanded.max_replans),
                max_dynamic_nodes=max(context.plan.max_dynamic_nodes, expanded.max_dynamic_nodes),
            ),
            reason=reason,
            metadata={"replan_source": "main_agent_llm_runtime"},
        )

    def _call_text_generator(
        self,
        prompt: str,
        *,
        request: OrchestrationRequest,
        stage: str,
        prompt_profile: Mapping[str, Any] | None = None,
    ):
        assert self._text_generator is not None
        try:
            signature = inspect.signature(self._text_generator)
        except (TypeError, ValueError):
            return self._text_generator(prompt)
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "request" in signature.parameters:
            kwargs["request"] = request
        if accepts_kwargs or "stage" in signature.parameters:
            kwargs["stage"] = stage
        if prompt_profile is not None and (accepts_kwargs or "prompt_profile" in signature.parameters):
            kwargs["prompt_profile"] = prompt_profile
        return self._text_generator(prompt, **kwargs) if kwargs else self._text_generator(prompt)

    @staticmethod
    def _parse_decision(raw_output: str) -> dict[str, Any]:
        stripped = raw_output.strip()
        if not stripped:
            raise ValueError("empty runtime replan output")
        if not stripped.startswith("{"):
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                stripped = stripped[start : end + 1]
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("runtime replan output must be an object")
        action = payload.get("action")
        if action not in {"none", "replan"}:
            raise ValueError("runtime replan action must be none or replan")
        if action == "replan" and not isinstance(payload.get("nodes"), list):
            raise ValueError("runtime replan must include nodes")
        return payload

    def _enrich_public_plan(self, plan: WorkflowPlan, *, request: OrchestrationRequest) -> WorkflowPlan:
        payload_policy = PlannerPayloadPolicy(self._resolve_payload_policies())
        nodes = tuple(payload_policy.apply(node, request=request) for node in plan.nodes)
        nodes, finalizer_added, finalizer_rewired = self._ensure_final_main_agent(
            nodes,
            request=request,
            payload_policy=payload_policy,
        )
        return WorkflowPlan(
            task_id=plan.task_id,
            nodes=nodes,
            metadata={
                **dict(plan.metadata),
                "source": "main_agent_runtime_replan_output",
                "planner_finalizer_added": finalizer_added,
                "planner_finalizer_rewired": finalizer_rewired,
            },
            max_replans=plan.max_replans,
            max_dynamic_nodes=plan.max_dynamic_nodes,
        )

    def _resolve_payload_policies(self) -> dict[str, CapabilityPayloadPolicy]:
        payload_policies = self._capability_registry.planner_payload_policies()
        payload_policies.update(self._payload_policy_overrides)
        return payload_policies

    @classmethod
    def _ensure_final_main_agent(
        cls,
        nodes: tuple[WorkflowNodePlan, ...],
        *,
        request: OrchestrationRequest,
        payload_policy: PlannerPayloadPolicy,
    ) -> tuple[tuple[WorkflowNodePlan, ...], bool, bool]:
        if not nodes:
            return nodes, False, False
        node_ids = {node.node_id for node in nodes}
        downstream_dependencies = {dependency for node in nodes for dependency in node.depends_on}
        tail_nodes = tuple(node for node in nodes if node.node_id not in downstream_dependencies)
        tail_main_nodes = tuple(node for node in tail_nodes if node.capability_id == "main_agent.respond")
        non_answering_tail_ids = tuple(node.node_id for node in tail_nodes if not cls._is_answer_producing(node.capability_id))
        if tail_main_nodes:
            if not non_answering_tail_ids:
                return nodes, False, False
            target = tail_main_nodes[-1]
            missing_dependencies = tuple(node_id for node_id in non_answering_tail_ids if node_id not in target.depends_on)
            if not missing_dependencies:
                return nodes, False, False
            rewired = WorkflowNodePlan(
                node_id=target.node_id,
                capability_id=target.capability_id,
                depends_on=target.depends_on + missing_dependencies,
                input_payload=target.input_payload,
                metadata=target.metadata,
                criticality=target.criticality,
                retry_policy=target.retry_policy,
                timeout_policy=target.timeout_policy,
                resource_class=target.resource_class,
            )
            return tuple(rewired if node.node_id == target.node_id else node for node in nodes), False, True
        if not non_answering_tail_ids:
            return nodes, False, False
        final_node = payload_policy.apply(
            WorkflowNodePlan(
                node_id=cls._unique_node_id("answer_user", node_ids),
                capability_id="main_agent.respond",
                depends_on=non_answering_tail_ids,
            ),
            request=request,
        )
        return (*nodes, final_node), True, False

    @staticmethod
    def _is_answer_producing(capability_id: str) -> bool:
        return capability_id == "main_agent.respond" or capability_id.startswith("skill.")

    @staticmethod
    def _unique_node_id(preferred: str, existing: set[str]) -> str:
        if preferred not in existing:
            return preferred
        index = 2
        while f"{preferred}_{index}" in existing:
            index += 1
        return f"{preferred}_{index}"

    @classmethod
    def _should_observe_for_replan(cls, context: RuntimeReplanContext) -> bool:
        if context.completion_status == CompletionStatus.REPLAN_AVAILABLE:
            return True
        if context.completion_status not in {CompletionStatus.RUNNING, CompletionStatus.COMPLETED}:
            return False
        return any(cls._output_requests_replan(output) for output in context.node_outputs.values())

    @staticmethod
    def _output_requests_replan(output: Mapping[str, Any]) -> bool:
        decision = output.get("soft_skill_decision")
        if isinstance(decision, Mapping):
            return False
        satisfaction = output.get("satisfaction")
        if isinstance(satisfaction, Mapping):
            if satisfaction.get("reason_code") == "soft_skill_execute":
                return False
            if satisfaction.get("replan_recommended") is True:
                return True
            return satisfaction.get("satisfied") is False
        return False

    def _build_prompt(self, context: RuntimeReplanContext) -> str:
        capabilities = self._format_public_capabilities(self._capability_registry.list(public_only=True))
        node_outputs = self._sanitize_node_outputs(context.node_outputs)
        current_nodes = []
        for node in context.nodes.values():
            if node.status == NodeStatus.ORPHANED:
                continue
            try:
                depends_on = list(context.plan.node_by_id(node.node_id).depends_on)
            except KeyError:
                depends_on = []
            current_nodes.append(
                {
                    "node_id": node.node_id,
                    "capability_id": node.capability_id,
                    "depends_on": depends_on,
                    "status": str(node.status),
                }
            )
        schema = {
            "action": "none | replan",
            "reason": "short reason when replanning",
            "nodes": [
                {
                    "node_id": "public node id",
                    "capability_id": "public capability id only",
                    "depends_on": ["other public node ids"],
                    "input_payload": {},
                }
            ],
        }
        return (
            "你是育种助手（SeedPilot）的运行时重编排决策器。\n"
            "你必须在 capability public contract 内工作，只能输出 public capability 高层 DAG；禁止输出任何 Skill 内部阶段、handler 或实现细节。\n"
            "如果当前结果已经足以回答用户，返回 {\"action\": \"none\"}。\n"
            "如果系统内可补足，返回 {\"action\": \"replan\", \"reason\": ..., \"nodes\": [...]}，nodes 是完整修订后的 public DAG。\n"
            "不要无限重排；必须尊重预算。只返回 JSON，不要输出 Markdown 或解释。\n\n"
            f"用户问题：{context.request.user_message}\n\n"
            f"重排预算：replan_count={context.replan_count}, max_replans={context.plan.max_replans}, "
            f"dynamic_node_count={context.dynamic_node_count}, max_dynamic_nodes={context.plan.max_dynamic_nodes}\n\n"
            f"可用 public capability：\n{capabilities}\n\n"
            f"当前节点：\n{json.dumps(current_nodes, ensure_ascii=False, indent=2, default=str)}\n\n"
            f"已完成节点输出 / 满足度：\n{json.dumps(node_outputs, ensure_ascii=False, indent=2, default=str)}\n\n"
            f"输出结构：\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

    def _build_prompt_resolution(self, context: RuntimeReplanContext):
        legacy_prompt = self._build_prompt(context)
        capabilities = self._format_public_capabilities(self._capability_registry.list(public_only=True))
        node_outputs = self._sanitize_node_outputs(context.node_outputs)
        current_nodes = []
        for node in context.nodes.values():
            if node.status == NodeStatus.ORPHANED:
                continue
            try:
                depends_on = list(context.plan.node_by_id(node.node_id).depends_on)
            except KeyError:
                depends_on = []
            current_nodes.append(
                {
                    "node_id": node.node_id,
                    "capability_id": node.capability_id,
                    "depends_on": depends_on,
                    "status": str(node.status),
                }
            )
        schema = {
            "action": "none | replan",
            "reason": "short reason when replanning",
            "nodes": [
                {
                    "node_id": "public node id",
                    "capability_id": "public capability id only",
                    "depends_on": ["other public node ids"],
                    "input_payload": {},
                }
            ],
        }
        metadata = context.request.metadata if isinstance(context.request.metadata, Mapping) else {}
        return resolve_profile_prompt_for_mode(
            legacy_prompt=legacy_prompt,
            template_id=_RUNTIME_REPLAN_TEMPLATE_ID,
            template_version=PROMPT_PROFILE_TEMPLATE_VERSION,
            trim_max_tokens=coerce_profile_trim_max_tokens(
                metadata.get("runtime_replan_trim_max_tokens"),
                metadata.get("trim_max_tokens"),
            ),
            segments=(
                PromptSegment(
                    name="stable_runtime_replan_rules",
                    role="system",
                    content=(
                        "你是育种助手（SeedPilot）的运行时重编排决策器。"
                        "必须在 capability public contract 内工作，只能输出 public capability 高层 DAG；"
                        "禁止输出任何 Skill 内部阶段、handler、runtime 或实现细节。"
                        "如果当前结果已经足以回答用户，返回 {\"action\":\"none\"}；"
                        "如果系统内可补足，返回完整修订后的 public DAG。只返回 JSON。"
                    ),
                    priority=0,
                    mutability="stable",
                    cache_affinity="prefix",
                    trim_policy="required",
                    security_role="instruction",
                ),
                PromptSegment(
                    name="runtime_replan_public_capabilities",
                    role="context",
                    content="# 可用 public capability\n" + capabilities,
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="required",
                    security_role="tool_profile",
                ),
                PromptSegment(
                    name="runtime_replan_budget_state",
                    role="context",
                    content=(
                        "# 重排预算\n"
                        f"replan_count={context.replan_count}, max_replans={context.plan.max_replans}, "
                        f"dynamic_node_count={context.dynamic_node_count}, max_dynamic_nodes={context.plan.max_dynamic_nodes}"
                    ),
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="required",
                    security_role="active_note",
                ),
                PromptSegment(
                    name="current_user_request",
                    role="user",
                    content="# 用户问题\n" + context.request.user_message,
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="required",
                    security_role="user_input",
                ),
                PromptSegment(
                    name="runtime_replan_current_nodes",
                    role="context",
                    content="# 当前 public 节点\n" + json.dumps(current_nodes, ensure_ascii=False, indent=2, default=str),
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="compressible",
                    security_role="tool_result",
                ),
                PromptSegment(
                    name="runtime_replan_sanitized_outputs",
                    role="context",
                    content="# 已完成节点输出 / 满足度（已脱敏）\n"
                    + json.dumps(node_outputs, ensure_ascii=False, indent=2, default=str),
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="compressible",
                    security_role="tool_result",
                ),
                PromptSegment(
                    name="runtime_replan_output_guard",
                    role="system",
                    content="输出结构如下，必须严格 JSON，不要 Markdown 或解释：\n"
                    + json.dumps(schema, ensure_ascii=False, indent=2),
                    priority=0,
                    mutability="stable",
                    cache_affinity="no_cache",
                    trim_policy="required",
                    security_role="guard",
                ),
            ),
            audit_context={"stage": "orchestration_replan"},
        )

    @classmethod
    def _sanitize_node_outputs(cls, node_outputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        used_tokens = 0
        for node_id, output in node_outputs.items():
            safe_output = cls._sanitize_single_output(output)
            candidate = json.dumps(safe_output, ensure_ascii=False, sort_keys=True, default=str)
            candidate_tokens = get_num_of_tokens_from_messages([candidate])
            if used_tokens + candidate_tokens > _MAX_OBSERVATION_TOKENS:
                sanitized[str(node_id)] = {
                    "omitted_due_to_budget": True,
                    "available_keys": sorted(str(key) for key in output.keys()),
                }
                break
            sanitized[str(node_id)] = safe_output
            used_tokens += candidate_tokens
        return sanitized

    @classmethod
    def _sanitize_single_output(cls, output: Mapping[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in output.items():
            key_text = str(key)
            if key_text in _SENSITIVE_OUTPUT_KEYS:
                continue
            if key_text in _OBSERVATION_ALLOWLIST:
                safe[key_text] = cls._sanitize_value(value)
        rows = output.get("rows")
        if isinstance(rows, list | tuple) and rows:
            safe["row_sample"] = [cls._sanitize_row(row) for row in rows[:_MAX_SAMPLE_ROWS] if isinstance(row, Mapping)]
            safe["row_sample_count"] = min(len(rows), _MAX_SAMPLE_ROWS)
        return safe

    @classmethod
    def _sanitize_row(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        safe_row: dict[str, Any] = {}
        for index, (key, value) in enumerate(row.items()):
            if index >= _MAX_SAMPLE_COLUMNS:
                safe_row["_truncated_columns"] = True
                break
            safe_row[str(key)] = cls._sanitize_value(value)
        return safe_row

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._sanitize_value(item) for key, item in value.items() if str(key) not in _SENSITIVE_OUTPUT_KEYS}
        if isinstance(value, list | tuple):
            return [cls._sanitize_value(item) for item in value[:_MAX_SAMPLE_ROWS]]
        if isinstance(value, str):
            return value if len(value) <= _MAX_STRING_LENGTH else value[:_MAX_STRING_LENGTH] + "…"
        if isinstance(value, int | float | bool) or value is None:
            return value
        text = str(value)
        return text if len(text) <= _MAX_STRING_LENGTH else text[:_MAX_STRING_LENGTH] + "…"

    @staticmethod
    def _format_public_capabilities(capabilities: tuple[CapabilityDescriptor, ...] | list[CapabilityDescriptor]) -> str:
        return "\n".join(f"- {descriptor.capability_id}：{descriptor.name} — {descriptor.description}" for descriptor in capabilities)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

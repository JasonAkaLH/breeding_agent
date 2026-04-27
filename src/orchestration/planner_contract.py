from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Iterable

from .models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan


PLANNER_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["node_id", "capability_id"],
                "properties": {
                    "node_id": {"type": "string", "minLength": 1},
                    "capability_id": {"type": "string", "minLength": 1},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "input_payload": {"type": "object"},
                },
            },
        }
    },
}


class PlannerOutputError(ValueError):
    """Raised when LLM planner output does not match the high-level DAG contract."""


TextGenerator = Callable[[str], str | Awaitable[str]]


def build_planner_prompt(
    request: OrchestrationRequest,
    *,
    public_capabilities: Iterable[CapabilityDescriptor] | None = None,
    planner_payload_allowlist: Mapping[str, Iterable[str]] | None = None,
) -> str:
    schema = json.dumps(PLANNER_OUTPUT_JSON_SCHEMA, ensure_ascii=False, indent=2)
    capability_block = _format_public_capabilities(
        public_capabilities,
        planner_payload_allowlist=planner_payload_allowlist,
    )
    return (
        "You are a bounded high-level workflow planner. "
        "Return JSON only. Choose the smallest useful acyclic DAG. "
        "Use only public capabilities listed below. "
        "Never emit SQLQuery internal capabilities or low-level implementation nodes. "
        "For database/data questions, prefer sql_query.query followed by main_agent.respond depending on it, "
        "so the final answer is conversational. For ordinary questions, use main_agent.respond only.\n\n"
        f"Public capabilities:\n{capability_block}\n\n"
        f"User message: {request.user_message}\n\n"
        f"Output JSON schema:\n{schema}"
    )


async def build_plan_from_llm_output(
    request: OrchestrationRequest,
    *,
    text_generator: TextGenerator,
    public_capabilities: Iterable[CapabilityDescriptor] | None = None,
    planner_payload_allowlist: Mapping[str, Iterable[str]] | None = None,
) -> WorkflowPlan:
    raw_output = text_generator(
        build_planner_prompt(
            request,
            public_capabilities=public_capabilities,
            planner_payload_allowlist=planner_payload_allowlist,
        )
    )
    if inspect.isawaitable(raw_output):
        raw_output = await raw_output
    if not isinstance(raw_output, str):
        raise PlannerOutputError("Planner text generator must return a string.")
    return parse_planner_output(raw_output, task_id=request.task_id)


def _format_public_capabilities(
    public_capabilities: Iterable[CapabilityDescriptor] | None,
    *,
    planner_payload_allowlist: Mapping[str, Iterable[str]] | None = None,
) -> str:
    capabilities = tuple(public_capabilities or ())
    allowlist = {
        capability_id: tuple(fields)
        for capability_id, fields in dict(planner_payload_allowlist or {}).items()
    }
    if not capabilities:
        return (
            "- main_agent.respond: Default LLM-backed main-agent response. "
            "Planner input_payload allowed fields: none; system fills trusted fields.\n"
            "- sql_query.query: Safely answer a natural-language data question through SQLQuery. "
            "Planner input_payload allowed fields: none; system fills trusted fields."
        )
    return "\n".join(
        f"- {descriptor.capability_id}: {descriptor.name} — {descriptor.description} "
        f"Planner input_payload allowed fields: {_format_payload_fields(allowlist.get(descriptor.capability_id, ()))}."
        for descriptor in capabilities
    )


def _format_payload_fields(fields: Iterable[str]) -> str:
    field_tuple = tuple(fields)
    if not field_tuple:
        return "none; system fills trusted fields"
    return ", ".join(field_tuple)


def _reject_unknown_keys(payload: dict[str, Any], allowed_keys: set[str], context: str) -> None:
    unknown_keys = set(payload) - allowed_keys
    if unknown_keys:
        joined = ", ".join(sorted(unknown_keys))
        raise PlannerOutputError(f"Unknown keys in {context}: {joined}")


def parse_planner_output(raw_output: str, *, task_id: str) -> WorkflowPlan:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError("Planner output must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise PlannerOutputError("Planner output must be a JSON object.")
    nodes_payload = payload.get("nodes")
    if not isinstance(nodes_payload, list) or not nodes_payload:
        raise PlannerOutputError("Planner output must include a non-empty nodes array.")
    _reject_unknown_keys(payload, {"nodes"}, "planner output")

    nodes: list[WorkflowNodePlan] = []
    for index, node_payload in enumerate(nodes_payload):
        if not isinstance(node_payload, dict):
            raise PlannerOutputError(f"Planner node at index {index} must be a node object.")
        _reject_unknown_keys(
            node_payload,
            {"node_id", "capability_id", "depends_on", "input_payload"},
            f"planner node at index {index}",
        )
        node_id = node_payload.get("node_id")
        capability_id = node_payload.get("capability_id")
        if not isinstance(node_id, str) or not node_id:
            raise PlannerOutputError(f"Planner node at index {index} must include node_id.")
        if not isinstance(capability_id, str) or not capability_id:
            raise PlannerOutputError(f"Planner node {node_id} must include capability_id.")

        depends_on_payload = node_payload.get("depends_on", [])
        if not isinstance(depends_on_payload, list) or not all(
            isinstance(item, str) for item in depends_on_payload
        ):
            raise PlannerOutputError(f"Planner node {node_id} depends_on must be a string array.")

        input_payload = node_payload.get("input_payload", {})
        if not isinstance(input_payload, dict):
            raise PlannerOutputError(f"Planner node {node_id} input_payload must be an object.")

        nodes.append(
            WorkflowNodePlan(
                node_id=node_id,
                capability_id=capability_id,
                depends_on=tuple(depends_on_payload),
                input_payload=dict(input_payload),
            )
        )

    return WorkflowPlan(
        task_id=task_id,
        nodes=tuple(nodes),
        metadata={"source": "llm_planner_output"},
        max_replans=0,
        max_dynamic_nodes=0,
    )

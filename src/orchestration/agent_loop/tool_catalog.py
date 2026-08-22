from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from src.orchestration.models import UserMCPServerProfile
from src.orchestration.registry import CapabilityRegistry

from .models import AgentToolDescriptor


@dataclass(frozen=True, slots=True)
class CapabilityVisibilityContext:
    authenticated_owner_scope: str
    execution_path: str = "default"
    pinned_skill_bundle_revision: str | None = None
    safe_mcp_server_profiles: tuple[UserMCPServerProfile, ...] = ()
    public_capability_allowlist: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.authenticated_owner_scope.strip():
            raise ValueError("authenticated_owner_scope must not be empty")
        if self.execution_path not in {"default", "user_scoped", "legacy", "unavailable"}:
            raise ValueError("execution_path is invalid")
        profiles = tuple(self.safe_mcp_server_profiles)
        if len({profile.server_id for profile in profiles}) != len(profiles):
            raise ValueError("safe_mcp_server_profiles must have unique server ids")
        object.__setattr__(self, "safe_mcp_server_profiles", profiles)


SystemPayloadFactory = Callable[[CapabilityVisibilityContext], Mapping[str, Any]]


def _empty_system_payload(_context: CapabilityVisibilityContext) -> Mapping[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class CapabilityInvocationPolicy:
    model_allowed_fields: tuple[str, ...]
    input_schema: Mapping[str, Any]
    system_payload_factory: SystemPayloadFactory = _empty_system_payload
    parallel_safe: bool = False
    can_suspend: bool = False

    def __post_init__(self) -> None:
        allowed = tuple(dict.fromkeys(str(field).strip() for field in self.model_allowed_fields))
        if any(not field for field in allowed):
            raise ValueError("model_allowed_fields must not contain empty names")
        schema = json.loads(
            json.dumps(
                dict(self.input_schema),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError("capability input_schema is invalid") from exc
        object.__setattr__(self, "model_allowed_fields", allowed)
        object.__setattr__(self, "input_schema", MappingProxyType(schema))

    def effective_payload(
        self,
        model_payload: Mapping[str, Any],
        *,
        context: CapabilityVisibilityContext,
    ) -> dict[str, Any]:
        allowed = set(self.model_allowed_fields)
        effective = {
            key: value for key, value in dict(model_payload).items() if key in allowed
        }
        effective.update(dict(self.system_payload_factory(context)))
        try:
            Draft202012Validator(dict(self.input_schema)).validate(effective)
        except ValidationError as exc:
            raise ValueError("agent_tool_payload_schema_invalid") from exc
        return effective


@dataclass(frozen=True, slots=True)
class AgentToolCatalog:
    tools: tuple[AgentToolDescriptor, ...]
    policies: Mapping[str, CapabilityInvocationPolicy] = field(repr=False)


class AgentToolCatalogBuilder:
    def __init__(
        self,
        registry: CapabilityRegistry,
        policies: Mapping[str, CapabilityInvocationPolicy] | None = None,
    ) -> None:
        self._registry = registry
        self._policies = None if policies is None else dict(policies)

    def build(self, context: CapabilityVisibilityContext) -> AgentToolCatalog:
        tools: list[AgentToolDescriptor] = []
        policies: dict[str, CapabilityInvocationPolicy] = {}
        available_policies = (
            self._registry.invocation_policies()
            if self._policies is None
            else self._policies
        )
        for descriptor in self._registry.list_for_visibility(context, public_only=True):
            if descriptor.kind != "skill" and descriptor.capability_id != "mcp.dispatch":
                continue
            policy = available_policies.get(descriptor.capability_id)
            if not isinstance(policy, CapabilityInvocationPolicy):
                continue
            schema = dict(policy.input_schema)
            if descriptor.capability_id == "mcp.dispatch":
                schema = _mcp_dispatch_schema(schema, context)
            tool = AgentToolDescriptor.for_capability(
                descriptor.capability_id,
                description=descriptor.description,
                input_schema=schema,
            )
            tools.append(tool)
            policies[descriptor.capability_id] = policy
        tools.sort(key=lambda tool: tool.capability_id.encode("utf-8"))
        return AgentToolCatalog(tuple(tools), MappingProxyType(policies))


class CatalogPreflightDecision(StrEnum):
    FITS = "fits"
    HISTORY_COMPACTION_REQUIRED = "history_compaction_required"
    FATAL_REQUIRED_SEGMENTS_TOO_LARGE = "fatal_required_segments_too_large"


@dataclass(frozen=True, slots=True)
class CatalogPreflightResult:
    decision: CatalogPreflightDecision
    tool_count: int
    schema_bytes: int
    required_tokens: int
    history_tokens: int
    total_tokens: int
    token_budget: int


class AgentCatalogPreflight:
    def __init__(self, token_estimator: Callable[[str], int] | None = None) -> None:
        self._estimate = token_estimator or _estimate_tokens

    def evaluate(
        self,
        *,
        catalog: AgentToolCatalog,
        stable_rules: str,
        safe_tool_rules: str,
        current_user_input: str,
        minimum_suffix: str,
        history_segments: tuple[str, ...],
        eligible_compactable_ranges: int,
        token_budget: int,
    ) -> CatalogPreflightResult:
        if token_budget <= 0 or eligible_compactable_ranges < 0:
            raise ValueError("catalog preflight budget inputs are invalid")
        catalog_json = json.dumps(
            [
                {
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                    "name": tool.provider_safe_name,
                }
                for tool in catalog.tools
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_json = json.dumps(
            [dict(tool.input_schema) for tool in catalog.tools],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        schema_bytes = len(schema_json.encode("utf-8"))
        required_tokens = sum(
            self._estimate(segment)
            for segment in (
                stable_rules,
                safe_tool_rules,
                catalog_json,
                current_user_input,
                minimum_suffix,
            )
        )
        history_tokens = sum(self._estimate(segment) for segment in history_segments)
        total_tokens = required_tokens + history_tokens
        if required_tokens > token_budget:
            decision = CatalogPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE
        elif total_tokens <= token_budget:
            decision = CatalogPreflightDecision.FITS
        elif eligible_compactable_ranges > 0:
            decision = CatalogPreflightDecision.HISTORY_COMPACTION_REQUIRED
        else:
            decision = CatalogPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE
        return CatalogPreflightResult(
            decision=decision,
            tool_count=len(catalog.tools),
            schema_bytes=schema_bytes,
            required_tokens=required_tokens,
            history_tokens=history_tokens,
            total_tokens=total_tokens,
            token_budget=token_budget,
        )


def _mcp_dispatch_schema(
    schema: Mapping[str, Any], context: CapabilityVisibilityContext
) -> dict[str, Any]:
    value = json.loads(json.dumps(dict(schema)))
    properties = dict(value.get("properties") or {})
    server = dict(properties.get("server_id") or {"type": "string"})
    server["enum"] = sorted(profile.server_id for profile in context.safe_mcp_server_profiles)
    properties["server_id"] = server
    value.update(
        {
            "type": "object",
            "properties": properties,
            "required": sorted(set(value.get("required") or ()) | {"server_id"}),
            "additionalProperties": False,
        }
    )
    return value


def _estimate_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)

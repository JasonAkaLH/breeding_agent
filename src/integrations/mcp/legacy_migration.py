from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

from .config import MCPRuntimeConfig, MCPServerConfig


class LegacyDisposition(str, Enum):
    MIGRATE_OWNER = "migrate_owner"
    RETAIN_FOR_ROLLBACK = "retain_for_rollback"
    RETIRE = "retire"


class LegacyConsumerScope(str, Enum):
    SERVICE_ACCOUNT_ONLY = "service_account_only"
    MULTI_USER = "multi_user"
    UNKNOWN = "unknown"


class LegacyCapabilityResolution(str, Enum):
    TARGET_HEALTH = "target_health"
    APPROVED_RETIREMENT = "approved_retirement"
    LEGACY_RETAINED = "legacy_retained"


class LegacyMigrationValidationError(ValueError):
    """A validation failure whose message is safe to expose to operators."""


class SafeReferenceProvider(Protocol):
    def safe_owner_reference(self, owner_user_id: str, *, context: str) -> str: ...


@dataclass(slots=True, frozen=True)
class LegacyServerClassification:
    server_id: str
    disposition: LegacyDisposition
    consumer_scope: LegacyConsumerScope
    owner_user_id: str | None = None
    retirement_approver: str | None = None
    retirement_reason: str | None = None
    impact_accepted: bool = False
    target_consumer_refs: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class LegacySourceInventoryEntry:
    server_id: str
    source_fingerprint: str
    target_consumer_count: int
    target_consumer_set_digest: str


@dataclass(slots=True, frozen=True)
class LegacyMigrationMappingCandidate:
    source_server_id: str
    source_fingerprint: str
    owner_consumer_ref: str
    target_server_id: str


@dataclass(slots=True, frozen=True)
class LegacyConsumerCapabilityObligation:
    consumer_ref: str
    capability_id: str
    source_contract_fingerprint: str
    resolution: LegacyCapabilityResolution


@dataclass(slots=True, frozen=True)
class LegacyConsumerCapabilityImpact:
    server_id: str
    consumer_scope: LegacyConsumerScope
    disposition: LegacyDisposition
    configured_tool_count: int
    exposed_capability_ids: tuple[str, ...]
    target_consumer_count: int
    target_consumer_set_digest: str
    obligations: tuple[LegacyConsumerCapabilityObligation, ...]


@dataclass(slots=True, frozen=True)
class LegacyMigrationPlan:
    inventory: tuple[LegacySourceInventoryEntry, ...]
    mapping_candidates: tuple[LegacyMigrationMappingCandidate, ...]
    consumer_capability_impact: tuple[LegacyConsumerCapabilityImpact, ...]
    retained_server_ids: tuple[str, ...]
    retired_server_ids: tuple[str, ...]
    assembly_off_allowed: bool
    assembly_off_blockers: tuple[str, ...]
    plan_fingerprint: str


@dataclass(slots=True, frozen=True)
class LegacyMigrationHealthPolicy:
    max_attempts: int = 2
    timeout_seconds_per_attempt: int = 60
    retry_delay_seconds: float = 0.25
    cleanup_timeout_seconds: float = 1.0

    @property
    def total_timeout_seconds(self) -> float:
        retry_count = max(0, self.max_attempts - 1)
        return (
            self.max_attempts * self.timeout_seconds_per_attempt
            + retry_count * self.retry_delay_seconds
            + self.max_attempts * self.cleanup_timeout_seconds
        )


LEGACY_MIGRATION_HEALTH_POLICY = LegacyMigrationHealthPolicy()


@dataclass(slots=True, frozen=True)
class LegacyMigrationHealthResult:
    server_id: str
    attempts: int
    handshake_ok: bool
    discovery_ok: bool
    full_paginated_tool_list_ok: bool
    nonempty_legal_tool_ok: bool
    safe_error_code: str | None = None
    target_server_id: str | None = None
    source_fingerprint: str | None = None
    target_consumer_set_digest: str | None = None
    catalog_fingerprint: str | None = None
    capability_fingerprint: str | None = None
    available_capability_ids: tuple[str, ...] = ()
    available_capability_contracts: tuple[tuple[str, str], ...] = ()
    observed_at: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not self.server_id.strip():
            raise LegacyMigrationValidationError(
                "Health result server_id must be a non-empty string"
            )
        if type(self.attempts) is not int:
            raise LegacyMigrationValidationError("Health attempts must be an integer")
        for value in (
            self.handshake_ok,
            self.discovery_ok,
            self.full_paginated_tool_list_ok,
            self.nonempty_legal_tool_ok,
        ):
            if type(value) is not bool:
                raise LegacyMigrationValidationError(
                    "Health result flags must be booleans"
                )
        if not isinstance(self.available_capability_ids, (list, tuple)) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.available_capability_ids
        ):
            raise LegacyMigrationValidationError(
                "Health result available_capability_ids must be strings"
            )
        normalized_capabilities = tuple(
            value.strip() for value in self.available_capability_ids
        )
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise LegacyMigrationValidationError(
                "Health result available_capability_ids must be unique"
            )
        object.__setattr__(
            self, "available_capability_ids", tuple(sorted(normalized_capabilities))
        )
        if not isinstance(self.available_capability_contracts, (list, tuple)):
            raise LegacyMigrationValidationError(
                "Health result available_capability_contracts must be pairs"
            )
        normalized_contracts: list[tuple[str, str]] = []
        for contract_pair in self.available_capability_contracts:
            if (
                not isinstance(contract_pair, (list, tuple))
                or len(contract_pair) != 2
                or not isinstance(contract_pair[0], str)
                or not contract_pair[0].strip()
                or not isinstance(contract_pair[1], str)
                or _SHA256_FINGERPRINT_RE.fullmatch(contract_pair[1]) is None
            ):
                raise LegacyMigrationValidationError(
                    "Health result available_capability_contracts must be "
                    "capability/fingerprint pairs"
                )
            normalized_contracts.append(
                (contract_pair[0].strip(), contract_pair[1])
            )
        if len({item[0] for item in normalized_contracts}) != len(
            normalized_contracts
        ):
            raise LegacyMigrationValidationError(
                "Health result available_capability_contracts must be unique"
            )
        object.__setattr__(
            self,
            "available_capability_contracts",
            tuple(sorted(normalized_contracts)),
        )
        succeeded = all(
            (
                self.handshake_ok,
                self.discovery_ok,
                self.full_paginated_tool_list_ok,
                self.nonempty_legal_tool_ok,
            )
        )
        if succeeded and self.safe_error_code is not None:
            raise LegacyMigrationValidationError(
                "Healthy result must not include safe_error_code"
            )
        if not succeeded and self.safe_error_code is None:
            object.__setattr__(self, "safe_error_code", "health_check_failed")
        if not succeeded and (
            not isinstance(self.safe_error_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.safe_error_code) is None
        ):
            raise LegacyMigrationValidationError(
                "Unhealthy result requires a safe_error_code"
            )

    @property
    def healthy(self) -> bool:
        return (
            1 <= self.attempts <= LEGACY_MIGRATION_HEALTH_POLICY.max_attempts
            and self.handshake_ok
            and self.discovery_ok
            and self.full_paginated_tool_list_ok
            and self.nonempty_legal_tool_ok
        )


_SAFE_CONSUMER_REF_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_SHA256_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(slots=True, frozen=True)
class LegacyMigrationApplyValidation:
    ready: bool
    blockers: tuple[str, ...]
    health_policy: LegacyMigrationHealthPolicy = LEGACY_MIGRATION_HEALTH_POLICY


def plan_legacy_mcp_config_migration(
    config: MCPRuntimeConfig,
    classifications: Iterable[LegacyServerClassification],
) -> LegacyMigrationPlan:
    """Build a deterministic, secret-safe migration dry-run; performs no I/O."""

    if not isinstance(config, MCPRuntimeConfig):
        raise TypeError("config must be MCPRuntimeConfig")
    servers = _unique_servers(config.servers)
    decisions = _classifications_by_server(classifications)
    expected_ids = set(servers)
    actual_ids = set(decisions)
    missing = sorted(expected_ids - actual_ids)
    unexpected = sorted(actual_ids - expected_ids)
    if missing:
        raise LegacyMigrationValidationError(
            f"Missing legacy classifications: {', '.join(missing)}"
        )
    if unexpected:
        raise LegacyMigrationValidationError(
            f"Unknown legacy classifications: {', '.join(unexpected)}"
        )

    inventory: list[LegacySourceInventoryEntry] = []
    mappings: list[LegacyMigrationMappingCandidate] = []
    impacts: list[LegacyConsumerCapabilityImpact] = []
    retained: list[str] = []
    retired: list[str] = []
    blockers: list[str] = []

    for server_id in sorted(servers):
        server = servers[server_id]
        decision = decisions[server_id]
        _validate_source_identifiers(server)
        _validate_decision(decision)
        consumer_refs = _normalized_target_consumer_refs(decision)
        consumer_set_digest = legacy_target_consumer_set_digest(consumer_refs)
        exposed_capability_ids = tuple(
            sorted(
                tool.effective_capability_id(server_id)
                for tool in server.tools
                if tool.expose
            )
        )
        if len(set(exposed_capability_ids)) != len(exposed_capability_ids):
            raise LegacyMigrationValidationError(
                f"Duplicate exposed capability obligation: {server_id}"
            )
        resolution = _capability_resolution(decision.disposition)
        exposed_tools = tuple(
            sorted(
                (
                    tool.effective_capability_id(server_id),
                    legacy_tool_contract_fingerprint(
                        input_schema=tool.input_schema,
                        output_schema=tool.output_schema,
                    ),
                    tool.input_schema is not None,
                )
                for tool in server.tools
                if tool.expose
            )
        )
        obligations = tuple(
            LegacyConsumerCapabilityObligation(
                consumer_ref=consumer_ref,
                capability_id=capability_id,
                source_contract_fingerprint=contract_fingerprint,
                resolution=resolution,
            )
            for consumer_ref in consumer_refs
            for capability_id, contract_fingerprint, _has_input_schema in exposed_tools
        )
        fingerprint = legacy_migration_source_fingerprint(server)
        inventory.append(
            LegacySourceInventoryEntry(
                server_id=server_id,
                source_fingerprint=fingerprint,
                target_consumer_count=len(consumer_refs),
                target_consumer_set_digest=consumer_set_digest,
            )
        )
        impacts.append(
            LegacyConsumerCapabilityImpact(
                server_id=server_id,
                consumer_scope=decision.consumer_scope,
                disposition=decision.disposition,
                configured_tool_count=len(server.tools),
                exposed_capability_ids=exposed_capability_ids,
                target_consumer_count=len(consumer_refs),
                target_consumer_set_digest=consumer_set_digest,
                obligations=obligations,
            )
        )
        if not exposed_capability_ids:
            blockers.append(f"{server_id}:capability_obligations_missing")
        if decision.disposition is LegacyDisposition.MIGRATE_OWNER:
            blockers.extend(
                f"{server_id}:capability_contract_missing:{capability_id}"
                for capability_id, _fingerprint, has_input_schema in exposed_tools
                if not has_input_schema
            )
        if decision.disposition is LegacyDisposition.MIGRATE_OWNER:
            owner = decision.owner_user_id.strip() if decision.owner_user_id else ""
            mappings.append(
                LegacyMigrationMappingCandidate(
                    source_server_id=server_id,
                    source_fingerprint=fingerprint,
                    owner_consumer_ref=consumer_refs[0],
                    target_server_id=deterministic_migrated_server_id(
                        server_id,
                        owner,
                    ),
                )
            )
        elif decision.disposition is LegacyDisposition.RETAIN_FOR_ROLLBACK:
            retained.append(server_id)
            blockers.append(f"{server_id}:retained_for_rollback")
        else:
            retired.append(server_id)

        if decision.disposition is LegacyDisposition.RETIRE:
            if not _approved_retirement(decision):
                blockers.append(f"{server_id}:retirement_approval_incomplete")
        elif decision.consumer_scope is not LegacyConsumerScope.SERVICE_ACCOUNT_ONLY:
            blockers.append(f"{server_id}:shared_or_unknown_consumer_not_retired")

    plan_payload = {
        "inventory": [(item.server_id, item.source_fingerprint) for item in inventory],
        "mappings": [
            (
                item.source_server_id,
                item.source_fingerprint,
                item.owner_consumer_ref,
                item.target_server_id,
            )
            for item in mappings
        ],
        "decisions": [
            (
                item.server_id,
                item.disposition.value,
                item.consumer_scope.value,
                tuple(sorted(item.target_consumer_refs)),
                _approved_retirement(item),
            )
            for item in sorted(decisions.values(), key=lambda value: value.server_id)
        ],
        "capability_obligations": [
            (
                impact.server_id,
                tuple(
                    (
                        obligation.consumer_ref,
                        obligation.capability_id,
                        obligation.source_contract_fingerprint,
                        obligation.resolution.value,
                    )
                    for obligation in impact.obligations
                ),
            )
            for impact in impacts
        ],
    }
    return LegacyMigrationPlan(
        inventory=tuple(inventory),
        mapping_candidates=tuple(mappings),
        consumer_capability_impact=tuple(impacts),
        retained_server_ids=tuple(retained),
        retired_server_ids=tuple(retired),
        assembly_off_allowed=not blockers,
        assembly_off_blockers=tuple(blockers),
        plan_fingerprint=_sha256_json(plan_payload),
    )


def validate_legacy_migration_apply(
    plan: LegacyMigrationPlan,
    health_results: Iterable[LegacyMigrationHealthResult],
    *,
    now: datetime | None = None,
    require_continuity_attestation: bool = True,
) -> LegacyMigrationApplyValidation:
    """Validate pre-apply evidence without connecting to MCP servers or writing state."""

    blockers = list(plan.assembly_off_blockers)
    results: dict[str, LegacyMigrationHealthResult] = {}
    for supplied_result in health_results:
        server_id = supplied_result.server_id.strip()
        if server_id in results:
            raise LegacyMigrationValidationError(
                f"Duplicate health result: {server_id}"
            )
        results[server_id] = supplied_result

    checked_at = _normalize_datetime(now or datetime.now(timezone.utc))
    expected = {candidate.source_server_id for candidate in plan.mapping_candidates}
    for server_id in sorted(expected):
        health_result = results.get(server_id)
        if health_result is None:
            blockers.append(f"{server_id}:health_result_missing")
        elif not require_continuity_attestation:
            if not health_result.healthy:
                blockers.append(f"{server_id}:health_validation_failed")
        else:
            blockers.extend(
                legacy_migration_health_result_blockers(
                    plan,
                    health_result,
                    now=checked_at,
                )
            )
    for server_id in sorted(set(results) - expected):
        blockers.append(f"{server_id}:unexpected_health_result")
    return LegacyMigrationApplyValidation(ready=not blockers, blockers=tuple(blockers))


def legacy_migration_health_result_blockers(
    plan: LegacyMigrationPlan,
    result: LegacyMigrationHealthResult,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Validate one live result against its exact target and continuity obligations."""

    server_id = result.server_id.strip()
    candidate = next(
        (
            item
            for item in plan.mapping_candidates
            if item.source_server_id == server_id
        ),
        None,
    )
    impact = next(
        (
            item
            for item in plan.consumer_capability_impact
            if item.server_id == server_id
        ),
        None,
    )
    if candidate is None or impact is None:
        return (f"{server_id}:unexpected_health_result",)
    if not result.healthy:
        return (f"{server_id}:health_validation_failed",)

    blockers: list[str] = []
    if result.target_server_id != candidate.target_server_id:
        blockers.append(f"{server_id}:health_target_mismatch")
    if result.source_fingerprint != candidate.source_fingerprint:
        blockers.append(f"{server_id}:health_source_fingerprint_mismatch")
    if result.target_consumer_set_digest != impact.target_consumer_set_digest:
        blockers.append(f"{server_id}:health_consumer_set_mismatch")
    expected_contracts = tuple(
        sorted(
            {
                (item.capability_id, item.source_contract_fingerprint)
                for item in impact.obligations
                if item.resolution is LegacyCapabilityResolution.TARGET_HEALTH
            }
        )
    )
    expected_capabilities = tuple(item[0] for item in expected_contracts)
    if result.available_capability_ids != expected_capabilities:
        blockers.append(f"{server_id}:health_capability_obligation_mismatch")
    if result.available_capability_contracts != expected_contracts:
        blockers.append(f"{server_id}:health_capability_contract_mismatch")
    expected_capability_fingerprint = legacy_capability_contract_set_fingerprint(
        expected_contracts
    )
    if result.capability_fingerprint != expected_capability_fingerprint:
        blockers.append(f"{server_id}:health_capability_fingerprint_mismatch")
    if not isinstance(
        result.catalog_fingerprint, str
    ) or not _SHA256_FINGERPRINT_RE.fullmatch(result.catalog_fingerprint):
        blockers.append(f"{server_id}:health_catalog_fingerprint_invalid")

    observed_at = _parse_health_datetime(result.observed_at)
    expires_at = _parse_health_datetime(result.expires_at)
    checked_at = _normalize_datetime(now)
    maximum_window = timedelta(
        seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
    )
    if (
        observed_at is None
        or expires_at is None
        or observed_at >= expires_at
        or expires_at - observed_at > maximum_window
        or observed_at > checked_at
    ):
        blockers.append(f"{server_id}:health_evidence_window_invalid")
    elif checked_at > expires_at:
        blockers.append(f"{server_id}:health_evidence_stale")
    return tuple(blockers)


# Short aliases keep call sites readable while retaining explicit public names above.
build_legacy_migration_plan = plan_legacy_mcp_config_migration
validate_apply_ready = validate_legacy_migration_apply


def _unique_servers(servers: Iterable[MCPServerConfig]) -> dict[str, MCPServerConfig]:
    indexed: dict[str, MCPServerConfig] = {}
    for server in servers:
        if server.server_id in indexed:
            raise LegacyMigrationValidationError(
                f"Duplicate legacy server_id: {server.server_id}"
            )
        indexed[server.server_id] = server
    return indexed


def _classifications_by_server(
    classifications: Iterable[LegacyServerClassification],
) -> dict[str, LegacyServerClassification]:
    indexed: dict[str, LegacyServerClassification] = {}
    for decision in classifications:
        if not isinstance(decision, LegacyServerClassification):
            raise TypeError(
                "classifications must contain LegacyServerClassification values"
            )
        server_id = decision.server_id.strip()
        if not server_id:
            raise LegacyMigrationValidationError(
                "Classification server_id must not be empty"
            )
        if server_id in indexed:
            raise LegacyMigrationValidationError(
                f"Duplicate legacy classification: {server_id}"
            )
        indexed[server_id] = decision
    return indexed


def _validate_source_identifiers(server: MCPServerConfig) -> None:
    validation_error = server.validation_error()
    if validation_error:
        raise LegacyMigrationValidationError(
            f"Legacy MCP source config is invalid: {server.server_id}"
        )
    for tool in server.tools:
        if _SAFE_TOOL_NAME_RE.fullmatch(tool.tool_name) is None:
            raise LegacyMigrationValidationError(
                f"Legacy MCP tool identifier is invalid: {server.server_id}"
            )
        capability_id = tool.effective_capability_id(server.server_id)
        if not tool.valid_capability_id(capability_id):
            raise LegacyMigrationValidationError(
                f"Legacy MCP capability identifier is invalid: {server.server_id}"
            )


def _validate_decision(decision: LegacyServerClassification) -> None:
    if not isinstance(decision.disposition, LegacyDisposition):
        raise LegacyMigrationValidationError(
            f"Invalid legacy disposition: {decision.server_id}"
        )
    if not isinstance(decision.consumer_scope, LegacyConsumerScope):
        raise LegacyMigrationValidationError(
            f"Invalid legacy consumer scope: {decision.server_id}"
        )
    consumer_refs = _normalized_target_consumer_refs(decision)
    owner = decision.owner_user_id.strip() if decision.owner_user_id else ""
    if decision.disposition is LegacyDisposition.MIGRATE_OWNER:
        if decision.consumer_scope is not LegacyConsumerScope.SERVICE_ACCOUNT_ONLY:
            raise LegacyMigrationValidationError(
                f"migrate_owner requires service_account_only consumer scope: {decision.server_id}"
            )
        if not owner:
            raise LegacyMigrationValidationError(
                f"migrate_owner requires an explicit owner: {decision.server_id}"
            )
        if len(consumer_refs) != 1:
            raise LegacyMigrationValidationError(
                "migrate_owner requires exactly one target consumer reference: "
                f"{decision.server_id}"
            )
    elif owner:
        raise LegacyMigrationValidationError(
            f"owner_user_id is only legal for migrate_owner: {decision.server_id}"
        )


def _normalized_target_consumer_refs(
    decision: LegacyServerClassification,
) -> tuple[str, ...]:
    if not isinstance(decision.target_consumer_refs, (list, tuple)):
        raise LegacyMigrationValidationError(
            f"Target consumer references are invalid: {decision.server_id}"
        )
    refs = tuple(str(value).strip() for value in decision.target_consumer_refs)
    if not refs:
        raise LegacyMigrationValidationError(
            f"Target consumer references are required: {decision.server_id}"
        )
    if any(_SAFE_CONSUMER_REF_RE.fullmatch(value) is None for value in refs):
        raise LegacyMigrationValidationError(
            f"Target consumer references must be secret-safe HMAC digests: {decision.server_id}"
        )
    if len(set(refs)) != len(refs):
        raise LegacyMigrationValidationError(
            f"Duplicate target consumer reference: {decision.server_id}"
        )
    return tuple(sorted(refs))


def _capability_resolution(
    disposition: LegacyDisposition,
) -> LegacyCapabilityResolution:
    if disposition is LegacyDisposition.MIGRATE_OWNER:
        return LegacyCapabilityResolution.TARGET_HEALTH
    if disposition is LegacyDisposition.RETIRE:
        return LegacyCapabilityResolution.APPROVED_RETIREMENT
    return LegacyCapabilityResolution.LEGACY_RETAINED


def _approved_retirement(decision: LegacyServerClassification) -> bool:
    return bool(
        decision.retirement_approver
        and decision.retirement_approver.strip()
        and decision.retirement_reason
        and decision.retirement_reason.strip()
        and decision.impact_accepted
    )


def legacy_target_consumer_set_digest(consumer_refs: Iterable[str]) -> str:
    refs = tuple(sorted(str(value).strip() for value in consumer_refs))
    return _sha256_json(
        {
            "schema": "legacy_mcp_target_consumer_set.v1",
            "consumer_refs": refs,
        }
    )


def legacy_target_consumer_reference(
    provider: SafeReferenceProvider,
    consumer_id: str,
) -> str:
    consumer = str(consumer_id or "").strip()
    if not consumer:
        raise ValueError("legacy target consumer identity is invalid")
    digest = provider.safe_owner_reference(
        consumer,
        context="legacy-mcp-target-consumer-v1",
    )
    return f"hmac-sha256:{digest}"


def legacy_capability_set_fingerprint(capability_ids: Iterable[str]) -> str:
    capabilities = tuple(sorted(str(value).strip() for value in capability_ids))
    return _sha256_json(
        {
            "schema": "legacy_mcp_capability_set.v1",
            "capability_ids": capabilities,
        }
    )


def legacy_capability_contract_set_fingerprint(
    capability_contracts: Iterable[tuple[str, str]],
) -> str:
    contracts = tuple(
        sorted(
            (str(capability_id).strip(), str(contract_fingerprint).strip())
            for capability_id, contract_fingerprint in capability_contracts
        )
    )
    return _sha256_json(
        {
            "schema": "legacy_mcp_capability_contract_set.v1",
            "capability_contracts": contracts,
        }
    )


def legacy_tool_contract_fingerprint(
    *,
    input_schema: Mapping[str, object] | None,
    output_schema: Mapping[str, object] | None,
) -> str:
    """Hash only the effective input/output schema contract for one tool."""

    return _sha256_json(
        {
            "schema": "legacy_mcp_tool_contract.v1",
            "input_schema": (
                dict(input_schema) if isinstance(input_schema, Mapping) else None
            ),
            "output_schema": (
                dict(output_schema) if isinstance(output_schema, Mapping) else None
            ),
        }
    )


def legacy_migration_catalog_fingerprint(
    catalog: Iterable[Mapping[str, object]],
) -> str:
    """Hash the discovered catalog without retaining tool names or schemas."""

    safe_catalog: list[dict[str, object]] = []
    for item in catalog:
        name = str(item.get("name") or "").strip()
        schema = item.get("inputSchema")
        output_schema = item.get("outputSchema")
        safe_catalog.append(
            {
                "name": name,
                "input_schema": dict(schema) if isinstance(schema, Mapping) else None,
                "output_schema": (
                    dict(output_schema)
                    if isinstance(output_schema, Mapping)
                    else None
                ),
            }
        )
    safe_catalog.sort(key=_safe_catalog_name)
    return _sha256_json(
        {
            "schema": "legacy_mcp_discovered_catalog.v1",
            "tools": safe_catalog,
        }
    )


def _safe_catalog_name(value: Mapping[str, object]) -> str:
    return str(value.get("name") or "")


def _parse_health_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _normalize_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def legacy_migration_source_fingerprint(server: MCPServerConfig) -> str:
    """Fingerprint the source contract without hashing credential values.

    CP-4 binds the resolved credential separately with a keyed digest during
    apply. This public fingerprint therefore includes only normalized header
    names and auth configuration identifiers, never header values or resolved
    environment values.
    """

    if not isinstance(server, MCPServerConfig):
        raise TypeError("server must be MCPServerConfig")
    return _sha256_json(
        {
            "schema": "legacy_mcp_source_identity.v2",
            "server_id": server.server_id,
            "enabled": server.enabled,
            "required": server.required,
            "transport": server.transport,
            "endpoint": server.endpoint,
            "endpoint_env": server.endpoint_env,
            "protocol_version": server.protocol_version,
            "protocol_version_pinned": server.protocol_version_pinned,
            "allow_http_localhost": server.allow_http_localhost,
            "request_header_names": sorted(
                {str(name).strip().lower() for name in server.request_headers}
            ),
            "client_capabilities": dict(server.client_capabilities),
            "auth": {
                "type": server.auth.type,
                "token_env": server.auth.token_env,
                "api_key_env": server.auth.api_key_env,
                "header_name": server.auth.header_name.strip().lower(),
            },
            "trust_level": server.trust_level,
            "discovery": {
                "refresh_on_startup": server.discovery.refresh_on_startup,
                "refresh_on_conversation_start": (
                    server.discovery.refresh_on_conversation_start
                ),
            },
            "limits": {
                "max_calls_per_task": server.limits.max_calls_per_task,
                "max_output_bytes": server.limits.max_output_bytes,
                "timeout_seconds": server.limits.timeout_seconds,
            },
            "tools": [
                {
                    "tool_name": tool.tool_name,
                    "expose": tool.expose,
                    "mode": tool.mode,
                    "capability_id": tool.capability_id,
                    "public_name": tool.public_name,
                    "public_description": tool.public_description,
                    "risk_level": tool.risk_level,
                    "planner_allowed_fields": list(tool.planner_allowed_fields),
                    "input_schema": (
                        dict(tool.input_schema)
                        if tool.input_schema is not None
                        else None
                    ),
                    "output_schema": (
                        dict(tool.output_schema)
                        if tool.output_schema is not None
                        else None
                    ),
                    "max_output_bytes": tool.max_output_bytes,
                    "task_augmented_mode": tool.task_augmented_mode,
                    "task_ttl_ms": tool.task_ttl_ms,
                    "task_max_polls": tool.task_max_polls,
                }
                for tool in server.tools
            ],
        }
    )


def deterministic_migrated_server_id(
    source_server_id: str,
    owner_user_id: str,
) -> str:
    suffix = hashlib.sha256(
        f"legacy-migration-v1\0{owner_user_id}\0{source_server_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"migrated-{suffix}"


def legacy_migration_record_id(
    *,
    plan_fingerprint: str,
    source_server_id: str,
    target_server_id: str,
) -> str:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (plan_fingerprint, source_server_id, target_server_id)
    ):
        raise ValueError("legacy migration record identity is invalid")
    return _sha256_json(
        {
            "schema": "legacy_mcp_migration_record_identity.v1",
            "plan_fingerprint": plan_fingerprint,
            "source_server_id": source_server_id,
            "target_server_id": target_server_id,
        }
    )


def legacy_migration_credential_provenance_digest(
    credential_cipher: SafeReferenceProvider,
    *,
    credential_values: Mapping[str, object] | None,
    owner_user_id: str,
    target_server_id: str,
    source_fingerprint: str,
    provenance: Mapping[str, object],
) -> str:
    """Bind CP-4 credentials and validation provenance with a keyed digest."""

    owner = str(owner_user_id or "").strip()
    target = str(target_server_id or "").strip()
    source = str(source_fingerprint or "").strip()
    if not owner or not target or not source:
        raise ValueError("legacy migration credential provenance identity is invalid")
    signed_provenance = {
        key: provenance.get(key)
        for key in (
            "schema",
            "source_server_id",
            "source_fingerprint",
            "owner_user_id",
            "target_server_id",
            "credential_security_version",
            "credential_storage_digest",
            "validator_provenance",
            "observed_at",
            "expires_at",
        )
    }
    material = _canonical_json(
        {
            "schema": "legacy_mcp_credential_provenance_digest.v2",
            "credential_values": (
                dict(credential_values) if credential_values is not None else None
            ),
            "provenance": signed_provenance,
        }
    )
    digest = credential_cipher.safe_owner_reference(
        material,
        context=(
            f"legacy-migration-credential-provenance-v2:{owner}:{target}:{source}"
        ),
    )
    return f"hmac-sha256:{digest}"


def _sha256_json(value: object) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )

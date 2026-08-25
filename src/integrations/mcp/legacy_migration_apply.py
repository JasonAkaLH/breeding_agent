from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError

from src.core.contracts import UserMCPConfigurationStoragePort
from src.core.enums import (
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import (
    MCPLegacyMigrationRecord,
    UserMCPCredentialRecord,
    UserMCPServer,
)

from .config import MCPRuntimeConfig, MCPServerConfig
from .credentials import (
    CredentialSecurityError,
    EncryptedCredential,
    MCPAuditReferenceSigner,
    MCPCredentialCipher,
)
from .endpoint_policy import EndpointPolicy
from .headers import validate_auth_header_name, validate_static_headers
from .health import run_health_discovery
from .legacy_migration import (
    LEGACY_MIGRATION_HEALTH_POLICY,
    LegacyConsumerCapabilityObligation,
    LegacyMigrationHealthPolicy,
    LegacyMigrationHealthResult,
    LegacyMigrationMappingCandidate,
    LegacyMigrationPlan,
    deterministic_migrated_server_id,
    legacy_capability_contract_set_fingerprint,
    legacy_migration_catalog_fingerprint,
    legacy_migration_credential_provenance_digest,
    legacy_migration_health_result_blockers,
    legacy_migration_record_id,
    legacy_migration_source_fingerprint,
    legacy_target_consumer_reference,
    legacy_tool_contract_fingerprint,
)


class LegacyMigrationApplyError(RuntimeError):
    """An apply failure whose code is safe to expose to operators."""


@dataclass(frozen=True, slots=True)
class _DesiredServer:
    server: UserMCPServer
    credential_values: Mapping[str, Any] | None
    encrypted: EncryptedCredential | None
    source_fingerprint: str
    credential_digest: str
    continuity_result: LegacyMigrationHealthResult | None = None


@dataclass(frozen=True, slots=True)
class LegacyMigrationLiveHealthRequest:
    source_server_id: str
    source_fingerprint: str
    owner_user_id: str
    target_server_id: str
    normalized_endpoint: str
    transport: UserMCPTransport
    protocol_preference: UserMCPProtocolPreference
    auth_type: UserMCPAuthType
    auth_metadata: Mapping[str, Any]
    credential_values: Mapping[str, Any] | None
    policy: LegacyMigrationHealthPolicy
    capability_bindings: tuple[tuple[str, str, str], ...]
    target_consumer_set_digest: str
    observed_at: str
    expires_at: str


LegacyMigrationLiveHealthValidator = Callable[
    [LegacyMigrationLiveHealthRequest],
    LegacyMigrationHealthResult | Awaitable[LegacyMigrationHealthResult],
]


class BuiltInLegacyMigrationLiveHealthValidator:
    """Production validator using the same policy-bound client and discovery contract."""

    provenance = "builtin-user-mcp-health-v1"

    def __init__(self, endpoint_policy: EndpointPolicy) -> None:
        self._endpoint_policy = endpoint_policy

    async def __call__(
        self, request: LegacyMigrationLiveHealthRequest
    ) -> LegacyMigrationHealthResult:
        from .user_client import UserMCPClientFactory

        server = UserMCPServer(
            server_id=request.target_server_id,
            owner_user_id=request.owner_user_id,
            display_name=request.source_server_id,
            routing_description="Legacy migration live validation.",
            endpoint_url=request.normalized_endpoint,
            transport=request.transport,
            protocol_preference=request.protocol_preference,
            auth_type=request.auth_type,
            auth_metadata=request.auth_metadata,
            enabled=True,
            credential_configured=request.credential_values is not None,
        )
        headers = _request_headers(
            request.auth_type,
            request.auth_metadata,
            request.credential_values,
        )
        factory = UserMCPClientFactory(self._endpoint_policy)
        attempts = 0
        discovered_catalog: list[Mapping[str, Any]] = []

        async def create_client():
            nonlocal attempts
            attempts += 1
            client = await factory.create(server, headers)
            return _CatalogCaptureClient(client, discovered_catalog)

        result = await run_health_discovery(
            create_client,
            timeout_seconds=request.policy.timeout_seconds_per_attempt,
            retry_delay_seconds=request.policy.retry_delay_seconds,
            cleanup_timeout_seconds=request.policy.cleanup_timeout_seconds,
        )
        discovered_by_name = {
            str(item.get("name") or "").strip(): item
            for item in discovered_catalog
        }
        available_contracts = tuple(
            sorted(
                (capability_id, expected_contract_fingerprint)
                for capability_id, tool_name, expected_contract_fingerprint in (
                    request.capability_bindings
                )
                if _catalog_contract_fingerprint(discovered_by_name.get(tool_name))
                == expected_contract_fingerprint
            )
        )
        available_capabilities = tuple(item[0] for item in available_contracts)
        return LegacyMigrationHealthResult(
            server_id=request.source_server_id,
            attempts=attempts,
            handshake_ok=result.available,
            discovery_ok=result.available,
            full_paginated_tool_list_ok=result.available,
            nonempty_legal_tool_ok=result.available,
            safe_error_code=result.error_code,
            target_server_id=request.target_server_id,
            source_fingerprint=request.source_fingerprint,
            target_consumer_set_digest=request.target_consumer_set_digest,
            catalog_fingerprint=(
                legacy_migration_catalog_fingerprint(discovered_catalog)
                if result.available
                else None
            ),
            capability_fingerprint=legacy_capability_contract_set_fingerprint(
                available_contracts
            ),
            available_capability_ids=available_capabilities,
            available_capability_contracts=available_contracts,
            observed_at=request.observed_at,
            expires_at=request.expires_at,
        )


class _CatalogCaptureClient:
    def __init__(self, client: Any, catalog: list[Mapping[str, Any]]) -> None:
        self._client = client
        self._catalog = catalog

    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        return self._client.server_capabilities

    async def initialize(self) -> Any:
        return await self._client.initialize()

    async def list_tools(self) -> list[Mapping[str, Any]]:
        tools = await self._client.list_tools()
        self._catalog[:] = [dict(item) for item in tools]
        return tools

    async def close(self) -> None:
        await self._client.close()


class LocalLegacyMigrationApplier:
    """Persist legacy configs only; runtime sessions, grants, and task state are untouched."""

    def __init__(
        self,
        *,
        storage: UserMCPConfigurationStoragePort,
        credential_cipher: MCPCredentialCipher,
        audit_reference_signer: MCPAuditReferenceSigner,
        endpoint_policy: EndpointPolicy,
        config: MCPRuntimeConfig,
        plan: LegacyMigrationPlan,
        service_account_owner: str,
        environ: Mapping[str, str] | None = None,
        now_fn: Any | None = None,
        live_health_validator: LegacyMigrationLiveHealthValidator | None = None,
        validator_provenance: str | None = None,
    ) -> None:
        owner = service_account_owner.strip()
        if not owner:
            raise LegacyMigrationApplyError("service_account_owner_required")
        if any(
            candidate.target_server_id
            != deterministic_migrated_server_id(candidate.source_server_id, owner)
            for candidate in plan.mapping_candidates
        ):
            raise LegacyMigrationApplyError("service_account_owner_mismatch")
        expected_owner_ref = legacy_target_consumer_reference(
            audit_reference_signer,
            owner,
        )
        if any(
            not hmac.compare_digest(candidate.owner_consumer_ref, expected_owner_ref)
            for candidate in plan.mapping_candidates
        ):
            raise LegacyMigrationApplyError("owner_consumer_reference_mismatch")
        self._storage = storage
        self._cipher = credential_cipher
        self._audit_signer = audit_reference_signer
        self._endpoint_policy = endpoint_policy
        self._servers = {server.server_id: server for server in config.servers}
        self._plan = plan
        self._owner = owner
        self._environ = environ if environ is not None else os.environ
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._live_health_validator = live_health_validator
        provenance = (validator_provenance or "").strip()
        if live_health_validator is not None and not provenance:
            raise LegacyMigrationApplyError("live_health_validator_provenance_required")
        self._validator_provenance = provenance

    def __call__(self, _artifact: Mapping[str, Any], *, idempotency_key: str) -> str:
        if idempotency_key != self._plan.plan_fingerprint:
            raise LegacyMigrationApplyError("migration_idempotency_key_mismatch")
        try:
            return asyncio.run(self._apply())
        except LegacyMigrationApplyError:
            raise
        except (CredentialSecurityError, SQLAlchemyError, RuntimeError):
            raise LegacyMigrationApplyError("legacy_apply_storage_failed") from None

    async def _apply(self) -> str:
        if self._live_health_validator is None:
            raise LegacyMigrationApplyError("live_health_validator_required")
        storage_candidates: list[
            tuple[
                UserMCPServer,
                UserMCPCredentialRecord | None,
                MCPLegacyMigrationRecord,
            ]
        ] = []
        for candidate in self._plan.mapping_candidates:
            source = self._servers.get(candidate.source_server_id)
            if source is None:
                raise LegacyMigrationApplyError("legacy_apply_source_missing")
            if (
                legacy_migration_source_fingerprint(source)
                != candidate.source_fingerprint
            ):
                raise LegacyMigrationApplyError(
                    "legacy_apply_source_fingerprint_mismatch"
                )
            replay_status = None
            replay_snapshot: Mapping[str, Any] | None = None
            if hasattr(
                self._storage,
                "get_legacy_mcp_migration_replay_snapshot",
            ):
                replay_desired = self._desired_server(
                    source,
                    candidate.target_server_id,
                    source_fingerprint=candidate.source_fingerprint,
                    encrypt_credential=False,
                )
                replay_status, replay_snapshot = await self._dedicated_replay_status(
                    candidate,
                    replay_desired,
                )
            if replay_status == "exact":
                stored_server = replay_snapshot.get("server") if replay_snapshot else None
                stored_auth = (
                    stored_server.get("auth_metadata")
                    if isinstance(stored_server, Mapping)
                    else None
                )
                if not isinstance(stored_auth, Mapping):
                    raise LegacyMigrationApplyError("legacy_apply_storage_failed")
                replay_desired = replace(
                    replay_desired,
                    server=replace(
                        replay_desired.server,
                        auth_metadata=dict(stored_auth),
                    ),
                )
                live_replay = await self._validate_live(
                    replay_desired,
                    source.server_id,
                )
                if replay_snapshot is None:
                    raise LegacyMigrationApplyError("legacy_apply_storage_failed")
                self._assert_live_replay_continuity(
                    snapshot=replay_snapshot,
                    desired=live_replay,
                    candidate=candidate,
                )
                continue
            desired = self._desired_server(
                source,
                candidate.target_server_id,
                source_fingerprint=candidate.source_fingerprint,
            )
            desired = await self._validate_live(desired, source.server_id)
            if replay_status == "missing":
                storage_candidates.append(
                    (
                        desired.server,
                        _credential_record(desired.server, desired.encrypted),
                        self._migration_record(desired.server, candidate),
                    )
                )
                continue
            existing = await self._storage.get_user_mcp_server(
                self._owner, candidate.target_server_id
            )
            if existing is not None:
                stored_credential = await self._assert_equivalent(existing, desired)
                storage_candidates.append(
                    (
                        existing,
                        stored_credential,
                        self._migration_record(existing, candidate),
                    )
                )
            else:
                storage_candidates.append(
                    (
                        desired.server,
                        _credential_record(desired.server, desired.encrypted),
                        self._migration_record(desired.server, candidate),
                    )
                )

        if not storage_candidates:
            return "already_applied"
        try:
            result = await self._storage.apply_legacy_mcp_migration_atomic(
                tuple(storage_candidates)
            )
        except ValueError:
            raise LegacyMigrationApplyError("legacy_apply_target_conflict") from None
        except SQLAlchemyError:
            raise LegacyMigrationApplyError("legacy_apply_storage_failed") from None
        if (
            len(result.servers) != len(storage_candidates)
            or len(result.records) != len(storage_candidates)
        ):
            raise LegacyMigrationApplyError("legacy_apply_storage_failed")
        return "applied" if result.applied else "already_applied"

    def _desired_server(
        self,
        source: MCPServerConfig,
        target_server_id: str,
        *,
        source_fingerprint: str,
        encrypt_credential: bool = True,
    ) -> _DesiredServer:
        now = self._now()
        parsed = urlsplit(source.endpoint)
        if parsed.query or parsed.fragment:
            raise LegacyMigrationApplyError(
                "legacy_apply_endpoint_query_or_fragment_forbidden"
            )
        endpoint = self._endpoint_policy.validate(source.endpoint)
        transport = _transport(source.transport)
        protocol = _protocol(source)
        auth_type, metadata, values = _credential_payload(source, self._environ)
        encrypted = None
        if values is not None and encrypt_credential:
            encrypted = self._cipher.encrypt(
                owner_user_id=self._owner,
                server_id=target_server_id,
                auth_type=str(auth_type),
                values=values,
            )
        provenance = {
            "schema": "legacy_mcp_migration_provenance.v1",
            "source_server_id": source.server_id,
            "source_fingerprint": source_fingerprint,
            "owner_user_id": self._owner,
            "target_server_id": target_server_id,
            "credential_digest": "pending-live-validation",
            "credential_security_version": 1,
        }
        metadata = {**metadata, "migration_provenance": provenance}
        return _DesiredServer(
            server=UserMCPServer(
                server_id=target_server_id,
                owner_user_id=self._owner,
                display_name=source.server_id,
                routing_description="Migrated legacy MCP server.",
                endpoint_url=endpoint.normalized_url,
                transport=transport,
                protocol_preference=protocol,
                auth_type=auth_type,
                auth_metadata=metadata,
                enabled=source.enabled,
                health_status=UserMCPHealthStatus.UNTESTED,
                credential_configured=values is not None,
                last_tested_at=None,
                created_at=now,
                updated_at=now,
            ),
            credential_values=values,
            encrypted=encrypted,
            source_fingerprint=source_fingerprint,
            credential_digest="pending-live-validation",
        )

    async def _dedicated_replay_status(
        self,
        candidate: LegacyMigrationMappingCandidate,
        desired: _DesiredServer,
    ) -> tuple[str | None, Mapping[str, Any] | None]:
        lookup = getattr(
            self._storage,
            "get_legacy_mcp_migration_replay_snapshot",
            None,
        )
        if lookup is None:
            return None, None
        migration_id = legacy_migration_record_id(
            plan_fingerprint=self._plan.plan_fingerprint,
            source_server_id=candidate.source_server_id,
            target_server_id=candidate.target_server_id,
        )
        snapshot = await lookup(
            migration_id=migration_id,
            plan_fingerprint=self._plan.plan_fingerprint,
            source_server_id=candidate.source_server_id,
            source_fingerprint=candidate.source_fingerprint,
            owner_consumer_ref=candidate.owner_consumer_ref,
            target_server_id=candidate.target_server_id,
        )
        if snapshot is None:
            return "missing", None
        if snapshot.get("status") != "exact":
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        server = snapshot.get("server")
        record = snapshot.get("record")
        if not isinstance(server, Mapping) or not isinstance(record, Mapping):
            raise LegacyMigrationApplyError("legacy_apply_storage_failed")
        self._assert_replay_snapshot(
            server=server,
            record=record,
            desired=desired,
            candidate=candidate,
            migration_id=migration_id,
        )
        return "exact", snapshot

    def _assert_live_replay_continuity(
        self,
        *,
        snapshot: Mapping[str, Any],
        desired: _DesiredServer,
        candidate: LegacyMigrationMappingCandidate,
    ) -> None:
        stored_record = snapshot.get("record")
        if not isinstance(stored_record, Mapping):
            raise LegacyMigrationApplyError("legacy_apply_storage_failed")
        current_record = self._migration_record(desired.server, candidate)
        immutable_continuity_fields = (
            "migration_id",
            "event_type",
            "plan_fingerprint",
            "source_server_id",
            "source_fingerprint",
            "owner_consumer_ref",
            "target_server_id",
            "target_consumer_set_digest",
            "capability_obligations_fingerprint",
            "capability_fingerprint",
            "disposition",
        )
        if any(
            stored_record.get(field) != getattr(current_record, field)
            for field in immutable_continuity_fields
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")

    def _assert_replay_snapshot(
        self,
        *,
        server: Mapping[str, Any],
        record: Mapping[str, Any],
        desired: _DesiredServer,
        candidate: LegacyMigrationMappingCandidate,
        migration_id: str,
    ) -> None:
        expected_server = {
            "server_id": desired.server.server_id,
            "owner_user_id": desired.server.owner_user_id,
            "display_name": desired.server.display_name,
            "routing_description": desired.server.routing_description,
            "endpoint_url": desired.server.endpoint_url,
            "transport": desired.server.transport.value,
            "protocol_preference": desired.server.protocol_preference.value,
            "auth_type": desired.server.auth_type.value,
            "enabled": desired.server.enabled,
            "credential_configured": desired.server.credential_configured,
        }
        if any(server.get(key) != value for key, value in expected_server.items()):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        stored_auth = server.get("auth_metadata")
        if not isinstance(stored_auth, Mapping):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        stored_provenance = stored_auth.get("migration_provenance")
        desired_provenance = desired.server.auth_metadata.get("migration_provenance")
        if not isinstance(stored_provenance, Mapping) or not isinstance(
            desired_provenance, Mapping
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        if {
            key: value
            for key, value in stored_auth.items()
            if key != "migration_provenance"
        } != {
            key: value
            for key, value in desired.server.auth_metadata.items()
            if key != "migration_provenance"
        }:
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        static_keys = (
            "schema",
            "source_server_id",
            "source_fingerprint",
            "owner_user_id",
            "target_server_id",
            "credential_security_version",
        )
        if any(
            stored_provenance.get(key) != desired_provenance.get(key)
            for key in static_keys
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        validator_raw = stored_provenance.get("validator_provenance")
        stored_digest = stored_provenance.get("credential_digest")
        stored_storage_digest = stored_provenance.get(
            "credential_storage_digest"
        )
        actual_storage_digest = server.get("credential_storage_digest")
        if (
            not isinstance(validator_raw, str)
            or not isinstance(stored_digest, str)
            or not isinstance(stored_storage_digest, str)
            or actual_storage_digest != stored_storage_digest
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        try:
            validator = json.loads(validator_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict") from None
        if not isinstance(validator, Mapping):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        impact = self._impact_for(candidate.source_server_id)
        obligations_fingerprint = _capability_obligations_fingerprint(
            impact.obligations
        )
        expected_digest = legacy_migration_credential_provenance_digest(
            self._audit_signer,
            credential_values=desired.credential_values,
            owner_user_id=self._owner,
            target_server_id=desired.server.server_id,
            source_fingerprint=desired.source_fingerprint,
            provenance=stored_provenance,
        )
        expected_record = {
            "migration_id": migration_id,
            "event_type": "mcp.legacy.config_migrated",
            "plan_fingerprint": self._plan.plan_fingerprint,
            "source_server_id": candidate.source_server_id,
            "source_fingerprint": candidate.source_fingerprint,
            "owner_consumer_ref": candidate.owner_consumer_ref,
            "target_server_id": candidate.target_server_id,
            "target_consumer_set_digest": impact.target_consumer_set_digest,
            "capability_obligations_fingerprint": obligations_fingerprint,
            "catalog_fingerprint": validator.get("catalog_fingerprint"),
            "capability_fingerprint": validator.get("capability_fingerprint"),
            "validator_provenance_fingerprint": _safe_sha256(validator_raw),
            "credential_digest": expected_digest,
            "disposition": "migrate_owner",
        }
        if stored_digest != expected_digest or any(
            record.get(key) != value for key, value in expected_record.items()
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        if (
            validator.get("target_consumer_set_digest")
            != impact.target_consumer_set_digest
            or validator.get("capability_obligations_fingerprint")
            != obligations_fingerprint
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        if not _same_instant(
            record.get("occurred_at"), stored_provenance.get("observed_at")
        ) or not _same_instant(
            record.get("evidence_expires_at"), stored_provenance.get("expires_at")
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")

    async def _validate_live(
        self, desired: _DesiredServer, source_server_id: str
    ) -> _DesiredServer:
        observed_at = self._now()
        timeout_seconds = LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
        expires_at = observed_at + timedelta(seconds=timeout_seconds)
        request = LegacyMigrationLiveHealthRequest(
            source_server_id=source_server_id,
            source_fingerprint=desired.source_fingerprint,
            owner_user_id=self._owner,
            target_server_id=desired.server.server_id,
            normalized_endpoint=desired.server.endpoint_url,
            transport=desired.server.transport,
            protocol_preference=desired.server.protocol_preference,
            auth_type=desired.server.auth_type,
            auth_metadata=desired.server.auth_metadata,
            credential_values=desired.credential_values,
            policy=LEGACY_MIGRATION_HEALTH_POLICY,
            capability_bindings=self._capability_bindings(source_server_id),
            target_consumer_set_digest=self._impact_for(
                source_server_id
            ).target_consumer_set_digest,
            observed_at=observed_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )

        async def invoke() -> LegacyMigrationHealthResult:
            result = await asyncio.to_thread(self._live_health_validator, request)  # type: ignore[arg-type]
            if isinstance(result, Awaitable):
                result = await result
            return result

        try:
            result = await asyncio.wait_for(invoke(), timeout=timeout_seconds)
        except TimeoutError:
            raise LegacyMigrationApplyError("live_health_validation_timeout") from None
        if not isinstance(result, LegacyMigrationHealthResult):
            raise LegacyMigrationApplyError("live_health_result_invalid")
        if result.server_id != source_server_id or not result.healthy:
            raise LegacyMigrationApplyError("live_health_validation_failed")
        completed_at = self._now()
        if completed_at > expires_at:
            raise LegacyMigrationApplyError("live_health_validation_expired")
        if legacy_migration_health_result_blockers(
            self._plan,
            result,
            now=completed_at,
        ):
            raise LegacyMigrationApplyError("live_health_continuity_failed")
        provenance = dict(desired.server.auth_metadata["migration_provenance"])
        provenance.update(
            {
                "validator_provenance": _continuity_validator_provenance(
                    self._validator_provenance,
                    result,
                    self._impact_for(source_server_id).obligations,
                ),
                "observed_at": result.observed_at,
                "expires_at": result.expires_at,
            }
        )
        if desired.encrypted is not None or desired.credential_values is None:
            provenance["credential_storage_digest"] = (
                _credential_storage_digest(desired.encrypted)
            )
        elif not isinstance(
            provenance.get("credential_storage_digest"), str
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        credential_digest = legacy_migration_credential_provenance_digest(
            self._audit_signer,
            credential_values=desired.credential_values,
            owner_user_id=self._owner,
            target_server_id=desired.server.server_id,
            source_fingerprint=desired.source_fingerprint,
            provenance=provenance,
        )
        provenance["credential_digest"] = credential_digest
        server = replace(
            desired.server,
            auth_metadata={
                **desired.server.auth_metadata,
                "migration_provenance": provenance,
            },
            health_status=(
                UserMCPHealthStatus.AVAILABLE
                if desired.server.enabled
                else UserMCPHealthStatus.DISABLED
            ),
            last_tested_at=completed_at,
            updated_at=completed_at,
        )
        return replace(
            desired,
            server=server,
            credential_digest=credential_digest,
            continuity_result=result,
        )

    def _capability_bindings(
        self, source_server_id: str
    ) -> tuple[tuple[str, str, str], ...]:
        expected_contracts = {
            (obligation.capability_id, obligation.source_contract_fingerprint)
            for obligation in self._impact_for(source_server_id).obligations
        }
        by_capability: dict[str, str] = {}
        for capability_id, fingerprint in expected_contracts:
            existing = by_capability.setdefault(capability_id, fingerprint)
            if existing != fingerprint:
                raise LegacyMigrationApplyError(
                    "legacy_apply_capability_contract_ambiguous"
                )
        bindings = tuple(
            sorted(
                (
                    tool.effective_capability_id(source_server_id),
                    tool.tool_name,
                    by_capability[tool.effective_capability_id(source_server_id)],
                )
                for tool in self._servers[source_server_id].tools
                if tool.expose
            )
        )
        if set(by_capability) != {binding[0] for binding in bindings}:
            raise LegacyMigrationApplyError(
                "legacy_apply_capability_contract_incomplete"
            )
        return bindings

    def _impact_for(self, source_server_id: str):
        impact = next(
            (
                item
                for item in self._plan.consumer_capability_impact
                if item.server_id == source_server_id
            ),
            None,
        )
        if impact is None:
            raise LegacyMigrationApplyError("legacy_apply_capability_impact_missing")
        return impact

    async def _assert_equivalent(
        self, existing: UserMCPServer, desired: _DesiredServer
    ) -> UserMCPCredentialRecord | None:
        comparable = (
            "owner_user_id",
            "server_id",
            "display_name",
            "routing_description",
            "endpoint_url",
            "transport",
            "protocol_preference",
            "auth_type",
            "enabled",
            "credential_configured",
        )
        if any(
            getattr(existing, field) != getattr(desired.server, field)
            for field in comparable
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        existing_auth = {
            key: value
            for key, value in existing.auth_metadata.items()
            if key != "migration_provenance"
        }
        desired_auth = {
            key: value
            for key, value in desired.server.auth_metadata.items()
            if key != "migration_provenance"
        }
        if existing_auth != desired_auth:
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        existing_provenance = existing.auth_metadata.get("migration_provenance")
        desired_provenance = desired.server.auth_metadata.get("migration_provenance")
        if not isinstance(existing_provenance, Mapping):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        static_keys = (
            "schema",
            "source_server_id",
            "source_fingerprint",
            "owner_user_id",
            "target_server_id",
            "credential_security_version",
        )
        if any(
            existing_provenance.get(key) != desired_provenance.get(key)
            for key in static_keys
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        try:
            observed_at = datetime.fromisoformat(existing_provenance["observed_at"])
            expires_at = datetime.fromisoformat(existing_provenance["expires_at"])
        except (KeyError, TypeError, ValueError):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            ) from None
        if observed_at >= expires_at:
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        record = await self._storage.get_user_mcp_credential(
            existing.owner_user_id, existing.server_id
        )
        if desired.credential_values is None:
            if record is not None:
                raise LegacyMigrationApplyError("legacy_apply_target_conflict")
            actual = None
        else:
            if record is None:
                raise LegacyMigrationApplyError("legacy_apply_target_conflict")
            try:
                actual = self._cipher.decrypt(
                    record,
                    owner_user_id=existing.owner_user_id,
                    server_id=existing.server_id,
                    auth_type=str(existing.auth_type),
                )
            except CredentialSecurityError:
                raise LegacyMigrationApplyError(
                    "legacy_apply_target_conflict"
                ) from None
        if actual != (
            dict(desired.credential_values)
            if desired.credential_values is not None
            else None
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        expected_digest = legacy_migration_credential_provenance_digest(
            self._audit_signer,
            credential_values=actual,
            owner_user_id=existing.owner_user_id,
            target_server_id=existing.server_id,
            source_fingerprint=desired.source_fingerprint,
            provenance=existing_provenance,
        )
        stored_digest = existing_provenance.get("credential_digest")
        if not isinstance(stored_digest, str) or not hmac.compare_digest(
            stored_digest,
            expected_digest,
        ):
            raise LegacyMigrationApplyError("legacy_apply_target_conflict")
        return record

    def _migration_record(
        self,
        server: UserMCPServer,
        candidate: LegacyMigrationMappingCandidate,
    ) -> MCPLegacyMigrationRecord:
        provenance = server.auth_metadata.get("migration_provenance")
        if not isinstance(provenance, Mapping):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        validator_value = provenance.get("validator_provenance")
        credential_digest_value = provenance.get("credential_digest")
        observed_value = provenance.get("observed_at")
        expires_value = provenance.get("expires_at")
        if not isinstance(validator_value, str):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        if not isinstance(credential_digest_value, str):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        if not isinstance(observed_value, str) or not isinstance(
            expires_value, str
        ):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        validator_raw = validator_value
        credential_digest = credential_digest_value
        try:
            validator = json.loads(validator_raw)
            occurred_at = datetime.fromisoformat(observed_value)
            evidence_expires_at = datetime.fromisoformat(expires_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            ) from None
        if not isinstance(validator, Mapping):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        impact = self._impact_for(candidate.source_server_id)
        obligations_fingerprint = _capability_obligations_fingerprint(impact.obligations)
        catalog_fingerprint = validator.get("catalog_fingerprint")
        capability_fingerprint = validator.get("capability_fingerprint")
        consumer_digest = validator.get("target_consumer_set_digest")
        validator_obligations = validator.get("capability_obligations_fingerprint")
        if (
            not isinstance(catalog_fingerprint, str)
            or not isinstance(capability_fingerprint, str)
            or consumer_digest != impact.target_consumer_set_digest
            or validator_obligations != obligations_fingerprint
        ):
            raise LegacyMigrationApplyError(
                "legacy_apply_existing_target_provenance_unavailable"
            )
        return MCPLegacyMigrationRecord(
            migration_id=legacy_migration_record_id(
                plan_fingerprint=self._plan.plan_fingerprint,
                source_server_id=candidate.source_server_id,
                target_server_id=candidate.target_server_id,
            ),
            event_type="mcp.legacy.config_migrated",
            plan_fingerprint=self._plan.plan_fingerprint,
            source_server_id=candidate.source_server_id,
            source_fingerprint=candidate.source_fingerprint,
            owner_consumer_ref=candidate.owner_consumer_ref,
            target_server_id=candidate.target_server_id,
            target_consumer_set_digest=impact.target_consumer_set_digest,
            capability_obligations_fingerprint=obligations_fingerprint,
            catalog_fingerprint=catalog_fingerprint,
            capability_fingerprint=capability_fingerprint,
            validator_provenance_fingerprint=_safe_sha256(validator_raw),
            credential_digest=credential_digest,
            disposition="migrate_owner",
            occurred_at=occurred_at,
            evidence_expires_at=evidence_expires_at,
        )


def _continuity_validator_provenance(
    validator: str,
    result: LegacyMigrationHealthResult,
    obligations: Iterable[LegacyConsumerCapabilityObligation],
) -> str:
    return json.dumps(
        {
            "schema": "legacy_mcp_continuity_provenance.v1",
            "validator": validator,
            "catalog_fingerprint": result.catalog_fingerprint,
            "capability_fingerprint": result.capability_fingerprint,
            "capability_obligations_fingerprint": (
                _capability_obligations_fingerprint(obligations)
            ),
            "target_consumer_set_digest": result.target_consumer_set_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _catalog_contract_fingerprint(
    item: Mapping[str, Any] | None,
) -> str | None:
    if item is None:
        return None
    input_schema = item.get("inputSchema")
    output_schema = item.get("outputSchema")
    if not isinstance(input_schema, Mapping) or (
        output_schema is not None and not isinstance(output_schema, Mapping)
    ):
        return None
    return legacy_tool_contract_fingerprint(
        input_schema=input_schema,
        output_schema=(output_schema if isinstance(output_schema, Mapping) else None),
    )


def _capability_obligations_fingerprint(
    obligations: Iterable[LegacyConsumerCapabilityObligation],
) -> str:
    closed_obligations = tuple(
        sorted(
            {
                (
                    obligation.consumer_ref,
                    obligation.capability_id,
                    obligation.source_contract_fingerprint,
                    obligation.resolution.value,
                )
                for obligation in obligations
            }
        )
    )
    return _safe_sha256(
        {
            "schema": "legacy_mcp_capability_obligations.v1",
            "obligations": closed_obligations,
        }
    )


def _safe_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _credential_storage_digest(
    encrypted: EncryptedCredential | None,
) -> str:
    if encrypted is None:
        material = "legacy_mcp_credential_storage.v1:none"
    else:
        material = (
            "legacy_mcp_credential_storage.v1:"
            f"{encrypted.ciphertext.hex()}:{encrypted.nonce.hex()}:"
            f"{encrypted.encryption_version}"
        )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _same_instant(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_at = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_at = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return False
    if left_at.tzinfo is None:
        left_at = left_at.replace(tzinfo=timezone.utc)
    if right_at.tzinfo is None:
        right_at = right_at.replace(tzinfo=timezone.utc)
    return left_at.astimezone(timezone.utc) == right_at.astimezone(timezone.utc)


def _transport(value: str) -> UserMCPTransport:
    try:
        return UserMCPTransport(value)
    except ValueError:
        raise LegacyMigrationApplyError("legacy_apply_transport_unsupported") from None


def _protocol(source: MCPServerConfig) -> UserMCPProtocolPreference:
    if not source.protocol_version_pinned:
        return UserMCPProtocolPreference.AUTO
    try:
        return UserMCPProtocolPreference(source.protocol_version)
    except ValueError:
        raise LegacyMigrationApplyError("legacy_apply_protocol_unsupported") from None


def _credential_payload(
    source: MCPServerConfig, environ: Mapping[str, str]
) -> tuple[UserMCPAuthType, dict[str, Any], dict[str, Any] | None]:
    if source.request_headers and source.auth.type != "none":
        raise LegacyMigrationApplyError("legacy_apply_auth_combination_unsupported")
    if source.request_headers:
        validated = validate_static_headers(source.request_headers)
        return (
            UserMCPAuthType.STATIC_HEADERS,
            {"header_names": list(validated.names)},
            {"values": validated.credential_values.reveal()},
        )
    if source.auth.type == "none":
        return UserMCPAuthType.NONE, {}, None
    if source.auth.type == "bearer_env":
        value = environ.get(source.auth.token_env, "")
        if not value:
            raise LegacyMigrationApplyError("legacy_apply_credential_env_missing")
        return UserMCPAuthType.BEARER, {}, {"token": value}
    if source.auth.type == "api_key_env":
        value = environ.get(source.auth.api_key_env, "")
        if not value:
            raise LegacyMigrationApplyError("legacy_apply_credential_env_missing")
        header_name = validate_auth_header_name(source.auth.header_name)
        return (
            UserMCPAuthType.API_KEY_HEADER,
            {"header_name": header_name},
            {"value": value},
        )
    raise LegacyMigrationApplyError("legacy_apply_auth_unsupported")


def _request_headers(
    auth_type: UserMCPAuthType,
    auth_metadata: Mapping[str, Any],
    credential_values: Mapping[str, Any] | None,
) -> dict[str, str]:
    if auth_type is UserMCPAuthType.NONE:
        return {}
    if credential_values is None:
        raise LegacyMigrationApplyError("live_health_credential_missing")
    if auth_type is UserMCPAuthType.BEARER:
        token = credential_values.get("token")
        if not isinstance(token, str) or not token:
            raise LegacyMigrationApplyError("live_health_credential_invalid")
        return {"Authorization": f"Bearer {token}"}
    if auth_type is UserMCPAuthType.API_KEY_HEADER:
        name = validate_auth_header_name(str(auth_metadata.get("header_name") or ""))
        value = credential_values.get("value")
        if not isinstance(value, str) or not value:
            raise LegacyMigrationApplyError("live_health_credential_invalid")
        return {name: value}
    values = credential_values.get("values")
    if not isinstance(values, Mapping):
        raise LegacyMigrationApplyError("live_health_credential_invalid")
    return {str(name): str(value) for name, value in values.items()}


def _credential_record(
    server: UserMCPServer, encrypted: EncryptedCredential | None
) -> UserMCPCredentialRecord | None:
    if encrypted is None:
        return None
    return UserMCPCredentialRecord(
        owner_user_id=server.owner_user_id,
        server_id=server.server_id,
        credential_ciphertext=encrypted.ciphertext,
        credential_nonce=encrypted.nonce,
        encryption_version=encrypted.encryption_version,
        credential_updated_at=server.updated_at,
    )

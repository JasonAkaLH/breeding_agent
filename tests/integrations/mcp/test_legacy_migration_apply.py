from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.core.enums import (
    UserMCPAuthType,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import UserMCPServer
from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.client import MCPClientError, MCPProtocolError
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.legacy_migration import (
    LEGACY_MIGRATION_HEALTH_POLICY,
    LegacyConsumerScope,
    LegacyDisposition,
    LegacyServerClassification,
    legacy_capability_contract_set_fingerprint,
    legacy_migration_health_result_blockers,
    legacy_migration_catalog_fingerprint,
    legacy_target_consumer_reference,
    plan_legacy_mcp_config_migration,
)
from src.integrations.mcp.legacy_migration_apply import (
    BuiltInLegacyMigrationLiveHealthValidator,
    LegacyMigrationApplyError,
    LegacyMigrationLiveHealthRequest,
    LocalLegacyMigrationApplier,
)
from src.integrations.mcp.legacy_migration import LegacyMigrationHealthResult
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from tests.master_key_support import audit_reference_signer, credential_cipher

SERVICE_CONSUMER_REF = legacy_target_consumer_reference(
    audit_reference_signer(b"a" * 32),
    "service-owner",
)


class _PublicResolver:
    def resolve(self, _hostname: str, _port: int) -> tuple[str, ...]:
        return ("8.8.8.8",)


class _FakeHealthClient:
    server_capabilities: dict[str, object] = {"tools": {}}

    async def initialize(self):
        return {}

    async def list_tools(self):
        return [
            {
                "name": "lookup",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            }
        ]

    async def close(self):
        return None


class _BlockingCloseHealthClient(_FakeHealthClient):
    async def close(self):
        await asyncio.Event().wait()


class _FakeClientFactory:
    created: list[tuple[object, object]] = []

    def __init__(self, _endpoint_policy):
        pass

    async def create(self, server, headers):
        self.created.append((server, headers))
        return _FakeHealthClient()


class _SequencedClientFactory:
    outcomes: list[object] = []
    calls = 0

    def __init__(self, _endpoint_policy):
        pass

    async def create(self, _server, _headers):
        outcome = self.outcomes[self.calls]
        self.__class__.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class LegacyMigrationApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.tempdir.name) / "state.db")
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.cipher = credential_cipher(b"a" * 32)
        self.audit_signer = audit_reference_signer(b"a" * 32)
        self.config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "https://secret-host.example/rpc",
                        "auth": {
                            "type": "bearer_env",
                            "token_env": "CRM_SECRET",
                        },
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    }
                ],
            }
        )
        self.plan = plan_legacy_mcp_config_migration(
            self.config,
            (
                LegacyServerClassification(
                    "crm",
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                ),
            ),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_apply_is_deterministic_and_reencrypts_credentials(self) -> None:
        applier = self._applier("secret-value")
        self.assertEqual(
            applier({}, idempotency_key=self.plan.plan_fingerprint), "applied"
        )
        self.assertEqual(
            applier({}, idempotency_key=self.plan.plan_fingerprint),
            "already_applied",
        )
        server = asyncio.run(
            self.storage.get_user_mcp_server(
                "service-owner", self.plan.mapping_candidates[0].target_server_id
            )
        )
        self.assertNotEqual(server.server_id, "crm")
        provenance = server.auth_metadata["migration_provenance"]
        self.assertEqual(
            provenance["source_fingerprint"], self.plan.inventory[0].source_fingerprint
        )
        self.assertEqual(provenance["owner_user_id"], "service-owner")
        self.assertEqual(provenance["target_server_id"], server.server_id)
        validator_provenance = json.loads(provenance["validator_provenance"])
        self.assertEqual(validator_provenance["validator"], "test-validator-v1")
        self.assertRegex(
            validator_provenance["catalog_fingerprint"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertTrue(provenance["credential_digest"].startswith("hmac-sha256:"))
        self.assertNotIn("secret-value", str(provenance))
        self.assertLess(provenance["observed_at"], provenance["expires_at"])
        record = asyncio.run(
            self.storage.get_user_mcp_credential(
                "service-owner", self.plan.mapping_candidates[0].target_server_id
            )
        )
        self.assertEqual(server.owner_user_id, "service-owner")
        self.assertEqual(
            self.cipher.decrypt(
                record,
                owner_user_id="service-owner",
                server_id=self.plan.mapping_candidates[0].target_server_id,
                auth_type="bearer",
            ),
            {"token": "secret-value"},
        )
        durable = applier._migration_record(server, self.plan.mapping_candidates[0])
        stored_durable = asyncio.run(
            self.storage.get_mcp_legacy_migration_record(durable.migration_id)
        )
        self.assertEqual(stored_durable, durable)
        self.assertEqual(durable.event_type, "mcp.legacy.config_migrated")
        durable_payload = json.dumps(
            {
                field: getattr(durable, field)
                for field in durable.__dataclass_fields__
            },
            default=str,
            sort_keys=True,
        )
        self.assertNotIn("service-owner", durable_payload)
        self.assertNotIn("secret-host", durable_payload)
        self.assertNotIn("secret-value", durable_payload)

        conflicting = self._applier("different-secret")
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "legacy_apply_target_conflict"
        ):
            conflicting({}, idempotency_key=self.plan.plan_fingerprint)

    def test_idempotent_rerun_accepts_fresh_catalog_evidence(self) -> None:
        first = self._applier("secret-value")
        self.assertEqual(
            first({}, idempotency_key=self.plan.plan_fingerprint), "applied"
        )

        def refreshed(request: LegacyMigrationLiveHealthRequest):
            result = self._healthy_validator(request)
            return replace(
                result,
                catalog_fingerprint=f"sha256:{'e' * 64}",
            )

        second = self._applier("secret-value", validator=refreshed)
        self.assertEqual(
            second({}, idempotency_key=self.plan.plan_fingerprint),
            "already_applied",
        )

    def test_owner_and_unrepresentable_auth_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "service_account_owner_mismatch"
        ):
            LocalLegacyMigrationApplier(
                storage=self.storage,
                credential_cipher=self.cipher,
                audit_reference_signer=self.audit_signer,
                endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
                config=self.config,
                plan=self.plan,
                service_account_owner="other-owner",
                environ={"CRM_SECRET": "secret-value"},
                live_health_validator=self._healthy_validator,
                validator_provenance="test-validator-v1",
            )

        forged_plan = replace(
            self.plan,
            mapping_candidates=(
                replace(
                    self.plan.mapping_candidates[0],
                    owner_consumer_ref=f"hmac-sha256:{'f' * 64}",
                ),
            ),
        )
        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "owner_consumer_reference_mismatch",
        ):
            LocalLegacyMigrationApplier(
                storage=self.storage,
                credential_cipher=self.cipher,
                audit_reference_signer=self.audit_signer,
                endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
                config=self.config,
                plan=forged_plan,
                service_account_owner="service-owner",
                environ={"CRM_SECRET": "secret-value"},
                live_health_validator=self._healthy_validator,
                validator_provenance="test-validator-v1",
            )

        combined = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "https://secret-host.example/rpc",
                        "headers": {"X-Static": "secret"},
                        "auth": {
                            "type": "bearer_env",
                            "token_env": "CRM_SECRET",
                        },
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    }
                ],
            }
        )
        combined_plan = plan_legacy_mcp_config_migration(
            combined,
            (
                LegacyServerClassification(
                    "crm",
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                ),
            ),
        )
        applier = LocalLegacyMigrationApplier(
            storage=self.storage,
            credential_cipher=self.cipher,
            audit_reference_signer=self.audit_signer,
            endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
            config=combined,
            plan=combined_plan,
            service_account_owner="service-owner",
            environ={"CRM_SECRET": "secret-value"},
            live_health_validator=self._healthy_validator,
            validator_provenance="test-validator-v1",
        )
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "legacy_apply_auth_combination_unsupported"
        ):
            applier({}, idempotency_key=combined_plan.plan_fingerprint)

    def test_live_validation_is_required_and_failure_writes_nothing(self) -> None:
        applier = LocalLegacyMigrationApplier(
            storage=self.storage,
            credential_cipher=self.cipher,
            audit_reference_signer=self.audit_signer,
            endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
            config=self.config,
            plan=self.plan,
            service_account_owner="service-owner",
            environ={"CRM_SECRET": "secret-value"},
        )
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "live_health_validator_required"
        ):
            applier({}, idempotency_key=self.plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

    def test_apply_rejects_config_that_no_longer_matches_the_approved_plan(
        self,
    ) -> None:
        changed = replace(
            self.config,
            servers=(
                replace(
                    self.config.servers[0],
                    endpoint="https://different.example/rpc",
                ),
            ),
        )
        applier = self._applier_for(changed, self.plan, self._healthy_validator)

        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "legacy_apply_source_fingerprint_mismatch",
        ):
            applier({}, idempotency_key=self.plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

        def unhealthy(request: LegacyMigrationLiveHealthRequest):
            self.assertEqual(request.credential_values, {"token": "secret-value"})
            self.assertEqual(
                request.normalized_endpoint, "https://secret-host.example/rpc"
            )
            return LegacyMigrationHealthResult(
                request.source_server_id,
                2,
                True,
                True,
                True,
                False,
                "no_legal_tools",
            )

        failed = self._applier("secret-value", validator=unhealthy)
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "live_health_validation_failed"
        ):
            failed({}, idempotency_key=self.plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

    def test_builtin_validator_uses_exact_endpoint_and_current_credentials(
        self,
    ) -> None:
        _FakeClientFactory.created.clear()
        validator = BuiltInLegacyMigrationLiveHealthValidator(
            EndpointPolicy(resolver=_PublicResolver())
        )
        observed_at = datetime.now(timezone.utc)
        request = LegacyMigrationLiveHealthRequest(
            source_server_id="crm",
            source_fingerprint=self.plan.inventory[0].source_fingerprint,
            owner_user_id="service-owner",
            target_server_id=self.plan.mapping_candidates[0].target_server_id,
            normalized_endpoint="https://secret-host.example/rpc",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
            auth_type=UserMCPAuthType.BEARER,
            auth_metadata={},
            credential_values={"token": "current-secret"},
            policy=LEGACY_MIGRATION_HEALTH_POLICY,
            capability_bindings=(
                (
                    "mcp.crm.lookup",
                    "lookup",
                    self.plan.consumer_capability_impact[0]
                    .obligations[0]
                    .source_contract_fingerprint,
                ),
            ),
            target_consumer_set_digest=self.plan.consumer_capability_impact[
                0
            ].target_consumer_set_digest,
            observed_at=observed_at.isoformat(),
            expires_at=(
                observed_at
                + timedelta(
                    seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
                )
            ).isoformat(),
        )
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _FakeClientFactory,
        ):
            result = asyncio.run(validator(request))
        self.assertTrue(result.healthy)
        server, headers = _FakeClientFactory.created[0]
        assert isinstance(server, UserMCPServer)
        self.assertEqual(server.endpoint_url, request.normalized_endpoint)
        self.assertEqual(headers, {"Authorization": "Bearer current-secret"})

    def test_builtin_validator_records_actual_retry_attempts(self) -> None:
        validator = BuiltInLegacyMigrationLiveHealthValidator(
            EndpointPolicy(resolver=_PublicResolver())
        )
        observed_at = datetime.now(timezone.utc)
        request = LegacyMigrationLiveHealthRequest(
            source_server_id="crm",
            source_fingerprint=self.plan.inventory[0].source_fingerprint,
            owner_user_id="service-owner",
            target_server_id=self.plan.mapping_candidates[0].target_server_id,
            normalized_endpoint="https://secret-host.example/rpc",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
            auth_type=UserMCPAuthType.NONE,
            auth_metadata={},
            credential_values=None,
            policy=LEGACY_MIGRATION_HEALTH_POLICY,
            capability_bindings=(
                (
                    "mcp.crm.lookup",
                    "lookup",
                    self.plan.consumer_capability_impact[0]
                    .obligations[0]
                    .source_contract_fingerprint,
                ),
            ),
            target_consumer_set_digest=self.plan.consumer_capability_impact[
                0
            ].target_consumer_set_digest,
            observed_at=observed_at.isoformat(),
            expires_at=(
                observed_at
                + timedelta(
                    seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
                )
            ).isoformat(),
        )
        _SequencedClientFactory.calls = 0
        _SequencedClientFactory.outcomes = [
            MCPClientError("transient", retriable=True),
            _FakeHealthClient(),
        ]
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _SequencedClientFactory,
        ):
            recovered = asyncio.run(validator(request))
        self.assertTrue(recovered.healthy)
        self.assertEqual(recovered.attempts, 2)

        _SequencedClientFactory.calls = 0
        _SequencedClientFactory.outcomes = [MCPProtocolError("invalid catalog")]
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _SequencedClientFactory,
        ):
            rejected = asyncio.run(validator(request))
        self.assertFalse(rejected.healthy)
        self.assertEqual(rejected.attempts, 1)
        self.assertEqual(rejected.safe_error_code, "tool_discovery_invalid")

    def test_builtin_validator_bounds_blocking_client_cleanup(self) -> None:
        validator = BuiltInLegacyMigrationLiveHealthValidator(
            EndpointPolicy(resolver=_PublicResolver())
        )
        observed_at = datetime.now(timezone.utc)
        request = LegacyMigrationLiveHealthRequest(
            source_server_id="crm",
            source_fingerprint=self.plan.inventory[0].source_fingerprint,
            owner_user_id="service-owner",
            target_server_id=self.plan.mapping_candidates[0].target_server_id,
            normalized_endpoint="https://secret-host.example/rpc",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
            auth_type=UserMCPAuthType.NONE,
            auth_metadata={},
            credential_values=None,
            policy=replace(
                LEGACY_MIGRATION_HEALTH_POLICY,
                cleanup_timeout_seconds=0.001,
            ),
            capability_bindings=(
                (
                    "mcp.crm.lookup",
                    "lookup",
                    self.plan.consumer_capability_impact[0]
                    .obligations[0]
                    .source_contract_fingerprint,
                ),
            ),
            target_consumer_set_digest=self.plan.consumer_capability_impact[
                0
            ].target_consumer_set_digest,
            observed_at=observed_at.isoformat(),
            expires_at=(
                observed_at
                + timedelta(
                    seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
                )
            ).isoformat(),
        )
        _SequencedClientFactory.calls = 0
        _SequencedClientFactory.outcomes = [_BlockingCloseHealthClient()]
        with patch(
            "src.integrations.mcp.user_client.UserMCPClientFactory",
            _SequencedClientFactory,
        ):
            result = asyncio.run(
                asyncio.wait_for(validator(request), timeout=0.1)
            )
        self.assertTrue(result.healthy)

    def test_builtin_validator_rejects_same_name_with_incompatible_schemas(
        self,
    ) -> None:
        validator = BuiltInLegacyMigrationLiveHealthValidator(
            EndpointPolicy(resolver=_PublicResolver())
        )
        observed_at = datetime.now(timezone.utc)
        request = LegacyMigrationLiveHealthRequest(
            source_server_id="crm",
            source_fingerprint=self.plan.inventory[0].source_fingerprint,
            owner_user_id="service-owner",
            target_server_id=self.plan.mapping_candidates[0].target_server_id,
            normalized_endpoint="https://secret-host.example/rpc",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
            auth_type=UserMCPAuthType.NONE,
            auth_metadata={},
            credential_values=None,
            policy=LEGACY_MIGRATION_HEALTH_POLICY,
            capability_bindings=(
                (
                    "mcp.crm.lookup",
                    "lookup",
                    self.plan.consumer_capability_impact[0]
                    .obligations[0]
                    .source_contract_fingerprint,
                ),
            ),
            target_consumer_set_digest=self.plan.consumer_capability_impact[
                0
            ].target_consumer_set_digest,
            observed_at=observed_at.isoformat(),
            expires_at=(
                observed_at
                + timedelta(
                    seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
                )
            ).isoformat(),
        )
        hostile_catalogs = (
            [
                {
                    "name": "lookup",
                    "inputSchema": {"type": "string"},
                    "outputSchema": {"type": "object"},
                }
            ],
            [
                {
                    "name": "lookup",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "string"},
                }
            ],
        )
        for catalog in hostile_catalogs:
            with self.subTest(catalog=catalog), patch.object(
                _FakeHealthClient,
                "list_tools",
                return_value=catalog,
            ), patch(
                "src.integrations.mcp.user_client.UserMCPClientFactory",
                _FakeClientFactory,
            ):
                result = asyncio.run(validator(request))
            self.assertEqual(result.available_capability_contracts, ())
            blockers = legacy_migration_health_result_blockers(
                self.plan,
                result,
                now=observed_at,
            )
            self.assertIn("crm:health_capability_contract_mismatch", blockers)

    def test_outer_timeout_uses_derived_policy_deadline_without_waiting(self) -> None:
        applier = self._applier("secret-value")
        captured: list[float | None] = []

        async def immediate_timeout(_awaitable, *, timeout=None):
            captured.append(timeout)
            _awaitable.close()
            raise TimeoutError

        with patch(
            "src.integrations.mcp.legacy_migration_apply.asyncio.wait_for",
            immediate_timeout,
        ):
            with self.assertRaisesRegex(
                LegacyMigrationApplyError, "live_health_validation_timeout"
            ):
                applier({}, idempotency_key=self.plan.plan_fingerprint)
        self.assertEqual(
            captured,
            [LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds],
        )
        self.assertGreater(
            LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds,
            2 * 60 + 0.25,
        )

    def test_query_endpoint_is_rejected_without_persisting_secret(self) -> None:
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "https://secret-host.example/rpc?token=plaintext",
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    }
                ],
            }
        )
        plan = plan_legacy_mcp_config_migration(
            config,
            (
                LegacyServerClassification(
                    "crm",
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                ),
            ),
        )
        applier = self._applier_for(config, plan, self._healthy_validator)
        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "legacy_apply_endpoint_query_or_fragment_forbidden",
        ):
            applier({}, idempotency_key=plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

    def test_all_candidates_are_preflighted_before_any_write(self) -> None:
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "a",
                        "endpoint": "https://a.example/rpc",
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    },
                    {
                        "server_id": "b",
                        "endpoint": "https://b.example/rpc",
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    },
                ],
            }
        )
        plan = plan_legacy_mcp_config_migration(
            config,
            tuple(
                LegacyServerClassification(
                    server_id,
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                )
                for server_id in ("a", "b")
            ),
        )

        def fail_second(request: LegacyMigrationLiveHealthRequest):
            healthy = request.source_server_id == "a"
            if healthy:
                return self._healthy_validator(request)
            return LegacyMigrationHealthResult(
                request.source_server_id,
                1,
                False,
                False,
                False,
                False,
                "handshake_failed",
            )

        applier = self._applier_for(config, plan, fail_second)
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "live_health_validation_failed"
        ):
            applier({}, idempotency_key=plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

        all_healthy = self._applier_for(config, plan, self._healthy_validator)
        self.assertEqual(
            all_healthy({}, idempotency_key=plan.plan_fingerprint), "applied"
        )
        self.assertEqual(
            len(asyncio.run(self.storage.list_user_mcp_servers("service-owner"))), 2
        )
        self.assertEqual(
            all_healthy({}, idempotency_key=plan.plan_fingerprint),
            "already_applied",
        )

    def test_second_candidate_conflict_leaves_first_unwritten(self) -> None:
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "a",
                        "endpoint": "https://a.example/rpc",
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    },
                    {
                        "server_id": "b",
                        "endpoint": "https://b.example/rpc",
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    },
                ],
            }
        )
        plan = plan_legacy_mcp_config_migration(
            config,
            tuple(
                LegacyServerClassification(
                    server_id,
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                )
                for server_id in ("a", "b")
            ),
        )
        desired = self._applier_for(config, plan, self._healthy_validator)
        second = plan.mapping_candidates[1]
        source = desired._servers[second.source_server_id]
        conflict = desired._desired_server(
            source,
            second.target_server_id,
            source_fingerprint=second.source_fingerprint,
        )
        conflicting_server = replace(
            conflict.server,
            endpoint_url="https://different.example/rpc",
        )
        asyncio.run(
            self.storage.create_user_mcp_servers_atomic(((conflicting_server, None),))
        )

        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "legacy_apply_target_conflict"
        ):
            desired({}, idempotency_key=plan.plan_fingerprint)
        first_id = plan.mapping_candidates[0].target_server_id
        self.assertIsNone(
            asyncio.run(self.storage.get_user_mcp_server("service-owner", first_id))
        )

    def test_durable_audit_failure_rolls_back_server_and_credential(self) -> None:
        applier = self._applier("secret-value")
        with patch(
            "src.storage.sqlite.repositories.SQLiteStateRepository."
            "_persist_mcp_legacy_migration_records_atomic",
            side_effect=SQLAlchemyError("forced audit failure"),
        ):
            with self.assertRaisesRegex(
                LegacyMigrationApplyError, "legacy_apply_storage_failed"
            ):
                applier({}, idempotency_key=self.plan.plan_fingerprint)
        target_id = self.plan.mapping_candidates[0].target_server_id
        self.assertIsNone(
            asyncio.run(self.storage.get_user_mcp_server("service-owner", target_id))
        )
        with self.engine.connect() as connection:
            count = connection.scalar(
                text("SELECT COUNT(*) FROM mcp_legacy_migration_record")
            )
        self.assertEqual(count, 0)

    def test_durable_record_exact_replay_accepts_same_and_rejects_conflict(
        self,
    ) -> None:
        applier = self._applier("secret-value")
        self.assertEqual(
            applier({}, idempotency_key=self.plan.plan_fingerprint), "applied"
        )
        candidate = self.plan.mapping_candidates[0]
        server = asyncio.run(
            self.storage.get_user_mcp_server("service-owner", candidate.target_server_id)
        )
        credential = asyncio.run(
            self.storage.get_user_mcp_credential(
                "service-owner", candidate.target_server_id
            )
        )
        record = applier._migration_record(server, candidate)
        replay = asyncio.run(
            self.storage.apply_legacy_mcp_migration_atomic(
                ((server, credential, record),)
            )
        )
        self.assertFalse(replay.applied)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            asyncio.run(
                self.storage.apply_legacy_mcp_migration_atomic(
                    (
                        (
                            server,
                            credential,
                            replace(
                                record,
                                credential_digest=f"hmac-sha256:{'f' * 64}",
                            ),
                        ),
                    )
                )
            )

    def test_live_validation_expiry_writes_nothing(self) -> None:
        started = datetime(2026, 1, 1)
        times = iter(
            (
                started,
                started,
                started
                + timedelta(
                    seconds=(
                        LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds + 1
                    )
                ),
            )
        )
        applier = LocalLegacyMigrationApplier(
            storage=self.storage,
            credential_cipher=self.cipher,
            audit_reference_signer=self.audit_signer,
            endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
            config=self.config,
            plan=self.plan,
            service_account_owner="service-owner",
            environ={"CRM_SECRET": "secret-value"},
            live_health_validator=self._healthy_validator,
            validator_provenance="test-validator-v1",
            now_fn=lambda: next(times),
        )
        with self.assertRaisesRegex(
            LegacyMigrationApplyError, "live_health_validation_expired"
        ):
            applier({}, idempotency_key=self.plan.plan_fingerprint)
        self.assertEqual(
            asyncio.run(self.storage.list_user_mcp_servers("service-owner")), []
        )

    def test_existing_row_without_source_provenance_is_refused(self) -> None:
        applier = self._applier("secret-value")
        self.assertEqual(
            applier({}, idempotency_key=self.plan.plan_fingerprint), "applied"
        )
        target_id = self.plan.mapping_candidates[0].target_server_id
        existing = asyncio.run(
            self.storage.get_user_mcp_server("service-owner", target_id)
        )
        asyncio.run(
            self.storage.update_user_mcp_server(
                "service-owner",
                target_id,
                changes={"auth_metadata": {}},
                expected_config_version=existing.config_version,
                expected_security_version=existing.security_version,
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "legacy_apply_existing_target_provenance_unavailable",
        ):
            applier({}, idempotency_key=self.plan.plan_fingerprint)

    def _applier(
        self,
        secret: str,
        *,
        validator=None,
    ) -> LocalLegacyMigrationApplier:
        return LocalLegacyMigrationApplier(
            storage=self.storage,
            credential_cipher=self.cipher,
            audit_reference_signer=self.audit_signer,
            endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
            config=self.config,
            plan=self.plan,
            service_account_owner="service-owner",
            environ={"CRM_SECRET": secret},
            live_health_validator=validator or self._healthy_validator,
            validator_provenance="test-validator-v1",
        )

    def _applier_for(self, config, plan, validator):
        return LocalLegacyMigrationApplier(
            storage=self.storage,
            credential_cipher=self.cipher,
            audit_reference_signer=self.audit_signer,
            endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
            config=config,
            plan=plan,
            service_account_owner="service-owner",
            environ={"CRM_SECRET": "secret-value"},
            live_health_validator=validator,
            validator_provenance="test-validator-v1",
        )

    @staticmethod
    def _healthy_validator(
        request: LegacyMigrationLiveHealthRequest,
    ) -> LegacyMigrationHealthResult:
        catalog = [
            {
                "name": tool_name,
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            }
            for _capability_id, tool_name, _fingerprint in request.capability_bindings
        ]
        contracts = tuple(
            (capability_id, fingerprint)
            for capability_id, _tool_name, fingerprint in request.capability_bindings
        )
        capabilities = tuple(item[0] for item in contracts)
        return LegacyMigrationHealthResult(
            request.source_server_id,
            1,
            True,
            True,
            True,
            True,
            target_server_id=request.target_server_id,
            source_fingerprint=request.source_fingerprint,
            target_consumer_set_digest=request.target_consumer_set_digest,
            catalog_fingerprint=legacy_migration_catalog_fingerprint(catalog),
            capability_fingerprint=legacy_capability_contract_set_fingerprint(
                contracts
            ),
            available_capability_ids=capabilities,
            available_capability_contracts=contracts,
            observed_at=request.observed_at,
            expires_at=request.expires_at,
        )


if __name__ == "__main__":
    unittest.main()

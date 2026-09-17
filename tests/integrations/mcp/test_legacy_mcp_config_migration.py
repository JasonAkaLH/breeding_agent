from __future__ import annotations

import json
import os
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.legacy_migration import (
    LEGACY_MIGRATION_HEALTH_POLICY,
    LegacyConsumerScope,
    LegacyDisposition,
    LegacyMigrationHealthResult,
    LegacyMigrationValidationError,
    LegacyServerClassification,
    legacy_migration_source_fingerprint,
    plan_legacy_mcp_config_migration,
    validate_legacy_migration_apply,
)

SERVICE_CONSUMER_REF = f"hmac-sha256:{'1' * 64}"
SHARED_CONSUMER_REF = f"hmac-sha256:{'2' * 64}"


class LegacyMCPConfigMigrationTests(unittest.TestCase):
    def _config(self) -> MCPRuntimeConfig:
        return MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "https://secret-host.example/rpc?token=endpoint-secret",
                        "headers": {"X-Private": "header-secret"},
                        "auth": {
                            "type": "api_key_env",
                            "api_key_env": "SECRET_CREDENTIAL_ENV",
                            "header_name": "X-Api-Key",
                        },
                        "tools": [
                            {
                                "name": "lookup",
                                "expose": True,
                                "input_schema": {
                                    "secret-schema-key": {"type": "string"}
                                },
                            }
                        ],
                    },
                    {
                        "server_id": "shared",
                        "endpoint": "https://shared.example/rpc",
                        "tools": [
                            {
                                "name": "search",
                                "expose": True,
                                "input_schema": {"type": "object"},
                                "output_schema": {"type": "object"},
                            }
                        ],
                    },
                ],
            }
        )

    def test_every_legacy_server_requires_exactly_one_classification(self) -> None:
        with self.assertRaisesRegex(
            LegacyMigrationValidationError, "Missing legacy classifications: shared"
        ):
            plan_legacy_mcp_config_migration(
                self._config(),
                [
                    LegacyServerClassification(
                        "crm",
                        LegacyDisposition.MIGRATE_OWNER,
                        LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                        owner_user_id="service-owner",
                        target_consumer_refs=(SERVICE_CONSUMER_REF,),
                    )
                ],
            )

        duplicate = LegacyServerClassification(
            "crm",
            LegacyDisposition.MIGRATE_OWNER,
            LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
            owner_user_id="service-owner",
            target_consumer_refs=(SERVICE_CONSUMER_REF,),
        )
        with self.assertRaisesRegex(
            LegacyMigrationValidationError, "Duplicate legacy classification: crm"
        ):
            plan_legacy_mcp_config_migration(self._config(), [duplicate, duplicate])

    def test_migrate_owner_is_only_legal_for_an_explicit_service_account_owner(
        self,
    ) -> None:
        for invalid in (
            LegacyServerClassification(
                "crm",
                LegacyDisposition.MIGRATE_OWNER,
                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                target_consumer_refs=(SERVICE_CONSUMER_REF,),
            ),
            LegacyServerClassification(
                "crm",
                LegacyDisposition.MIGRATE_OWNER,
                LegacyConsumerScope.MULTI_USER,
                owner_user_id="alice",
                target_consumer_refs=(SERVICE_CONSUMER_REF, SHARED_CONSUMER_REF),
            ),
            LegacyServerClassification(
                "crm",
                LegacyDisposition.RETAIN_FOR_ROLLBACK,
                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                owner_user_id="alice",
                target_consumer_refs=(SERVICE_CONSUMER_REF,),
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(LegacyMigrationValidationError):
                    plan_legacy_mcp_config_migration(
                        self._config(),
                        [
                            invalid,
                            LegacyServerClassification(
                                "shared",
                                LegacyDisposition.RETAIN_FOR_ROLLBACK,
                                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                                target_consumer_refs=(SHARED_CONSUMER_REF,),
                            ),
                        ],
                    )

    def test_shared_or_unknown_consumer_blocks_assembly_off_until_approved_retirement(
        self,
    ) -> None:
        base = LegacyServerClassification(
            "crm",
            LegacyDisposition.MIGRATE_OWNER,
            LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
            owner_user_id="service-owner",
            target_consumer_refs=(SERVICE_CONSUMER_REF,),
        )
        blocked = plan_legacy_mcp_config_migration(
            self._config(),
            [
                base,
                LegacyServerClassification(
                    "shared",
                    LegacyDisposition.RETIRE,
                    LegacyConsumerScope.MULTI_USER,
                    target_consumer_refs=(
                        SERVICE_CONSUMER_REF,
                        SHARED_CONSUMER_REF,
                    ),
                ),
            ],
        )
        self.assertFalse(blocked.assembly_off_allowed)
        self.assertEqual(
            blocked.assembly_off_blockers, ("shared:retirement_approval_incomplete",)
        )

        allowed = plan_legacy_mcp_config_migration(
            self._config(),
            [
                base,
                LegacyServerClassification(
                    "shared",
                    LegacyDisposition.RETIRE,
                    LegacyConsumerScope.UNKNOWN,
                    retirement_approver="platform-owner",
                    retirement_reason="No supported consumers remain",
                    impact_accepted=True,
                    target_consumer_refs=(SHARED_CONSUMER_REF,),
                ),
            ],
        )
        self.assertTrue(allowed.assembly_off_allowed)

    def test_plan_is_idempotent_and_does_not_expose_source_secrets(self) -> None:
        classifications = [
            LegacyServerClassification(
                "shared",
                LegacyDisposition.RETAIN_FOR_ROLLBACK,
                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                target_consumer_refs=(SHARED_CONSUMER_REF,),
            ),
            LegacyServerClassification(
                "crm",
                LegacyDisposition.MIGRATE_OWNER,
                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                owner_user_id="service-owner",
                target_consumer_refs=(SERVICE_CONSUMER_REF,),
            ),
        ]
        first = plan_legacy_mcp_config_migration(self._config(), classifications)
        second = plan_legacy_mcp_config_migration(
            self._config(), reversed(classifications)
        )
        self.assertEqual(first, second)
        serialized = json.dumps(asdict(first), sort_keys=True)
        for secret in (
            "secret-host",
            "endpoint-secret",
            "X-Private",
            "header-secret",
            "SECRET_CREDENTIAL_ENV",
            "X-Api-Key",
            "secret-schema-key",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("service-owner", serialized)
        self.assertRegex(
            first.inventory[0].source_fingerprint, r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(first.mapping_candidates[0].source_server_id, "crm")
        self.assertEqual(
            first.consumer_capability_impact[0].exposed_capability_ids,
            ("mcp.crm.lookup",),
        )

    def test_source_fingerprint_omits_header_values_and_resolved_auth_values(
        self,
    ) -> None:
        source = self._config().servers[0]
        baseline = legacy_migration_source_fingerprint(source)

        changed_value = replace(
            source,
            request_headers={"x-private": "different-secret"},
        )
        changed_name = replace(
            source,
            request_headers={"X-Other": "header-secret"},
        )
        self.assertEqual(
            legacy_migration_source_fingerprint(changed_value),
            baseline,
        )
        self.assertNotEqual(
            legacy_migration_source_fingerprint(changed_name),
            baseline,
        )

        with patch.dict(os.environ, {"SECRET_CREDENTIAL_ENV": "first"}, clear=False):
            first = MCPRuntimeConfig.from_mapping(
                {
                    "enabled": True,
                    "servers": [
                        {
                            "server_id": "auth-only",
                            "endpoint": "https://auth.example/rpc",
                            "auth": {
                                "type": "api_key_env",
                                "api_key_env": "SECRET_CREDENTIAL_ENV",
                                "header_name": "X-Api-Key",
                            },
                        }
                    ],
                }
            ).servers[0]
        with patch.dict(os.environ, {"SECRET_CREDENTIAL_ENV": "second"}, clear=False):
            second = MCPRuntimeConfig.from_mapping(
                {
                    "enabled": True,
                    "servers": [
                        {
                            "server_id": "auth-only",
                            "endpoint": "https://auth.example/rpc",
                            "auth": {
                                "type": "api_key_env",
                                "api_key_env": "SECRET_CREDENTIAL_ENV",
                                "header_name": "x-api-key",
                            },
                        }
                    ],
                }
            ).servers[0]
        self.assertEqual(
            legacy_migration_source_fingerprint(first),
            legacy_migration_source_fingerprint(second),
        )

    def test_apply_validation_requires_complete_health_contract(self) -> None:
        plan = plan_legacy_mcp_config_migration(
            MCPRuntimeConfig.from_mapping(
                {
                    "enabled": True,
                    "servers": [
                        {
                            "server_id": "crm",
                            "endpoint": "https://x.test",
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
            ),
            [
                LegacyServerClassification(
                    "crm",
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                )
            ],
        )
        checked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candidate = plan.mapping_candidates[0]
        impact = plan.consumer_capability_impact[0]

        def health_result(
            *,
            legal_tool: bool = True,
            capability_ids: tuple[str, ...] = ("mcp.crm.lookup",),
            observed_at: datetime = checked_at,
            expires_at: datetime = checked_at
            + timedelta(
                seconds=LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds
            ),
        ) -> LegacyMigrationHealthResult:
            from src.integrations.mcp.legacy_migration import (
                legacy_capability_contract_set_fingerprint,
            )

            expected_by_id = {
                obligation.capability_id: obligation.source_contract_fingerprint
                for obligation in impact.obligations
            }
            contracts = tuple(
                (capability_id, expected_by_id[capability_id])
                for capability_id in capability_ids
            )

            return LegacyMigrationHealthResult(
                "crm",
                2,
                True,
                True,
                True,
                legal_tool,
                target_server_id=candidate.target_server_id,
                source_fingerprint=candidate.source_fingerprint,
                target_consumer_set_digest=impact.target_consumer_set_digest,
                catalog_fingerprint=f"sha256:{'3' * 64}",
                capability_fingerprint=legacy_capability_contract_set_fingerprint(
                    contracts
                ),
                available_capability_ids=capability_ids,
                available_capability_contracts=contracts,
                observed_at=observed_at.isoformat(),
                expires_at=expires_at.isoformat(),
            )

        self.assertEqual(LEGACY_MIGRATION_HEALTH_POLICY.max_attempts, 2)
        self.assertEqual(LEGACY_MIGRATION_HEALTH_POLICY.timeout_seconds_per_attempt, 60)
        self.assertEqual(LEGACY_MIGRATION_HEALTH_POLICY.retry_delay_seconds, 0.25)
        self.assertEqual(LEGACY_MIGRATION_HEALTH_POLICY.cleanup_timeout_seconds, 1.0)
        self.assertEqual(
            LEGACY_MIGRATION_HEALTH_POLICY.total_timeout_seconds,
            122.25,
        )
        failed = validate_legacy_migration_apply(
            plan,
            [health_result(legal_tool=False)],
            now=checked_at,
        )
        self.assertFalse(failed.ready)
        passed = validate_legacy_migration_apply(
            plan,
            [health_result()],
            now=checked_at,
        )
        self.assertTrue(passed.ready)

        missing_capability = validate_legacy_migration_apply(
            plan,
            [health_result(capability_ids=())],
            now=checked_at,
        )
        self.assertIn(
            "crm:health_capability_obligation_mismatch",
            missing_capability.blockers,
        )
        stale = validate_legacy_migration_apply(
            plan,
            [
                health_result(
                    observed_at=checked_at - timedelta(minutes=3),
                    expires_at=checked_at - timedelta(minutes=1),
                )
            ],
            now=checked_at,
        )
        self.assertIn("crm:health_evidence_stale", stale.blockers)
        with self.assertRaisesRegex(
            LegacyMigrationValidationError,
            "Duplicate health result",
        ):
            validate_legacy_migration_apply(
                plan,
                [health_result(), health_result()],
                now=checked_at,
            )

    def test_raw_missing_and_duplicate_target_consumers_fail_closed(self) -> None:
        for refs in ((), ("alice",), (SERVICE_CONSUMER_REF, SERVICE_CONSUMER_REF)):
            with self.subTest(refs=refs):
                with self.assertRaises(LegacyMigrationValidationError):
                    plan_legacy_mcp_config_migration(
                        MCPRuntimeConfig.from_mapping(
                            {
                                "enabled": True,
                                "servers": [
                                    {
                                        "server_id": "crm",
                                        "endpoint": "https://x.test",
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
                        ),
                        [
                            LegacyServerClassification(
                                "crm",
                                LegacyDisposition.MIGRATE_OWNER,
                                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                                owner_user_id="service-owner",
                                target_consumer_refs=refs,
                            )
                        ],
                    )

    def test_migrate_owner_without_source_input_schema_is_blocked(self) -> None:
        plan = plan_legacy_mcp_config_migration(
            MCPRuntimeConfig.from_mapping(
                {
                    "enabled": True,
                    "servers": [
                        {
                            "server_id": "crm",
                            "endpoint": "https://x.test",
                            "tools": [{"name": "lookup", "expose": True}],
                        }
                    ],
                }
            ),
            [
                LegacyServerClassification(
                    "crm",
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id="service-owner",
                    target_consumer_refs=(SERVICE_CONSUMER_REF,),
                )
            ],
        )
        self.assertFalse(plan.assembly_off_allowed)
        self.assertIn(
            "crm:capability_contract_missing:mcp.crm.lookup",
            plan.assembly_off_blockers,
        )

    def test_secret_like_source_and_capability_identifiers_fail_before_plan(self) -> None:
        for server_id, capability_id in (
            ("https://private.example/owner/alice", ""),
            ("crm", "mcp.crm.https://private.example/token"),
        ):
            with self.subTest(server_id=server_id, capability_id=capability_id):
                config = MCPRuntimeConfig.from_mapping(
                    {
                        "enabled": True,
                        "servers": [
                            {
                                "server_id": server_id,
                                "endpoint": "https://x.test",
                                "tools": [
                                    {
                                        "name": "lookup",
                                        "expose": True,
                                        "capability_id": capability_id,
                                        "input_schema": {"type": "object"},
                                    }
                                ],
                            }
                        ],
                    }
                )
                with self.assertRaises(LegacyMigrationValidationError):
                    plan_legacy_mcp_config_migration(
                        config,
                        [
                            LegacyServerClassification(
                                server_id,
                                LegacyDisposition.MIGRATE_OWNER,
                                LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                                owner_user_id="service-owner",
                                target_consumer_refs=(SERVICE_CONSUMER_REF,),
                            )
                        ],
                    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    MCPLegacyMigrationRecord,
    UserMCPCredentialRecord,
    UserMCPServer,
)
from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.credentials import CredentialCipher
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.legacy_migration import (
    LegacyConsumerScope,
    LegacyDisposition,
    LegacyMigrationHealthResult,
    LegacyServerClassification,
    legacy_capability_contract_set_fingerprint,
    legacy_migration_catalog_fingerprint,
    legacy_migration_record_id,
    legacy_target_consumer_reference,
    plan_legacy_mcp_config_migration,
)
from src.integrations.mcp.legacy_migration_apply import (
    LegacyMigrationApplyError,
    LegacyMigrationLiveHealthRequest,
    LocalLegacyMigrationApplier,
)
from src.storage.postgres import (
    PostgreSQLStorage,
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.session import (
    validate_mcp_legacy_migration_connection_role,
)


INTEGRATION_DSN_ENV = "MAF_POSTGRES_ROLLOUT_INTEGRATION_TEST_DSN"
PERMISSIONS_SQL = (
    Path(__file__).resolve().parents[2]
    / "scripts/postgres/user_mcp_legacy_migration_permissions.sql"
).read_text(encoding="utf-8")
ROLE_PASSWORD = f"legacy-migration-{uuid4().hex}"


def _credential_storage_digest(
    ciphertext: bytes,
    nonce: bytes,
    encryption_version: int,
) -> str:
    material = (
        "legacy_mcp_credential_storage.v1:"
        f"{ciphertext.hex()}:{nonce.hex()}:{encryption_version}"
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class _PublicResolver:
    def resolve(self, _hostname: str, _port: int) -> tuple[str, ...]:
        return ("8.8.8.8",)


class LegacyMCPMigrationPostgresIntegrationTest(unittest.TestCase):
    """Real PostgreSQL coverage for the CP-4 migration transaction boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get(INTEGRATION_DSN_ENV, "")
        if not cls.dsn:
            raise unittest.SkipTest(
                "postgres_rollout_integration_test_dsn_not_configured"
            )
        sqlalchemy_dsn = (
            make_url(cls.dsn)
            .set(drivername="postgresql+psycopg")
            .render_as_string(hide_password=False)
        )
        cls.engine = create_postgres_engine(sqlalchemy_dsn, pool_size=1, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.login_name = f"legacy_migrator_{uuid4().hex[:12]}"
        with cls.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(PERMISSIONS_SQL)
            connection.exec_driver_sql(
                f"CREATE ROLE \"{cls.login_name}\" LOGIN PASSWORD '{ROLE_PASSWORD}'"
            )
            connection.exec_driver_sql(
                f'GRANT maf_mcp_legacy_migrator TO "{cls.login_name}" '
                "WITH INHERIT TRUE, SET FALSE"
            )
        login_dsn = (
            make_url(cls.dsn)
            .set(
                drivername="postgresql+psycopg",
                username=cls.login_name,
                password=ROLE_PASSWORD,
            )
            .render_as_string(hide_password=False)
        )
        cls.migration_engine = create_postgres_engine(
            login_dsn, pool_size=4, max_overflow=0
        )
        validate_mcp_legacy_migration_connection_role(
            cls.migration_engine,
            cls.login_name,
        )
        migration_factory = create_postgres_session_factory(cls.migration_engine)
        cls.storage = PostgreSQLStorage(
            migration_factory,
            mcp_legacy_migration_session_factory=migration_factory,
            mcp_legacy_migration_role=cls.login_name,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.migration_engine.dispose()
        with cls.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{cls.login_name}"')
        cls.engine.dispose()

    def setUp(self) -> None:
        self.scope_id = uuid4().hex
        self.owner_user_id = f"legacy-migration-owner-{self.scope_id}"
        self.server_id = f"legacy-migration-server-{self.scope_id}"
        self.source_server_id = f"legacy-source-{self.scope_id}"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        credential_ciphertext = f"cipher-{self.scope_id}".encode()
        credential_nonce = f"nonce-{self.scope_id}".encode()
        credential_storage_digest = _credential_storage_digest(
            credential_ciphertext,
            credential_nonce,
            1,
        )
        self.server = UserMCPServer(
            server_id=self.server_id,
            owner_user_id=self.owner_user_id,
            display_name=f"legacy migration {self.scope_id}",
            routing_description="isolated PostgreSQL migration test",
            endpoint_url=f"https://{self.scope_id}.example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            auth_metadata={
                "migration_provenance": {
                    "credential_storage_digest": credential_storage_digest,
                }
            },
            health_status=UserMCPHealthStatus.AVAILABLE,
            credential_configured=True,
            last_tested_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )
        self.credential = UserMCPCredentialRecord(
            owner_user_id=self.owner_user_id,
            server_id=self.server_id,
            credential_ciphertext=credential_ciphertext,
            credential_nonce=credential_nonce,
            encryption_version=1,
            credential_updated_at=self.now,
        )
        self.record = MCPLegacyMigrationRecord(
            migration_id=self._sha("migration"),
            event_type="mcp.legacy.config_migrated",
            plan_fingerprint=self._sha("plan"),
            source_server_id=self.source_server_id,
            source_fingerprint=self._sha("source"),
            owner_consumer_ref=self._hmac("owner"),
            target_server_id=self.server_id,
            target_consumer_set_digest=self._sha("consumers"),
            capability_obligations_fingerprint=self._sha("obligations"),
            catalog_fingerprint=self._sha("catalog"),
            capability_fingerprint=self._sha("capability"),
            validator_provenance_fingerprint=self._sha("validator"),
            credential_digest=self._hmac("credential"),
            disposition="migrate_owner",
            occurred_at=self.now,
            evidence_expires_at=self.now + timedelta(days=1),
        )
        self.cleanup_identities = [(self.server_id, self.record.migration_id)]

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            for server_id, migration_id in self.cleanup_identities:
                connection.execute(
                    text(
                        "DELETE FROM mcp_legacy_migration_record "
                        "WHERE migration_id = :migration_id "
                        "OR target_server_id = :server_id"
                    ),
                    {"migration_id": migration_id, "server_id": server_id},
                )
                connection.execute(
                    text("DELETE FROM user_mcp_server WHERE server_id = :server_id"),
                    {"server_id": server_id},
                )

    def test_saves_server_credential_and_record_in_one_transaction(self) -> None:
        result = self._apply()

        self.assertTrue(result.applied)
        self.assertEqual(self._row_counts(), (1, 1, 1))

    def test_second_candidate_failure_rolls_back_whole_api_batch(self) -> None:
        second_server = replace(
            self.server,
            server_id=f"{self.server_id}-expired",
        )
        second_credential = replace(
            self.credential,
            server_id=second_server.server_id,
        )
        second_record = replace(
            self.record,
            migration_id=self._sha("expired-migration"),
            source_server_id=f"{self.source_server_id}-expired",
            target_server_id=second_server.server_id,
            evidence_expires_at=self.now - timedelta(seconds=1),
            occurred_at=self.now - timedelta(seconds=2),
        )

        with self.assertRaisesRegex(DBAPIError, "evidence is expired"):
            asyncio.run(
                self.storage.apply_legacy_mcp_migration_atomic(
                    (
                        (self.server, self.credential, self.record),
                        (second_server, second_credential, second_record),
                    )
                )
            )

        self.assertEqual(self._row_counts(), (0, 0, 0))

    def test_independent_login_cannot_mutate_base_tables_directly(self) -> None:
        statements = (
            "SELECT credential_ciphertext FROM public.user_mcp_server",
            "SELECT credential_digest FROM public.mcp_legacy_migration_record",
            "INSERT INTO public.user_mcp_server DEFAULT VALUES",
            "UPDATE public.user_mcp_server SET display_name = display_name",
            "DELETE FROM public.user_mcp_server",
            "INSERT INTO public.mcp_legacy_migration_record DEFAULT VALUES",
            "UPDATE public.mcp_legacy_migration_record SET disposition = disposition",
            "DELETE FROM public.mcp_legacy_migration_record",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(DBAPIError, "permission denied"):
                    with self.migration_engine.begin() as connection:
                        connection.exec_driver_sql(statement)

    def test_unrelated_credential_ciphertext_cannot_be_read(self) -> None:
        self._apply()

        with self.assertRaisesRegex(DBAPIError, "permission denied"):
            with self.migration_engine.begin() as connection:
                connection.execute(
                    text(
                        "SELECT credential_ciphertext FROM public.user_mcp_server "
                        "WHERE server_id = :server_id"
                    ),
                    {"server_id": self.server_id},
                ).scalar_one()

    def test_rogue_login_with_schema_and_function_grants_is_rejected(self) -> None:
        rogue_name = f"legacy_rogue_{uuid4().hex[:12]}"
        rogue_password = f"legacy-rogue-{uuid4().hex}"
        rogue_engine = None
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                f"CREATE ROLE \"{rogue_name}\" LOGIN PASSWORD '{rogue_password}'"
            )
            connection.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA mcp_migration_api TO "{rogue_name}"'
            )
            connection.exec_driver_sql(
                "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mcp_migration_api "
                f'TO "{rogue_name}"'
            )
        try:
            rogue_dsn = (
                make_url(self.dsn)
                .set(
                    drivername="postgresql+psycopg",
                    username=rogue_name,
                    password=rogue_password,
                )
                .render_as_string(hide_password=False)
            )
            rogue_engine = create_postgres_engine(
                rogue_dsn, pool_size=1, max_overflow=0
            )
            with self.assertRaises(RuntimeError):
                validate_mcp_legacy_migration_connection_role(
                    rogue_engine,
                    rogue_name,
                )
        finally:
            if rogue_engine is not None:
                rogue_engine.dispose()
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(
                    "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA mcp_migration_api "
                    f'FROM "{rogue_name}"'
                )
                connection.exec_driver_sql(
                    f'REVOKE USAGE ON SCHEMA mcp_migration_api FROM "{rogue_name}"'
                )
                connection.exec_driver_sql(f'DROP ROLE "{rogue_name}"')

    def test_validator_rejects_rogue_global_acl_grantees(self) -> None:
        rogue_name = f"legacy_acl_rogue_{uuid4().hex[:12]}"
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(f'CREATE ROLE "{rogue_name}" NOLOGIN')
            connection.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA mcp_migration_api TO "{rogue_name}"'
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "schema ACL"):
                validate_mcp_legacy_migration_connection_role(
                    self.migration_engine,
                    self.login_name,
                )
        finally:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(
                    f'REVOKE USAGE ON SCHEMA mcp_migration_api '
                    f'FROM "{rogue_name}"'
                )

        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                "GRANT EXECUTE ON FUNCTION "
                "mcp_migration_api.lock_legacy_migration_batch(text[]) "
                f'TO "{rogue_name}"'
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "function ACL"):
                validate_mcp_legacy_migration_connection_role(
                    self.migration_engine,
                    self.login_name,
                )
        finally:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(
                    "REVOKE EXECUTE ON FUNCTION "
                    "mcp_migration_api.lock_legacy_migration_batch(text[]) "
                    f'FROM "{rogue_name}"'
                )
                connection.exec_driver_sql(f'DROP ROLE "{rogue_name}"')

    def test_validator_rejects_unlisted_function_overload(self) -> None:
        overload = (
            "mcp_migration_api.read_legacy_migration_replay_snapshot(text)"
        )
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                "CREATE FUNCTION " + overload + " RETURNS jsonb "
                "LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog "
                "AS 'SELECT NULL::jsonb'"
            )
            connection.exec_driver_sql(
                "ALTER FUNCTION " + overload
                + " OWNER TO maf_mcp_migration_api_owner"
            )
            connection.exec_driver_sql(
                "REVOKE ALL ON FUNCTION " + overload + " FROM PUBLIC"
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "function contract"):
                validate_mcp_legacy_migration_connection_role(
                    self.migration_engine,
                    self.login_name,
                )
        finally:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql("DROP FUNCTION " + overload)

    def test_api_owner_is_nonlogin_nonprivileged_and_has_no_membership(self) -> None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT role.rolcanlogin, role.rolinherit, role.rolsuper,
                        role.rolbypassrls,
                        role.rolcreatedb, role.rolcreaterole, role.rolreplication,
                        EXISTS (
                            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                            WHERE membership.member = role.oid
                        ) AS has_membership
                    FROM pg_catalog.pg_roles AS role
                    WHERE role.rolname = 'maf_mcp_migration_api_owner'
                    """
                )
            ).one()
        self.assertFalse(any(bool(value) for value in row))

    def test_append_only_trigger_is_exact_enabled_and_blocks_admin_mutation(
        self,
    ) -> None:
        self._apply()
        with self.engine.connect() as connection:
            trigger = connection.execute(
                text(
                    """
                    SELECT trigger.tgenabled, trigger.tgtype,
                        namespace.nspname, procedure.proname
                    FROM pg_catalog.pg_trigger AS trigger
                    JOIN pg_catalog.pg_proc AS procedure
                      ON procedure.oid = trigger.tgfoid
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    WHERE trigger.tgrelid =
                        'public.mcp_legacy_migration_record'::regclass
                      AND trigger.tgname =
                        'mcp_legacy_migration_record_append_only'
                      AND NOT trigger.tgisinternal
                    """
                )
            ).one()
        self.assertEqual(
            tuple(trigger),
            ("O", 27, "mcp_migration_api", "reject_legacy_migration_mutation"),
        )

        for statement in (
            "UPDATE public.mcp_legacy_migration_record SET disposition = disposition",
            "DELETE FROM public.mcp_legacy_migration_record",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(DBAPIError, "append-only"):
                    with self.engine.begin() as connection:
                        connection.exec_driver_sql(statement)

    def test_validator_rejects_disabled_or_missing_append_only_trigger(self) -> None:
        trigger_name = "mcp_legacy_migration_record_append_only"
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE public.mcp_legacy_migration_record "
                f"DISABLE TRIGGER {trigger_name}"
            )
        try:
            with self.assertRaises(RuntimeError):
                validate_mcp_legacy_migration_connection_role(
                    self.migration_engine,
                    self.login_name,
                )
        finally:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE public.mcp_legacy_migration_record "
                    f"ENABLE TRIGGER {trigger_name}"
                )

        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql(
                f"DROP TRIGGER {trigger_name} ON public.mcp_legacy_migration_record"
            )
        try:
            with self.assertRaises(RuntimeError):
                validate_mcp_legacy_migration_connection_role(
                    self.migration_engine,
                    self.login_name,
                )
        finally:
            with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql(PERMISSIONS_SQL)

    def test_exact_replay_is_idempotent(self) -> None:
        self._apply()

        replay = self._apply()

        self.assertFalse(replay.applied)
        self.assertEqual(self._row_counts(), (1, 1, 1))

    def test_local_applier_uses_nonsecret_replay_api_without_base_select(
        self,
    ) -> None:
        cipher = CredentialCipher(b"a" * 32)
        source_server_id = f"crm-{self.scope_id}"
        owner_user_id = f"service-owner-{self.scope_id}"
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": source_server_id,
                        "endpoint": f"https://{self.scope_id}.example.test/mcp",
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
        owner_ref = legacy_target_consumer_reference(cipher, owner_user_id)
        plan = plan_legacy_mcp_config_migration(
            config,
            (
                LegacyServerClassification(
                    source_server_id,
                    LegacyDisposition.MIGRATE_OWNER,
                    LegacyConsumerScope.SERVICE_ACCOUNT_ONLY,
                    owner_user_id=owner_user_id,
                    target_consumer_refs=(owner_ref,),
                ),
            ),
        )
        target_server_id = plan.mapping_candidates[0].target_server_id
        migration_id = legacy_migration_record_id(
            plan_fingerprint=plan.plan_fingerprint,
            source_server_id=source_server_id,
            target_server_id=target_server_id,
        )
        self.cleanup_identities.append(
            (target_server_id, migration_id)
        )
        health_calls = 0
        catalog_suffix = ""

        def healthy(
            request: LegacyMigrationLiveHealthRequest,
        ) -> LegacyMigrationHealthResult:
            nonlocal health_calls
            health_calls += 1
            catalog = [
                {
                    "name": f"{tool_name}{catalog_suffix}",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                }
                for _capability_id, tool_name, _fingerprint
                in request.capability_bindings
            ]
            contracts = tuple(
                (capability_id, fingerprint)
                for capability_id, _tool_name, fingerprint
                in request.capability_bindings
            )
            return LegacyMigrationHealthResult(
                server_id=request.source_server_id,
                attempts=1,
                handshake_ok=True,
                discovery_ok=True,
                full_paginated_tool_list_ok=True,
                nonempty_legal_tool_ok=True,
                target_server_id=request.target_server_id,
                source_fingerprint=request.source_fingerprint,
                target_consumer_set_digest=request.target_consumer_set_digest,
                catalog_fingerprint=legacy_migration_catalog_fingerprint(catalog),
                capability_fingerprint=(
                    legacy_capability_contract_set_fingerprint(contracts)
                ),
                available_capability_ids=tuple(item[0] for item in contracts),
                available_capability_contracts=contracts,
                observed_at=request.observed_at,
                expires_at=request.expires_at,
            )

        def applier(secret: str) -> LocalLegacyMigrationApplier:
            return LocalLegacyMigrationApplier(
                storage=self.storage,
                credential_cipher=cipher,
                endpoint_policy=EndpointPolicy(resolver=_PublicResolver()),
                config=config,
                plan=plan,
                service_account_owner=owner_user_id,
                environ={"CRM_SECRET": secret},
                live_health_validator=healthy,
                validator_provenance="postgres-integration-validator-v1",
            )

        self.assertEqual(
            applier("secret-value")({}, idempotency_key=plan.plan_fingerprint),
            "applied",
        )
        self.assertEqual(health_calls, 1)
        self.assertEqual(
            applier("secret-value")({}, idempotency_key=plan.plan_fingerprint),
            "already_applied",
        )
        self.assertEqual(health_calls, 2)
        catalog_suffix = "-drift"
        self.assertEqual(
            applier("secret-value")({}, idempotency_key=plan.plan_fingerprint),
            "already_applied",
        )
        self.assertEqual(health_calls, 3)
        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "legacy_apply_target_conflict",
        ):
            applier("different-secret")(
                {}, idempotency_key=plan.plan_fingerprint
            )
        self.assertEqual(health_calls, 3)

        current = asyncio.run(
            self.storage.get_legacy_mcp_migration_replay_snapshot(
                migration_id=migration_id,
                plan_fingerprint=plan.plan_fingerprint,
                source_server_id=source_server_id,
                source_fingerprint=plan.mapping_candidates[0].source_fingerprint,
                owner_consumer_ref=plan.mapping_candidates[0].owner_consumer_ref,
                target_server_id=target_server_id,
            )
        )
        self.assertIsNotNone(current)
        assert current is not None
        snapshot_server = current["server"]
        self.assertNotIn("credential_ciphertext", snapshot_server)
        self.assertNotIn("credential_nonce", snapshot_server)
        self.assertRegex(
            snapshot_server["credential_storage_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )
        rotated = cipher.encrypt(
            owner_user_id=owner_user_id,
            server_id=target_server_id,
            auth_type="bearer",
            values={"token": "rotated-secret"},
        )
        admin_storage = PostgreSQLStorage(
            create_postgres_session_factory(self.engine)
        )
        updated = asyncio.run(
            admin_storage.update_user_mcp_server(
                owner_user_id,
                target_server_id,
                changes={},
                credential_operation="replace",
                credential=UserMCPCredentialRecord(
                    owner_user_id=owner_user_id,
                    server_id=target_server_id,
                    credential_ciphertext=rotated.ciphertext,
                    credential_nonce=rotated.nonce,
                    encryption_version=rotated.encryption_version,
                    credential_updated_at=self.now + timedelta(seconds=1),
                ),
                security_sensitive=True,
                updated_at=self.now + timedelta(seconds=1),
            )
        )
        self.assertIsNotNone(updated)
        with self.assertRaisesRegex(
            LegacyMigrationApplyError,
            "legacy_apply_target_conflict",
        ):
            applier("secret-value")(
                {}, idempotency_key=plan.plan_fingerprint
            )
        self.assertEqual(health_calls, 3)

    def test_conflicting_replay_is_rejected(self) -> None:
        self._apply()
        conflicting_record = replace(
            self.record,
            credential_digest=self._hmac("different-credential"),
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            asyncio.run(
                self.storage.apply_legacy_mcp_migration_atomic(
                    ((self.server, self.credential, conflicting_record),)
                )
            )

        self.assertEqual(self._row_counts(), (1, 1, 1))
        with self.engine.connect() as connection:
            stored_digest = connection.execute(
                text(
                    "SELECT credential_digest FROM mcp_legacy_migration_record "
                    "WHERE migration_id = :migration_id"
                ),
                {"migration_id": self.record.migration_id},
            ).scalar_one()
        self.assertEqual(stored_digest, self.record.credential_digest)

    def test_opposite_order_concurrent_batches_do_not_deadlock(self) -> None:
        first = (self.server, self.credential, self.record)
        second = self._candidate("opposite-order")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(self._apply_candidates, (first, second)),
                executor.submit(self._apply_candidates, (second, first)),
            )
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(sorted(result.applied for result in results), [False, True])
        self.assertEqual(self._row_counts(), (1, 1, 1))
        self.assertEqual(self._row_counts_for(second[0], second[2]), (1, 1, 1))

    def test_concurrent_conflict_fails_closed(self) -> None:
        exact = (self.server, self.credential, self.record)
        conflicting = (
            replace(
                self.server,
                display_name=f"conflict {self.scope_id}",
                auth_metadata={
                    "migration_provenance": {
                        "credential_storage_digest": _credential_storage_digest(
                            f"different-{self.scope_id}".encode(),
                            self.credential.credential_nonce,
                            self.credential.encryption_version,
                        )
                    }
                },
            ),
            replace(
                self.credential,
                credential_ciphertext=f"different-{self.scope_id}".encode(),
            ),
            replace(self.record, credential_digest=self._hmac("conflict")),
        )

        outcomes: list[object] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(self._apply_candidates, (exact,)),
                executor.submit(self._apply_candidates, (conflicting,)),
            )
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except ValueError as exc:
                    outcomes.append(exc)

        self.assertEqual(sum(not isinstance(value, Exception) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, ValueError) for value in outcomes), 1)
        self.assertEqual(self._row_counts(), (1, 1, 1))

    def _apply(self):
        return asyncio.run(
            self.storage.apply_legacy_mcp_migration_atomic(
                ((self.server, self.credential, self.record),)
            )
        )

    def _apply_candidates(self, candidates):
        return asyncio.run(self.storage.apply_legacy_mcp_migration_atomic(candidates))

    def _candidate(self, label: str):
        server_id = f"{self.server_id}-{label}"
        server = replace(
            self.server,
            server_id=server_id,
            display_name=f"legacy migration {label} {self.scope_id}",
        )
        credential = replace(self.credential, server_id=server_id)
        record = replace(
            self.record,
            migration_id=self._sha(f"migration-{label}"),
            source_server_id=f"{self.source_server_id}-{label}",
            target_server_id=server_id,
        )
        self.cleanup_identities.append((server_id, record.migration_id))
        return server, credential, record

    def _row_counts(self) -> tuple[int, int, int]:
        return self._row_counts_for(self.server, self.record)

    def _row_counts_for(
        self,
        server: UserMCPServer,
        record: MCPLegacyMigrationRecord,
    ) -> tuple[int, int, int]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT
                        count(*) FILTER (WHERE server_id = :server_id) AS servers,
                        count(*) FILTER (
                            WHERE server_id = :server_id
                              AND credential_ciphertext IS NOT NULL
                              AND credential_nonce IS NOT NULL
                              AND encryption_version IS NOT NULL
                              AND credential_updated_at IS NOT NULL
                        ) AS credentials,
                        (
                            SELECT count(*)
                            FROM mcp_legacy_migration_record
                            WHERE migration_id = :migration_id
                              AND target_server_id = :server_id
                        ) AS records
                    FROM user_mcp_server
                    WHERE owner_user_id = :owner_user_id
                    """
                ),
                {
                    "migration_id": record.migration_id,
                    "owner_user_id": server.owner_user_id,
                    "server_id": server.server_id,
                },
            ).one()
        return tuple(row)

    def _sha(self, label: str) -> str:
        value = hashlib.sha256(f"{label}:{self.scope_id}".encode()).hexdigest()
        return f"sha256:{value}"

    def _hmac(self, label: str) -> str:
        value = hashlib.sha256(f"hmac:{label}:{self.scope_id}".encode()).hexdigest()
        return f"hmac-sha256:{value}"


if __name__ == "__main__":
    unittest.main()

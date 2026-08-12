from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import unittest
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from unittest.mock import Mock, patch

from sqlalchemy import text

from scripts.control_user_mcp_rollout import (
    RolloutControlError,
    activate_approval,
    append_approval,
    append_block,
    resolve_block,
    rollback_activation,
)
from scripts import control_user_mcp_rollout as control
from src.core.models import MCPRolloutEvidenceSnapshot
from src.integrations.mcp.rollout import MCPRolloutConfig
from src.integrations.mcp.observability import (
    mcp_evidence_snapshot_matches_record,
    validate_mcp_evidence_snapshot_record,
)
from src.integrations.mcp.rollout_evidence import (
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPGateBlocker,
    MCPRedLineCount,
    MCPRolloutEvidencePayload,
    MCPRolloutStage,
    MCPSafetyRedLine,
    MCPShadowScenario,
    MCPShadowScenarioObservation,
    canonical_evidence_attestation_signature,
    canonical_evidence_content_digest,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


_ATTESTATION_KEY_ID = "prod-rollout-v1"
_ATTESTATION_KEY = b"test-only-production-rollout-attestation-key"
_TRUSTED_ATTESTATION_KEYS = {_ATTESTATION_KEY_ID: _ATTESTATION_KEY}


class Phase3RolloutControlPostgresRoleContractTests(unittest.TestCase):
    def test_postgres_commands_use_distinct_evaluator_and_operator_dsns(self) -> None:
        cases = (
            (
                "append-block",
                "evaluator",
                "MAF_MCP_ROLLOUT_EVALUATOR_DSN",
                "postgresql+psycopg://evaluator",
            ),
            (
                "activate",
                "operator",
                "MAF_MCP_ROLLOUT_OPERATOR_DSN",
                "postgresql+psycopg://operator",
            ),
        )
        for command, role, dsn_env, dsn in cases:
            with self.subTest(command=command):
                engine = Mock()
                storage = object()
                factory = object()
                with (
                    patch.dict(os.environ, {dsn_env: dsn}, clear=True),
                    patch.object(
                        control,
                        "build_state_platform_runtime_config",
                        return_value=SimpleNamespace(
                            backend=control.StatePlatformBackend.POSTGRESQL
                        ),
                    ),
                    patch.object(
                        control, "create_postgres_engine", return_value=engine
                    ) as create_engine,
                    patch.object(
                        control,
                        "create_postgres_session_factory",
                        return_value=factory,
                    ),
                    patch.object(
                        control, "validate_mcp_rollout_connection_role"
                    ) as validate_role,
                    patch.object(
                        control, "PostgreSQLStorage", return_value=storage
                    ) as storage_cls,
                ):
                    configured, configured_engine = control._configured_storage(
                        SimpleNamespace(command=command, database_path="ignored.sqlite3")
                    )
                self.assertIs(configured, storage)
                self.assertIs(configured_engine, engine)
                create_engine.assert_called_once_with(dsn)
                validate_role.assert_called_once_with(engine, role)
                storage_cls.assert_called_once_with(
                    factory,
                    mcp_rollout_session_factory=factory,
                    mcp_rollout_role=role,
                )

    def test_postgres_control_role_failure_masks_dsn(self) -> None:
        secret = "postgresql+psycopg://operator:secret-password@db/rollout"
        engine = Mock()
        with (
            patch.dict(
                os.environ,
                {"MAF_MCP_ROLLOUT_OPERATOR_DSN": secret},
                clear=True,
            ),
            patch.object(
                control,
                "build_state_platform_runtime_config",
                return_value=SimpleNamespace(
                    backend=control.StatePlatformBackend.POSTGRESQL
                ),
            ),
            patch.object(control, "create_postgres_engine", return_value=engine),
            patch.object(
                control,
                "validate_mcp_rollout_connection_role",
                side_effect=RuntimeError(secret),
            ),
            self.assertRaisesRegex(
                RolloutControlError, "operator role is invalid"
            ) as caught,
        ):
            control._configured_storage(
                SimpleNamespace(command="activate", database_path="ignored.sqlite3")
            )
        self.assertNotIn(secret, str(caught.exception))
        engine.dispose.assert_called_once_with()


class Phase3RolloutControlTests(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 13, tzinfo=timezone.utc)

    def test_active_block_prevents_promotion_activation_and_replays_fail(self) -> None:
        evidence = self._evidence("evidence-a", snapshot_id=1)
        self._store(evidence)
        candidate = self._enforce_config(80)
        approval = asyncio.run(
            append_approval(
                self.storage,
                approval_id="approval-a",
                target_deployment_id="deploy-enforce",
                candidate_config=candidate,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                evidence=evidence,
                reason="independently reviewed",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        with self.assertRaisesRegex(ValueError, "already approved"):
            asyncio.run(
                append_approval(
                    self.storage,
                    approval_id="approval-replay",
                    target_deployment_id="deploy-enforce",
                    candidate_config=candidate,
                    target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                    evidence=evidence,
                    reason="independently reviewed",
                    approver="operator-a",
                    created_at=self.now + timedelta(hours=25, seconds=1),
                    trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
                )
            )
        asyncio.run(
            append_block(
                self.storage,
                block_id="block-a",
                evidence=evidence,
                reason_code=MCPGateBlocker.SAFETY_RED_LINE,
                reason="red-line observation",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25, seconds=2),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )

        with self.assertRaisesRegex(ValueError, "active promotion block"):
            asyncio.run(
                activate_approval(
                    self.storage,
                    activation_id="activation-a",
                    approval_id=approval.approval_id,
                    target_deployment_id=approval.deployment_id,
                    candidate_config=candidate,
                    target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                    evidence=evidence,
                    previous_activation_id=None,
                    reason="activate approved transition",
                    approver="operator-a",
                    created_at=self.now + timedelta(hours=25, seconds=3),
                    trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
                )
            )

    def test_production_attestation_round_trips_and_revalidates_from_storage(self) -> None:
        evidence = self._evidence("evidence-roundtrip", snapshot_id=1)
        self._store(evidence)

        stored = asyncio.run(
            self.storage.get_mcp_rollout_evidence_snapshot(evidence.evidence_id)
        )

        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.attestation_key_id, evidence.attestation_key_id)
        self.assertEqual(stored.attestation_signature, evidence.attestation_signature)
        self.assertEqual(
            validate_mcp_evidence_snapshot_record(
                stored,
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            ),
            (),
        )
        self.assertIn(
            MCPGateBlocker.ATTESTATION_MISSING,
            validate_mcp_evidence_snapshot_record(
                replace(
                    stored,
                    attestation_key_id=None,
                    attestation_signature=None,
                ),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            ),
        )
        self.assertIn(
            MCPGateBlocker.ATTESTATION_INVALID,
            validate_mcp_evidence_snapshot_record(
                replace(stored, attestation_signature="0" * 64),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            ),
        )
        self.assertTrue(mcp_evidence_snapshot_matches_record(evidence, stored))
        self.assertFalse(
            mcp_evidence_snapshot_matches_record(
                replace(evidence, git_sha="f" * 40), stored
            )
        )

    def test_control_rejects_valid_canonical_record_for_different_signed_fields(self) -> None:
        caller = self._evidence("evidence-canonical-binding", snapshot_id=1)
        self._store(caller)
        canonical = MCPEvidenceSnapshot.seal(
            evidence_id=caller.evidence_id,
            environment_id=caller.environment_id,
            git_sha="f" * 40,
            deployment_id=caller.deployment_id,
            stage=caller.stage,
            config_fingerprint=caller.config_fingerprint,
            window_started_at=caller.window_started_at,
            window_ended_at=caller.window_ended_at,
            recorded_at=caller.recorded_at,
            producer=caller.producer,
            source=caller.source,
            snapshot_id=caller.snapshot_id,
            nonce=caller.nonce,
            payload=caller.payload,
            attestation_key_id=_ATTESTATION_KEY_ID,
            attestation_key=_ATTESTATION_KEY,
        )
        canonical_content = {
            "evidence_id": canonical.evidence_id,
            "environment_id": canonical.environment_id,
            "git_sha": canonical.git_sha,
            "deployment_id": canonical.deployment_id,
            "stage": canonical.stage.value,
            "config_fingerprint": canonical.config_fingerprint,
            "window_started_at": canonical.window_started_at,
            "window_ended_at": canonical.window_ended_at,
            "recorded_at": canonical.recorded_at,
            "producer": canonical.producer.value,
            "source": canonical.source.value,
            "snapshot_id": canonical.snapshot_id,
            "nonce": canonical.nonce,
            "payload": _json_value(canonical.payload),
        }
        canonical_digest = canonical_evidence_content_digest(canonical_content)
        canonical_signature = canonical_evidence_attestation_signature(
            {
                "payload_digest": canonical_digest,
                "evidence_id": canonical.evidence_id,
                "environment_id": canonical.environment_id,
                "git_sha": canonical.git_sha,
                "deployment_id": canonical.deployment_id,
                "stage": canonical.stage,
                "config_fingerprint": canonical.config_fingerprint,
                "snapshot_id": canonical.snapshot_id,
                "nonce": canonical.nonce,
            },
            key_id=_ATTESTATION_KEY_ID,
            key=_ATTESTATION_KEY,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mcp_rollout_evidence_snapshot "
                    "SET git_sha = :git_sha, payload_digest = :payload_digest, "
                    "attestation_signature = :attestation_signature "
                    "WHERE evidence_id = :evidence_id"
                ),
                {
                    "git_sha": canonical.git_sha,
                    "payload_digest": canonical_digest,
                    "attestation_signature": canonical_signature,
                    "evidence_id": canonical.evidence_id,
                },
            )

        with self.assertRaisesRegex(
            RolloutControlError,
            "caller evidence does not match canonical stored evidence",
        ):
            asyncio.run(
                append_approval(
                    self.storage,
                    approval_id="approval-canonical-binding",
                    target_deployment_id="deploy-enforce",
                    candidate_config=self._enforce_config(80),
                    target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                    evidence=caller,
                    reason="reviewed",
                    approver="operator-a",
                    created_at=caller.recorded_at + timedelta(seconds=1),
                    trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
                )
            )

    def test_duplicate_activation_and_resolution_are_rejected(self) -> None:
        evidence = self._evidence("evidence-a", snapshot_id=1)
        self._store(evidence)
        candidate = self._enforce_config(80)
        approval = asyncio.run(
            append_approval(
                self.storage,
                approval_id="approval-a",
                target_deployment_id="deploy-enforce",
                candidate_config=candidate,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                evidence=evidence,
                reason="reviewed",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        activation_args = dict(
            activation_id="activation-a",
            approval_id=approval.approval_id,
            target_deployment_id=approval.deployment_id,
            candidate_config=candidate,
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
            evidence=evidence,
            previous_activation_id=None,
            reason="activate approved transition",
            approver="operator-a",
            created_at=self.now + timedelta(hours=25, seconds=1),
            trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
        )
        asyncio.run(activate_approval(self.storage, **activation_args))
        with self.assertRaisesRegex(ValueError, "replay|consumed"):
            asyncio.run(activate_approval(self.storage, **activation_args))

        evidence_b = self._evidence(
            "evidence-b",
            snapshot_id=2,
            started_at=evidence.window_ended_at,
        )
        self._store(evidence_b)
        block = asyncio.run(
            append_block(
                self.storage,
                block_id="block-b",
                evidence=evidence_b,
                reason_code=MCPGateBlocker.WINDOW_INCOMPLETE,
                reason="window gap observed",
                approver="operator-a",
                created_at=evidence_b.recorded_at + timedelta(seconds=1),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        resolution_candidate = self._enforce_config(60)
        resolution_approval = asyncio.run(
            append_approval(
                self.storage,
                approval_id="approval-resolution",
                target_deployment_id="deploy-resolution",
                candidate_config=resolution_candidate,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                evidence=evidence_b,
                reason="remediation reviewed",
                approver="operator-b",
                created_at=evidence_b.recorded_at + timedelta(seconds=2),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        resolution_args = dict(
            resolution_id="resolution-a",
            block_id=block.block_id,
            approval_id=resolution_approval.approval_id,
            evidence=evidence_b,
            reason="verified remediation",
            approver="operator-b",
            created_at=evidence_b.recorded_at + timedelta(seconds=3),
            trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
        )
        asyncio.run(resolve_block(self.storage, **resolution_args))
        with self.assertRaisesRegex(ValueError, "replay|already resolved"):
            asyncio.run(resolve_block(self.storage, **resolution_args))

    def test_rollback_bypasses_active_block_only_for_strict_exposure_decrease(
        self,
    ) -> None:
        current = self._enforce_config(80)
        lower = self._enforce_config(20)
        evidence = self._evidence(
            "evidence-rollback",
            snapshot_id=1,
            stage=MCPRolloutStage.COHORT_ENFORCE,
            config_fingerprint=current.fingerprint,
        )
        self._store(evidence)
        approval = asyncio.run(
            append_approval(
                self.storage,
                approval_id="approval-rollback",
                target_deployment_id="deploy-rollback",
                candidate_config=lower,
                target_stage=MCPRolloutStage.COHORT_ENFORCE,
                evidence=evidence,
                reason="rollback reviewed",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        asyncio.run(
            append_block(
                self.storage,
                block_id="block-rollback",
                evidence=evidence,
                reason_code=MCPGateBlocker.ERROR_RATE_REGRESSED,
                reason="error rate regression",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25, seconds=1),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )

        activation = asyncio.run(
            rollback_activation(
                self.storage,
                activation_id="activation-rollback",
                approval_id=approval.approval_id,
                target_deployment_id=approval.deployment_id,
                current_config=current,
                candidate_config=lower,
                target_stage=MCPRolloutStage.COHORT_ENFORCE,
                evidence=evidence,
                previous_activation_id=None,  # type: ignore[arg-type]
                reason="reduce exposure after regression",
                approver="operator-a",
                created_at=self.now + timedelta(hours=25, seconds=2),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        self.assertTrue(activation.is_rollback)

        canonical_off = MCPRolloutConfig.from_env({})
        off_evidence = self._evidence(
            "evidence-rollback-off",
            snapshot_id=2,
            started_at=evidence.window_ended_at,
            stage=MCPRolloutStage.COHORT_ENFORCE,
            config_fingerprint=current.fingerprint,
        )
        self._store(off_evidence)
        off_approval = asyncio.run(
            append_approval(
                self.storage,
                approval_id="approval-rollback-off",
                target_deployment_id="deploy-rollback-off",
                candidate_config=canonical_off,
                target_stage=MCPRolloutStage.OFF,
                evidence=off_evidence,
                reason="flag rollback reviewed",
                approver="operator-a",
                created_at=off_evidence.recorded_at + timedelta(seconds=1),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        off_activation = asyncio.run(
            rollback_activation(
                self.storage,
                activation_id="activation-rollback-off",
                approval_id=off_approval.approval_id,
                target_deployment_id=off_approval.deployment_id,
                current_config=current,
                candidate_config=canonical_off,
                target_stage=MCPRolloutStage.OFF,
                evidence=off_evidence,
                previous_activation_id=activation.activation_id,
                reason="disable user-scoped routing after regression",
                approver="operator-a",
                created_at=off_evidence.recorded_at + timedelta(seconds=2),
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )
        )
        self.assertTrue(off_activation.is_rollback)
        self.assertEqual(off_activation.stage, MCPRolloutStage.OFF.value)

        with self.assertRaisesRegex(
            RolloutControlError, "strict MCP exposure decrease"
        ):
            asyncio.run(
                rollback_activation(
                    self.storage,
                    activation_id="activation-not-lower",
                    approval_id="unused",
                    target_deployment_id="deploy-not-lower",
                    current_config=current,
                    candidate_config=current,
                    target_stage=MCPRolloutStage.COHORT_ENFORCE,
                    evidence=evidence,
                    previous_activation_id="previous-activation",
                    reason="not actually lower",
                    approver="operator-a",
                    created_at=off_evidence.recorded_at + timedelta(seconds=3),
                    trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
                )
            )

    def test_missing_reason_fails_before_storage_mutation(self) -> None:
        evidence = self._evidence("evidence-a", snapshot_id=1)
        self._store(evidence)
        with self.assertRaisesRegex(RolloutControlError, "reason is required"):
            asyncio.run(
                append_approval(
                    self.storage,
                    approval_id="approval-a",
                    target_deployment_id="deploy-enforce",
                    candidate_config=self._enforce_config(80),
                    target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                    evidence=evidence,
                    reason=" ",
                    approver="operator-a",
                    created_at=self.now + timedelta(hours=25),
                    trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
                )
            )

    def test_production_approval_fails_closed_without_trusted_keyring(self) -> None:
        evidence = self._evidence("evidence-untrusted", snapshot_id=1)
        self._store(evidence)

        with self.assertRaisesRegex(RolloutControlError, "attestation_missing"):
            asyncio.run(
                append_approval(
                    self.storage,
                    approval_id="approval-untrusted",
                    target_deployment_id="deploy-enforce",
                    candidate_config=self._enforce_config(80),
                    target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                    evidence=evidence,
                    reason="must not trust artifact-supplied identity",
                    approver="operator-a",
                    created_at=self.now + timedelta(hours=25),
                )
            )

    def _evidence(
        self,
        evidence_id: str,
        *,
        snapshot_id: int,
        started_at: datetime | None = None,
        stage: MCPRolloutStage = MCPRolloutStage.INTERNAL_SHADOW,
        config_fingerprint: str = "b" * 64,
    ) -> MCPEvidenceSnapshot:
        start = started_at or self.now
        return MCPEvidenceSnapshot.seal(
            evidence_id=evidence_id,
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id="deploy-shadow",
            stage=stage,
            config_fingerprint=config_fingerprint,
            window_started_at=start,
            window_ended_at=start + timedelta(hours=24),
            recorded_at=start + timedelta(hours=24, seconds=1),
            producer=MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
            source=MCPEvidenceSource.PRODUCTION,
            snapshot_id=snapshot_id,
            nonce=f"nonce-{evidence_id}",
            payload=MCPRolloutEvidencePayload(
                kind=(
                    MCPEvidenceKind.INTERNAL_SHADOW
                    if stage is MCPRolloutStage.INTERNAL_SHADOW
                    else MCPEvidenceKind.COHORT_ENFORCE
                ),
                shadow_scenarios=tuple(
                    MCPShadowScenarioObservation(scenario=item, matched_count=1)
                    for item in MCPShadowScenario
                ),
                red_line_counts=tuple(
                    MCPRedLineCount(item, 0) for item in MCPSafetyRedLine
                ),
                continuous_window=True,
                shadow_observation_count=len(MCPShadowScenario),
            ),
            attestation_key_id=_ATTESTATION_KEY_ID,
            attestation_key=_ATTESTATION_KEY,
        )

    def _store(self, evidence: MCPEvidenceSnapshot) -> None:
        asyncio.run(
            self.storage.append_mcp_rollout_evidence_snapshot(
                MCPRolloutEvidenceSnapshot(
                    evidence_id=evidence.evidence_id,
                    environment_id=evidence.environment_id,
                    git_sha=evidence.git_sha,
                    deployment_id=evidence.deployment_id,
                    stage=evidence.stage.value,
                    config_fingerprint=evidence.config_fingerprint,
                    window_started_at=evidence.window_started_at,
                    window_ended_at=evidence.window_ended_at,
                    recorded_at=evidence.recorded_at,
                    producer=evidence.producer.value,
                    source=evidence.source.value,
                    snapshot_id=evidence.snapshot_id,
                    nonce=evidence.nonce,
                    evidence_kind=evidence.payload.kind.value,
                    payload=_json_value(evidence.payload),
                    payload_digest=evidence.payload_digest,
                    attestation_key_id=evidence.attestation_key_id,
                    attestation_signature=evidence.attestation_signature,
                )
            )
        )

    @staticmethod
    def _enforce_config(percent: int) -> MCPRolloutConfig:
        return MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": str(percent),
                "MCP_ENFORCE_HASH_SALT": "stable-salt",
            }
        )


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


if __name__ == "__main__":
    unittest.main()

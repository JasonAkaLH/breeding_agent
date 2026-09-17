from __future__ import annotations

import unittest
import base64
import json
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory

from scripts.validate_user_mcp_phase3_evidence import (
    ARTIFACT_SCHEMA,
    ATTESTATION_KEYRING_SCHEMA,
    load_attestation_keyring,
    validate_artifact,
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
)


_NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)
_ATTESTATION_KEY_ID = "prod-rollout-v1"
_ATTESTATION_KEY = b"test-only-production-rollout-attestation-key"
_TRUSTED_ATTESTATION_KEYS = {_ATTESTATION_KEY_ID: _ATTESTATION_KEY}


class Phase3EvidenceValidatorTests(unittest.TestCase):
    def test_replay_is_rejected_after_domain_reconstruction(self) -> None:
        evidence = self._ci_evidence()
        artifact = self._artifact(
            evidence,
            target_stage=MCPRolloutStage.INTERNAL_SHADOW,
            records=[evidence, evidence],
        )

        result = validate_artifact(artifact)

        self.assertFalse(result["allowed"])
        self.assertIn(MCPGateBlocker.EVIDENCE_ID_REPLAY.value, result["blockers"])

    def test_ci_source_cannot_satisfy_production_transition(self) -> None:
        production_shape_with_ci_source = self._shadow_evidence(
            source=MCPEvidenceSource.CI
        )
        artifact = self._artifact(
            production_shape_with_ci_source,
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
        )

        result = validate_artifact(artifact)

        self.assertFalse(result["allowed"])
        self.assertIn(MCPGateBlocker.SOURCE_POLICY_VIOLATION.value, result["blockers"])

    def test_supplied_digest_is_verified_not_silently_resealed(self) -> None:
        evidence = self._ci_evidence()
        artifact = self._artifact(
            evidence, target_stage=MCPRolloutStage.INTERNAL_SHADOW
        )
        artifact["records"][0]["payload_digest"] = "0" * 64

        result = validate_artifact(artifact)

        self.assertIn(MCPGateBlocker.DIGEST_INVALID.value, result["blockers"])

    def test_production_attestation_requires_external_trusted_keyring(self) -> None:
        evidence = self._shadow_evidence(source=MCPEvidenceSource.PRODUCTION)
        artifact = self._artifact(
            evidence,
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
        )

        without_keyring = validate_artifact(artifact)
        with_keyring = validate_artifact(
            artifact,
            trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
        )
        forged_artifact = self._artifact(
            replace(evidence, attestation_signature="0" * 64),
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
        )
        forged = validate_artifact(
            forged_artifact,
            trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
        )

        self.assertIn(
            MCPGateBlocker.ATTESTATION_MISSING.value, without_keyring["blockers"]
        )
        self.assertNotIn(
            MCPGateBlocker.ATTESTATION_MISSING.value, with_keyring["blockers"]
        )
        self.assertNotIn(
            MCPGateBlocker.ATTESTATION_INVALID.value, with_keyring["blockers"]
        )
        self.assertIn(MCPGateBlocker.ATTESTATION_INVALID.value, forged["blockers"])

    def test_ci_artifact_cannot_carry_a_production_attestation(self) -> None:
        evidence = replace(
            self._ci_evidence(),
            attestation_key_id=_ATTESTATION_KEY_ID,
            attestation_signature="0" * 64,
        )

        result = validate_artifact(
            self._artifact(evidence, target_stage=MCPRolloutStage.INTERNAL_SHADOW),
            trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
        )

        self.assertFalse(result["allowed"])
        self.assertIn(MCPGateBlocker.ATTESTATION_INVALID.value, result["blockers"])

    def test_keyring_loader_is_strict_and_decodes_canonical_base64(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "keyring.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": ATTESTATION_KEYRING_SCHEMA,
                        "keys": {
                            _ATTESTATION_KEY_ID: base64.b64encode(
                                _ATTESTATION_KEY
                            ).decode("ascii")
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_attestation_keyring(path), _TRUSTED_ATTESTATION_KEYS)

            path.write_text(
                json.dumps(
                    {
                        "schema": ATTESTATION_KEYRING_SCHEMA,
                        "keys": {_ATTESTATION_KEY_ID: "not-base64!"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "canonical base64"):
                load_attestation_keyring(path)

    def _ci_evidence(self) -> MCPEvidenceSnapshot:
        return MCPEvidenceSnapshot.seal(
            evidence_id="evidence-ci",
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id="deploy-off",
            stage=MCPRolloutStage.OFF,
            config_fingerprint="b" * 64,
            window_started_at=_NOW,
            window_ended_at=_NOW + timedelta(hours=1),
            recorded_at=_NOW + timedelta(hours=1, seconds=1),
            producer=MCPEvidenceProducer.CI_PIPELINE,
            source=MCPEvidenceSource.CI,
            snapshot_id=1,
            nonce="nonce-ci",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.CI_CONFORMANCE,
                ci_conformance_passed=True,
                red_line_counts=self._red_lines(),
            ),
        )

    def _shadow_evidence(self, *, source: MCPEvidenceSource) -> MCPEvidenceSnapshot:
        producer = (
            MCPEvidenceProducer.CI_PIPELINE
            if source is MCPEvidenceSource.CI
            else MCPEvidenceProducer.PRODUCTION_SNAPSHOT
        )
        return MCPEvidenceSnapshot.seal(
            evidence_id="evidence-shadow",
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id="deploy-shadow",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint="c" * 64,
            window_started_at=_NOW,
            window_ended_at=_NOW + timedelta(hours=24),
            recorded_at=_NOW + timedelta(hours=24, seconds=1),
            producer=producer,
            source=source,
            snapshot_id=1,
            nonce="nonce-shadow",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.INTERNAL_SHADOW,
                shadow_scenarios=tuple(
                    MCPShadowScenarioObservation(scenario=item, matched_count=1)
                    for item in MCPShadowScenario
                ),
                red_line_counts=self._red_lines(),
                continuous_window=True,
                shadow_observation_count=len(MCPShadowScenario),
            ),
            attestation_key_id=(
                _ATTESTATION_KEY_ID if source is MCPEvidenceSource.PRODUCTION else None
            ),
            attestation_key=(
                _ATTESTATION_KEY if source is MCPEvidenceSource.PRODUCTION else None
            ),
        )

    @staticmethod
    def _red_lines() -> tuple[MCPRedLineCount, ...]:
        return tuple(MCPRedLineCount(item, 0) for item in MCPSafetyRedLine)

    @staticmethod
    def _artifact(
        evidence: MCPEvidenceSnapshot,
        *,
        target_stage: MCPRolloutStage,
        records: list[MCPEvidenceSnapshot] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": ARTIFACT_SCHEMA,
            "request": {
                "evidence_id": evidence.evidence_id,
                "environment_id": evidence.environment_id,
                "evidence_deployment_id": evidence.deployment_id,
                "evidence_config_fingerprint": evidence.config_fingerprint,
                "current_stage": evidence.stage.value,
                "target_stage": target_stage.value,
            },
            "records": [_json_value(item) for item in (records or [evidence])],
        }


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

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from src.integrations.mcp.observability import (
    mcp_evidence_snapshot_to_record,
    validate_mcp_evidence_snapshot_record,
)
from src.integrations.mcp.rollout_evidence import (
    FIRST_ENABLEMENT_EMPTY_TABLES,
    MCPFirstEnablementPayload,
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPRolloutEvidencePayload,
    MCPRolloutStage,
    canonical_evidence_content_digest,
    evaluate_mcp_stage_observation,
    validate_evidence_snapshot,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def first_snapshot():
    counts = {name: 0 for name in FIRST_ENABLEMENT_EMPTY_TABLES}
    return MCPEvidenceSnapshot.seal(
        evidence_id="initial-evidence",
        environment_id="prod",
        git_sha="a" * 40,
        deployment_id="release-1",
        stage=MCPRolloutStage.OFF,
        config_fingerprint="b" * 64,
        window_started_at=NOW,
        window_ended_at=NOW,
        recorded_at=NOW,
        producer=MCPEvidenceProducer.DEPLOYMENT_INITIALIZER,
        source=MCPEvidenceSource.DEPLOYMENT,
        snapshot_id=1,
        nonce="initial-nonce",
        payload=MCPFirstEnablementPayload(
            kind=MCPEvidenceKind.FIRST_ENABLEMENT,
            schema_version="maf.mcp.first_enablement.v1",
            database_name="biobin_db",
            backend_image_digest="sha256:" + "c" * 64,
            administrator="postgres",
            initial_table_counts=counts,
            initial_state_digest=canonical_evidence_content_digest(counts),
        ),
    )


class FirstEnablementEvidenceTest(unittest.TestCase):
    def test_initialization_is_valid_but_not_observation_evidence(self):
        snapshot = first_snapshot()
        self.assertEqual(validate_evidence_snapshot(snapshot), ())
        self.assertTrue(evaluate_mcp_stage_observation(snapshot))
        record = mcp_evidence_snapshot_to_record(snapshot)
        self.assertEqual(validate_mcp_evidence_snapshot_record(record), ())

    def test_source_kind_stage_and_empty_inventory_are_closed(self):
        snapshot = first_snapshot()
        for modified in (
            replace(
                snapshot,
                source=MCPEvidenceSource.CI,
                producer=MCPEvidenceProducer.CI_PIPELINE,
            ),
            replace(snapshot, stage=MCPRolloutStage.LEGACY_ASSEMBLY_OFF),
            replace(
                snapshot, payload=replace(snapshot.payload, initial_table_counts={})
            ),
            replace(
                snapshot,
                payload=replace(
                    snapshot.payload,
                    initial_table_counts={
                        **snapshot.payload.initial_table_counts,
                        "user_mcp_server": 1,
                    },
                ),
            ),
            replace(
                snapshot,
                payload=replace(snapshot.payload, backend_image_digest="latest"),
            ),
            replace(
                snapshot,
                attestation_key_id="unexpected",
                attestation_signature="d" * 64,
            ),
        ):
            with self.subTest(modified=modified):
                self.assertTrue(validate_evidence_snapshot(modified))

    def test_existing_ci_evidence_digest_does_not_change(self):
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id="legacy",
            environment_id="prod",
            git_sha="a" * 40,
            deployment_id="release-1",
            stage=MCPRolloutStage.OFF,
            config_fingerprint="b" * 64,
            window_started_at=NOW,
            window_ended_at=NOW + timedelta(minutes=1),
            recorded_at=NOW + timedelta(minutes=1),
            producer=MCPEvidenceProducer.CI_PIPELINE,
            source=MCPEvidenceSource.CI,
            snapshot_id=1,
            nonce="legacy-nonce",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.CI_CONFORMANCE, ci_conformance_passed=True
            ),
        )
        self.assertEqual(
            snapshot.payload_digest,
            "da4fb333968dc9413a69715d8a3e7af31998f7ad9d3099c58efb6147971ee863",
        )

    def test_parser_roundtrip_and_unknown_fields(self):
        from dataclasses import asdict
        from scripts.validate_user_mcp_phase3_evidence import (
            parse_evidence_snapshot,
            Phase3EvidenceError,
        )

        raw = asdict(first_snapshot())
        for key in ("window_started_at", "window_ended_at", "recorded_at"):
            raw[key] = raw[key].isoformat()
        self.assertEqual(validate_evidence_snapshot(parse_evidence_snapshot(raw)), ())
        raw["payload"]["ci_conformance_passed"] = True
        with self.assertRaises(Phase3EvidenceError):
            parse_evidence_snapshot(raw)

    def test_empty_inventory_is_explicit_and_covers_every_mcp_business_table(self):
        from src.storage.sqlalchemy_base import SQLiteBase
        import src.storage.sqlalchemy_models  # noqa: F401

        expected = {
            name
            for name in SQLiteBase.metadata.tables
            if name.startswith(("mcp_", "user_mcp_"))
        }
        expected -= {"mcp_dispatch_aggregate_migration", "mcp_rollout_gate_scope"}
        self.assertEqual(set(FIRST_ENABLEMENT_EMPTY_TABLES), expected)

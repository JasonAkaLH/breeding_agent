from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.core.models import (
    MCPRolloutEvidenceSnapshot,
    MCPRolloutMetricBucket,
    MCPShadowAuditSample,
)
from src.integrations.mcp.audit import MCPAuditService
from src.integrations.mcp.shadow_evidence import (
    APPROVED_VERIFIED_RETIRE_BLOCKER,
    MCP_SHADOW_SAMPLE_EXPECTATIONS,
    build_internal_shadow_evidence_payload,
    seal_shadow_audit_sample,
    validate_shadow_audit_sample,
)
from src.integrations.mcp.rollout_evidence import CURRENT_MCP_SHADOW_SCENARIOS
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
FP = "a" * 64


class MCPShadowAuditSampleStorageTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)

    def _sample(self, sample_id: str = "sample-1", nonce: str = "nonce-1", **changes):
        sample = MCPShadowAuditSample(
            sample_id=sample_id,
            environment_id="staging",
            deployment_id="deploy-1",
            stage="internal_shadow",
            config_fingerprint=FP,
            manifest_fingerprint="b" * 64,
            fixture_fingerprint="c" * 64,
            mapping_fingerprint="d" * 64,
            scenario="https_streamable_success",
            nonce=nonce,
            safe_owner_ref="hmac-sha256:" + "1" * 64,
            safe_task_ref="hmac-sha256:" + "2" * 64,
            safe_call_ref="hmac-sha256:" + "3" * 64,
            legacy_outcome="tool_call_succeeded",
            shadow_outcome="control_plane_ready",
            transport="streamable_http",
            endpoint_policy="runtime_enforced",
            comparison="matched",
            blockers=(),
            payload_digest="",
            observed_at=NOW,
            recorded_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
        return seal_shadow_audit_sample(replace(sample, **changes))

    def test_save_is_exactly_idempotent_and_conflicting_id_fails(self) -> None:
        sample = self._sample()
        first = asyncio.run(self.storage.save_mcp_shadow_audit_sample(sample))
        second = asyncio.run(self.storage.save_mcp_shadow_audit_sample(sample))
        self.assertEqual(first, second)

        conflict = self._sample(
            comparison="mismatched",
            blockers=("shadow_outcome_mismatch",),
        )
        with self.assertRaisesRegex(ValueError, "payload conflict"):
            asyncio.run(self.storage.save_mcp_shadow_audit_sample(conflict))

    def test_nonce_replay_is_rejected_within_scope(self) -> None:
        asyncio.run(self.storage.save_mcp_shadow_audit_sample(self._sample()))
        with self.assertRaisesRegex(ValueError, "nonce replay"):
            asyncio.run(
                self.storage.save_mcp_shadow_audit_sample(
                    self._sample(sample_id="sample-2")
                )
            )

    def test_digest_tamper_and_retention_tamper_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "digest_invalid"):
            asyncio.run(
                self.storage.save_mcp_shadow_audit_sample(
                    replace(self._sample(), shadow_outcome="timeout")
                )
            )
        with self.assertRaisesRegex(ValueError, "retention_invalid"):
            asyncio.run(
                self.storage.save_mcp_shadow_audit_sample(
                    self._sample(expires_at=NOW + timedelta(days=29))
                )
            )

    def test_hostile_closed_values_and_plaintext_safe_refs_fail_closed(self) -> None:
        hostile_changes = (
            ({"scenario": "future_scenario"}, "scenario_invalid"),
            ({"legacy_outcome": "raw_success"}, "outcome_invalid"),
            ({"shadow_outcome": "raw_success"}, "outcome_invalid"),
            ({"transport": "stdio"}, "transport_invalid"),
            ({"endpoint_policy": "https_only"}, "endpoint_policy_invalid"),
            ({"comparison": "close_enough"}, "comparison_invalid"),
            ({"safe_owner_ref": "owner-plain"}, "safe_ref_invalid"),
            (
                {"safe_task_ref": "hmac-sha256:" + "A" * 64},
                "safe_ref_invalid",
            ),
            ({"safe_call_ref": "hmac-sha256:" + "3" * 63}, "safe_ref_invalid"),
            (
                {"comparison": "mismatched", "blockers": ("future:blocker",)},
                "blockers_invalid",
            ),
            (
                {
                    "comparison": "matched",
                    "blockers": ("shadow_outcome_mismatch",),
                },
                "blockers_invalid",
            ),
            (
                {
                    "comparison": "excluded",
                    "blockers": ("shadow_outcome_mismatch",),
                },
                "blockers_invalid",
            ),
            (
                {"legacy_outcome": "control_plane_ready"},
                "matched_expectation_invalid",
            ),
            (
                {"scenario": "authentication_failure"},
                "matched_expectation_invalid",
            ),
            (
                {
                    "scenario": "permission_denial",
                    "shadow_outcome": "control_plane_ready",
                },
                "matched_expectation_invalid",
            ),
        )
        for index, (changes, expected) in enumerate(hostile_changes):
            with self.subTest(changes=changes):
                sample = self._sample(
                    sample_id=f"hostile-{index}",
                    nonce=f"hostile-nonce-{index}",
                    **changes,
                )
                self.assertIn(expected, validate_shadow_audit_sample(sample))
                with self.assertRaisesRegex(ValueError, expected):
                    asyncio.run(self.storage.save_mcp_shadow_audit_sample(sample))

    def test_safe_refs_accept_only_null_or_lowercase_hmac_sha256(self) -> None:
        null_refs = self._sample(
            safe_owner_ref=None,
            safe_task_ref=None,
            safe_call_ref=None,
        )
        self.assertEqual(validate_shadow_audit_sample(null_refs), ())
        self.assertEqual(validate_shadow_audit_sample(self._sample()), ())

    def test_current_and_historical_matched_scenarios_use_golden_expectations(self) -> None:
        self.assertEqual(len(CURRENT_MCP_SHADOW_SCENARIOS), 7)
        self.assertEqual(len(MCP_SHADOW_SAMPLE_EXPECTATIONS), 8)
        for index, (scenario, expectation) in enumerate(
            MCP_SHADOW_SAMPLE_EXPECTATIONS.items()
        ):
            legacy_outcome, shadow_outcome, transport, endpoint_policy = expectation
            sample = self._sample(
                sample_id=f"golden-{index}",
                nonce=f"golden-nonce-{index}",
                scenario=scenario,
                legacy_outcome=legacy_outcome,
                shadow_outcome=shadow_outcome,
                transport=transport,
                endpoint_policy=endpoint_policy,
            )
            self.assertEqual(validate_shadow_audit_sample(sample), ())

    def test_scoped_listing_requires_full_retention_covering_window(self) -> None:
        asyncio.run(self.storage.save_mcp_shadow_audit_sample(self._sample()))
        rows = asyncio.run(
            self.storage.list_mcp_shadow_audit_samples(
                "staging",
                "deploy-1",
                "internal_shadow",
                window_started_at=NOW - timedelta(minutes=1),
                window_ended_at=NOW + timedelta(days=1),
            )
        )
        self.assertEqual(rows, [self._sample()])

    def test_audit_service_awaits_durable_sample(self) -> None:
        saved = asyncio.run(
            MCPAuditService(storage=self.storage).record_shadow_sample(self._sample())
        )
        self.assertEqual(saved.sample_id, "sample-1")

    def test_expired_samples_are_deleted_at_the_thirty_day_boundary(self) -> None:
        asyncio.run(self.storage.save_mcp_shadow_audit_sample(self._sample()))
        deleted = asyncio.run(
            self.storage.delete_expired_mcp_shadow_audit_samples(
                now=NOW + timedelta(days=30), limit=100
            )
        )
        self.assertEqual(deleted, 1)

    def test_aggregation_does_not_dilute_mismatch_and_rejects_mixed_scope(self) -> None:
        matched = self._sample()
        mismatch = self._sample(
            sample_id="sample-2",
            nonce="nonce-2",
            comparison="mismatched",
            blockers=("shadow_outcome_mismatch",),
        )
        mixed = self._sample(
            sample_id="sample-3",
            nonce="nonce-3",
            deployment_id="deploy-other",
        )
        payload = build_internal_shadow_evidence_payload(
            (matched, mismatch, mixed),
            environment_id="staging",
            deployment_id="deploy-1",
            config_fingerprint=FP,
            manifest_fingerprint="b" * 64,
            fixture_fingerprint="c" * 64,
            mapping_fingerprint="d" * 64,
            window_started_at=NOW - timedelta(minutes=1),
            window_ended_at=NOW + timedelta(minutes=1),
        )
        scenario = payload.shadow_scenarios[0]
        self.assertEqual((scenario.matched_count, scenario.mismatched_count), (1, 1))
        self.assertEqual(payload.unresolved_mismatch_count, 1)
        self.assertEqual(payload.invalid_evidence_count, 1)

    def test_approved_retire_is_not_quota_and_not_unapproved(self) -> None:
        retired = self._sample(
            comparison="not_comparable",
            blockers=(APPROVED_VERIFIED_RETIRE_BLOCKER,),
        )
        payload = build_internal_shadow_evidence_payload(
            (retired,),
            environment_id="staging",
            deployment_id="deploy-1",
            config_fingerprint=FP,
            manifest_fingerprint="b" * 64,
            fixture_fingerprint="c" * 64,
            mapping_fingerprint="d" * 64,
            window_started_at=NOW - timedelta(minutes=1),
            window_ended_at=NOW + timedelta(minutes=1),
        )
        scenario = payload.shadow_scenarios[0]
        self.assertEqual(scenario.matched_count, 0)
        self.assertEqual(scenario.not_comparable_count, 1)
        self.assertEqual(payload.unapproved_not_comparable_count, 0)

    def test_producer_reads_samples_metrics_and_appends_in_one_storage_transaction(self) -> None:
        asyncio.run(self.storage.save_mcp_shadow_audit_sample(self._sample()))
        asyncio.run(
            self.storage.upsert_mcp_rollout_metric_bucket(
                MCPRolloutMetricBucket(
                    metric_bucket_id="metric-1",
                    environment_id="staging",
                    deployment_id="deploy-1",
                    stage="internal_shadow",
                    config_fingerprint=FP,
                    metric_name="mcp_route_requests_total",
                    bucket_started_at=NOW - timedelta(minutes=1),
                    bucket_ended_at=NOW,
                    execution_path="legacy",
                    routing_mode="shadow",
                    transport="not_applicable",
                    protocol_version="not_applicable",
                    adapter="legacy_global_runtime",
                    result_category="succeeded",
                    error_category="none",
                    latency_bucket="not_applicable",
                    value=1,
                )
            )
        )

        def builder(samples, metrics):
            self.assertEqual([item.sample_id for item in samples], ["sample-1"])
            self.assertEqual([item.metric_bucket_id for item in metrics], ["metric-1"])
            return MCPRolloutEvidenceSnapshot(
                evidence_id="evidence-1",
                environment_id="staging",
                git_sha="a" * 40,
                deployment_id="deploy-1",
                stage="internal_shadow",
                config_fingerprint=FP,
                window_started_at=NOW - timedelta(minutes=1),
                window_ended_at=NOW + timedelta(minutes=1),
                recorded_at=NOW + timedelta(minutes=2),
                producer="production_snapshot_producer",
                source="production",
                snapshot_id=1,
                nonce="evidence-nonce-1",
                evidence_kind="internal_shadow",
                payload={"from_storage": True},
                payload_digest="e" * 64,
                attestation_key_id="test-key",
                attestation_signature="f" * 64,
            )

        saved = asyncio.run(
            self.storage.produce_mcp_shadow_evidence_snapshot(
                "staging",
                "deploy-1",
                window_started_at=NOW - timedelta(minutes=1),
                window_ended_at=NOW + timedelta(minutes=1),
                builder=builder,
            )
        )
        self.assertEqual(saved.evidence_id, "evidence-1")

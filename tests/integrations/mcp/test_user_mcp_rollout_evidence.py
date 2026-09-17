from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from src.integrations.mcp.rollout import MCPRolloutConfig
from src.integrations.mcp.rollout_evidence import (
    CURRENT_MCP_SHADOW_SCENARIOS,
    MCPCallKind,
    MCPCallKindObservation,
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPGateBlocker,
    MCPGateStatus,
    MCPMetricAdapter,
    MCPMetricBucket,
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricLabels,
    MCPMetricName,
    MCPMetricProtocolVersion,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPMetricTransport,
    MCPRedLineCount,
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutDrill,
    MCPRolloutEvidencePayload,
    MCPRolloutPromotionBlock,
    MCPRolloutStage,
    MCPRolloutStageApproval,
    MCPSafetyRedLine,
    MCPShadowScenario,
    MCPShadowScenarioObservation,
    MCPStageGateRequest,
    active_mcp_promotion_blocks,
    canonical_evidence_digest,
    evaluate_mcp_stage_gate as _evaluate_mcp_stage_gate,
    is_provable_mcp_exposure_decrease,
    validate_evidence_records as _validate_evidence_records,
    validate_evidence_snapshot as _validate_evidence_snapshot,
)


_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
_GIT_SHA = "a" * 40
_CONFIG_FINGERPRINT = "b" * 64
_ATTESTATION_KEY_ID = "prod-rollout-v1"
_ATTESTATION_KEY = b"test-only-production-rollout-attestation-key"
_TRUSTED_ATTESTATION_KEYS = {_ATTESTATION_KEY_ID: _ATTESTATION_KEY}


def validate_evidence_snapshot(snapshot: MCPEvidenceSnapshot) -> tuple[MCPGateBlocker, ...]:
    return _validate_evidence_snapshot(
        snapshot,
        trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
    )


def validate_evidence_records(
    records: tuple[MCPEvidenceSnapshot, ...],
):
    return _validate_evidence_records(
        records,
        trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
    )


def evaluate_mcp_stage_gate(
    request: MCPStageGateRequest,
    records: tuple[MCPEvidenceSnapshot, ...],
):
    return _evaluate_mcp_stage_gate(
        request,
        records,
        trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
    )


class UserMCPRolloutEvidenceTests(unittest.TestCase):
    def test_metric_bucket_has_only_closed_low_cardinality_labels(self) -> None:
        labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            transport=MCPMetricTransport.STREAMABLE_HTTP,
            protocol_version=MCPMetricProtocolVersion.V2026_07_28,
            adapter=MCPMetricAdapter.PYTHON_2026,
            result_category=MCPMetricResultCategory.SUCCEEDED,
            error_category=MCPMetricErrorCategory.NONE,
            call_kind=MCPCallKind.ORDINARY,
        )
        bucket = MCPMetricBucket(
            metric_name=MCPMetricName.TOOL_CALL_DURATION_SECONDS,
            bucket_started_at=_START,
            bucket_ended_at=_START + timedelta(minutes=1),
            labels=labels,
            value=7,
        )

        self.assertEqual(bucket.labels.transport, MCPMetricTransport.STREAMABLE_HTTP)
        self.assertFalse(hasattr(bucket.labels, "owner_user_id"))
        self.assertFalse(hasattr(bucket.labels, "url"))
        with self.assertRaises(FrozenInstanceError):
            bucket.value = 8  # type: ignore[misc]

    def test_metric_bucket_contract_rejects_nonminute_and_duplicate_identity(
        self,
    ) -> None:
        labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            result_category=MCPMetricResultCategory.SUCCEEDED,
            error_category=MCPMetricErrorCategory.NONE,
            call_kind=MCPCallKind.ORDINARY,
        )
        bucket = MCPMetricBucket(
            metric_name=MCPMetricName.TOOL_CALLS_TOTAL,
            bucket_started_at=_START,
            bucket_ended_at=_START + timedelta(minutes=1),
            labels=labels,
            value=1,
        )

        for label, hostile in (
            ("coarse", replace(bucket, bucket_ended_at=_START + timedelta(minutes=2))),
            ("subminute", replace(bucket, bucket_ended_at=_START + timedelta(seconds=30))),
            (
                "unaligned",
                replace(
                    bucket,
                    bucket_started_at=_START + timedelta(seconds=1),
                    bucket_ended_at=_START + timedelta(minutes=1, seconds=1),
                ),
            ),
        ):
            with self.subTest(case=label):
                snapshot = self._snapshot(
                    stage=MCPRolloutStage.OFF,
                    duration=timedelta(hours=1),
                    source=MCPEvidenceSource.CI,
                    producer=MCPEvidenceProducer.CI_PIPELINE,
                    payload=replace(
                        self._payload(
                            kind=MCPEvidenceKind.CI_CONFORMANCE,
                            ci_conformance_passed=True,
                        ),
                        metric_buckets=(hostile,),
                    ),
                )
                self.assertIn(
                    MCPGateBlocker.PAYLOAD_INVALID,
                    validate_evidence_snapshot(snapshot),
                )

        duplicate = self._snapshot(
            stage=MCPRolloutStage.OFF,
            duration=timedelta(hours=1),
            source=MCPEvidenceSource.CI,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=replace(
                self._payload(
                    kind=MCPEvidenceKind.CI_CONFORMANCE,
                    ci_conformance_passed=True,
                ),
                metric_buckets=(bucket, replace(bucket, value=2)),
            ),
        )
        self.assertIn(
            MCPGateBlocker.PAYLOAD_INVALID,
            validate_evidence_snapshot(duplicate),
        )

        distinct_labels = self._snapshot(
            stage=MCPRolloutStage.OFF,
            duration=timedelta(hours=1),
            source=MCPEvidenceSource.CI,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=replace(
                self._payload(
                    kind=MCPEvidenceKind.CI_CONFORMANCE,
                    ci_conformance_passed=True,
                ),
                metric_buckets=(
                    bucket,
                    replace(
                        bucket,
                        labels=replace(
                            labels,
                            transport=MCPMetricTransport.STREAMABLE_HTTP,
                        ),
                    ),
                ),
            ),
        )
        self.assertNotIn(
            MCPGateBlocker.PAYLOAD_INVALID,
            validate_evidence_snapshot(distinct_labels),
        )

    def test_canonical_digest_is_stable_and_payload_tampering_is_blocked(self) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.OFF,
            duration=timedelta(hours=1),
            source=MCPEvidenceSource.CI,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=self._payload(
                kind=MCPEvidenceKind.CI_CONFORMANCE,
                ci_conformance_passed=True,
            ),
        )

        self.assertEqual(snapshot.payload_digest, canonical_evidence_digest(snapshot))
        self.assertEqual(validate_evidence_snapshot(snapshot), ())
        tampered = replace(
            snapshot,
            payload=replace(snapshot.payload, ci_conformance_passed=False),
        )
        self.assertIn(MCPGateBlocker.DIGEST_INVALID, validate_evidence_snapshot(tampered))

    def test_production_attestation_is_required_and_verified_fail_closed(self) -> None:
        payload = self._internal_enforce_payload()
        with self.assertRaises(ValueError):
            self._snapshot(
                stage=MCPRolloutStage.INTERNAL_ENFORCE,
                duration=timedelta(hours=48),
                payload=payload,
                attest=False,
            )

        snapshot = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=payload,
        )
        self.assertIn(
            MCPGateBlocker.ATTESTATION_MISSING,
            _validate_evidence_snapshot(snapshot),
        )
        forged = replace(snapshot, attestation_signature="0" * 64)
        self.assertIn(MCPGateBlocker.ATTESTATION_INVALID, validate_evidence_snapshot(forged))

    def test_production_identity_tampering_cannot_be_resealed_without_the_key(self) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._internal_enforce_payload(),
        )
        tampered = replace(snapshot, deployment_id="deployment-forged", payload_digest="")
        tampered = replace(tampered, payload_digest=canonical_evidence_digest(tampered))

        blockers = validate_evidence_snapshot(tampered)

        self.assertNotIn(MCPGateBlocker.DIGEST_INVALID, blockers)
        self.assertIn(MCPGateBlocker.ATTESTATION_INVALID, blockers)

    def test_production_empty_buckets_and_forged_summary_are_blocked(self) -> None:
        empty = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._internal_enforce_payload(),
            complete_metrics=False,
        )
        self.assertIn(MCPGateBlocker.METRIC_SERIES_MISSING, validate_evidence_snapshot(empty))

        snapshot = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._internal_enforce_payload(),
        )
        terminal_only = self._resign(
            replace(
                snapshot,
                payload=replace(
                    snapshot.payload,
                    metric_buckets=tuple(
                        bucket
                        for bucket in snapshot.payload.metric_buckets
                        if bucket.metric_name is not MCPMetricName.SAFETY_RED_LINE_TOTAL
                    ),
                ),
            )
        )
        self.assertIn(
            MCPGateBlocker.METRIC_SERIES_MISSING,
            validate_evidence_snapshot(terminal_only),
        )
        call_kind = snapshot.payload.call_kinds[0]
        forged_payload = replace(
            snapshot.payload,
            call_kinds=(replace(call_kind, terminal_success_count=call_kind.terminal_success_count + 1),),
        )
        forged = self._resign(replace(snapshot, payload=forged_payload))

        self.assertIn(
            MCPGateBlocker.METRIC_SUMMARY_MISMATCH,
            validate_evidence_snapshot(forged),
        )

    def test_record_validation_rejects_nonce_snapshot_and_id_replays(self) -> None:
        first = self._snapshot(
            evidence_id="evidence-1",
            nonce="nonce-1",
            snapshot_id=1,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._internal_enforce_payload(),
        )
        nonce_replay = self._snapshot(
            evidence_id="evidence-2",
            nonce="nonce-1",
            snapshot_id=2,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            started_at=_START,
            duration=timedelta(hours=49),
            recorded_at=_START + timedelta(hours=50),
            payload=self._internal_enforce_payload(),
        )
        snapshot_replay = self._snapshot(
            evidence_id="evidence-3",
            nonce="nonce-3",
            snapshot_id=1,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            started_at=_START,
            duration=timedelta(hours=50),
            recorded_at=_START + timedelta(hours=51),
            payload=self._internal_enforce_payload(),
        )
        id_replay = replace(first)

        validation = validate_evidence_records((first, nonce_replay, snapshot_replay, id_replay))

        self.assertFalse(validation.valid)
        self.assertIn(MCPGateBlocker.NONCE_REPLAY, validation.blockers)
        self.assertIn(MCPGateBlocker.SNAPSHOT_REPLAY, validation.blockers)
        self.assertIn(MCPGateBlocker.EVIDENCE_ID_REPLAY, validation.blockers)

    def test_record_validation_rejects_non_monotonic_snapshots_and_window_gaps(self) -> None:
        first = self._snapshot(
            evidence_id="evidence-1",
            nonce="nonce-1",
            snapshot_id=2,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            recorded_at=_START + timedelta(hours=49),
            payload=self._internal_enforce_payload(),
        )
        later_with_lower_id = self._snapshot(
            evidence_id="evidence-2",
            nonce="nonce-2",
            snapshot_id=1,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            started_at=_START + timedelta(hours=50),
            duration=timedelta(hours=48),
            recorded_at=_START + timedelta(hours=99),
            payload=self._internal_enforce_payload(),
        )

        validation = validate_evidence_records((first, later_with_lower_id))

        self.assertIn(MCPGateBlocker.SNAPSHOT_NON_MONOTONIC, validation.blockers)
        self.assertIn(MCPGateBlocker.WINDOW_INCOMPLETE, validation.blockers)

    def test_off_to_shadow_accepts_only_matching_ci_conformance(self) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.OFF,
            duration=timedelta(hours=1),
            source=MCPEvidenceSource.CI,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=self._payload(
                kind=MCPEvidenceKind.CI_CONFORMANCE,
                ci_conformance_passed=True,
            ),
        )
        request = self._request(
            snapshot,
            current_stage=MCPRolloutStage.OFF,
            target_stage=MCPRolloutStage.INTERNAL_SHADOW,
        )

        evaluation = evaluate_mcp_stage_gate(request, (snapshot,))

        self.assertTrue(evaluation.allowed)
        self.assertEqual(evaluation.status, MCPGateStatus.PASSED)

    def test_ci_cannot_forge_production_or_promote_shadow_to_enforce(self) -> None:
        forged = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=24),
            source=MCPEvidenceSource.PRODUCTION,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=self._shadow_payload(),
        )
        self.assertIn(MCPGateBlocker.PROVENANCE_INVALID, validate_evidence_snapshot(forged))

        ci_shadow = self._snapshot(
            evidence_id="ci-shadow",
            nonce="ci-shadow-nonce",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=24),
            source=MCPEvidenceSource.CI,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            payload=self._shadow_payload(),
        )
        request = self._request(
            ci_shadow,
            current_stage=MCPRolloutStage.INTERNAL_SHADOW,
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
        )

        evaluation = evaluate_mcp_stage_gate(request, (ci_shadow,))

        self.assertFalse(evaluation.allowed)
        self.assertIn(MCPGateBlocker.SOURCE_POLICY_VIOLATION, evaluation.blockers)

    def test_shadow_gate_requires_24_hours_every_scenario_and_no_mismatch(self) -> None:
        good = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=24),
            payload=self._shadow_payload(),
        )
        request = self._request(
            good,
            current_stage=MCPRolloutStage.INTERNAL_SHADOW,
            target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
        )
        self.assertTrue(evaluate_mcp_stage_gate(request, (good,)).allowed)

        short = self._snapshot(
            evidence_id="short-window",
            nonce="short-window-nonce",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=23, minutes=59),
            payload=self._shadow_payload(),
        )
        short_result = evaluate_mcp_stage_gate(
            self._request(
                short,
                current_stage=MCPRolloutStage.INTERNAL_SHADOW,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
            ),
            (short,),
        )
        self.assertIn(MCPGateBlocker.WINDOW_TOO_SHORT, short_result.blockers)

        scenarios = list(self._shadow_scenarios())
        scenarios[0] = replace(scenarios[0], mismatched_count=1)
        mismatch = self._snapshot(
            evidence_id="mismatch",
            nonce="mismatch-nonce",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=24),
            payload=self._payload(
                kind=MCPEvidenceKind.INTERNAL_SHADOW,
                shadow_scenarios=tuple(scenarios),
            ),
        )
        mismatch_result = evaluate_mcp_stage_gate(
            self._request(
                mismatch,
                current_stage=MCPRolloutStage.INTERNAL_SHADOW,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
            ),
            (mismatch,),
        )
        self.assertIn(MCPGateBlocker.UNRESOLVED_MISMATCH, mismatch_result.blockers)

        without_public_http = tuple(
            item
            for item in self._shadow_scenarios()
            if item.scenario is not MCPShadowScenario.PUBLIC_HTTP_LEGACY_SSE_SUCCESS
        ) + (
            MCPShadowScenarioObservation(
                scenario=MCPShadowScenario.ALLOWLISTED_HTTP_LEGACY_SSE_SUCCESS,
                matched_count=3,
            ),
        )
        historical_does_not_substitute = self._snapshot(
            evidence_id="historical-http-only",
            nonce="historical-http-only-nonce",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            duration=timedelta(hours=24),
            payload=self._payload(
                kind=MCPEvidenceKind.INTERNAL_SHADOW,
                shadow_scenarios=without_public_http,
            ),
        )
        historical_result = evaluate_mcp_stage_gate(
            self._request(
                historical_does_not_substitute,
                current_stage=MCPRolloutStage.INTERNAL_SHADOW,
                target_stage=MCPRolloutStage.INTERNAL_ENFORCE,
            ),
            (historical_does_not_substitute,),
        )
        self.assertIn(
            MCPGateBlocker.SCENARIO_SAMPLE_INSUFFICIENT,
            historical_result.blockers,
        )

    def test_internal_enforce_requires_48_hours_all_drills_and_nonzero_traffic(self) -> None:
        good = self._snapshot(
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._internal_enforce_payload(),
        )
        request = self._request(
            good,
            current_stage=MCPRolloutStage.INTERNAL_ENFORCE,
            target_stage=MCPRolloutStage.COHORT_ENFORCE,
        )
        self.assertTrue(evaluate_mcp_stage_gate(request, (good,)).allowed)

        zero = self._snapshot(
            evidence_id="zero",
            nonce="zero-nonce",
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            duration=timedelta(hours=48),
            payload=self._payload(
                kind=MCPEvidenceKind.INTERNAL_ENFORCE,
                completed_drills=frozenset(MCPRolloutDrill),
            ),
        )
        zero_result = evaluate_mcp_stage_gate(
            self._request(
                zero,
                current_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                target_stage=MCPRolloutStage.COHORT_ENFORCE,
            ),
            (zero,),
        )
        self.assertIn(MCPGateBlocker.ZERO_DENOMINATOR, zero_result.blockers)

        no_drills = replace(
            good,
            evidence_id="no-drills",
            nonce="no-drills-nonce",
            payload=replace(good.payload, completed_drills=frozenset()),
        )
        no_drills = self._resign(no_drills)
        no_drills_result = evaluate_mcp_stage_gate(
            self._request(
                no_drills,
                current_stage=MCPRolloutStage.INTERNAL_ENFORCE,
                target_stage=MCPRolloutStage.COHORT_ENFORCE,
            ),
            (no_drills,),
        )
        self.assertIn(MCPGateBlocker.REQUIRED_DRILL_MISSING, no_drills_result.blockers)

    def test_cohort_gate_uses_real_terminal_denominator_and_separate_call_kinds(self) -> None:
        payload = self._performance_payload()
        self.assertEqual(payload.terminal_sample_count, 1000)
        self.assertEqual(sum(item.cancellation_count for item in payload.call_kinds), 1000)
        self.assertEqual(payload.shadow_observation_count, 5000)

        snapshot = self._snapshot(
            stage=MCPRolloutStage.COHORT_ENFORCE,
            duration=timedelta(days=7),
            payload=payload,
        )
        request = self._request(
            snapshot,
            current_stage=MCPRolloutStage.COHORT_ENFORCE,
            target_stage=MCPRolloutStage.FULL_ENFORCE,
        )

        evaluation = evaluate_mcp_stage_gate(request, (snapshot,))

        self.assertTrue(evaluation.allowed)

    def test_zero_fill_and_real_event_overlap_still_form_continuous_series(
        self,
    ) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.COHORT_ENFORCE,
            duration=timedelta(days=7),
            payload=self._performance_payload(),
        )
        ordinary = next(
            item
            for item in snapshot.payload.metric_buckets
            if item.metric_name is MCPMetricName.TOOL_CALLS_TOTAL
            and item.labels.call_kind is MCPCallKind.ORDINARY
            and item.labels.result_category is MCPMetricResultCategory.SUCCEEDED
        )
        overlapping_real_event = replace(
            ordinary,
            labels=replace(
                ordinary.labels,
                transport=MCPMetricTransport.STREAMABLE_HTTP,
            ),
            value=1,
        )
        payload = replace(
            snapshot.payload,
            metric_buckets=snapshot.payload.metric_buckets
            + (overlapping_real_event,),
        )
        overlapped = replace(snapshot, payload=payload)
        overlapped = self._resign(overlapped)

        evaluation = evaluate_mcp_stage_gate(
            self._request(
                overlapped,
                current_stage=MCPRolloutStage.COHORT_ENFORCE,
                target_stage=MCPRolloutStage.FULL_ENFORCE,
            ),
            (overlapped,),
        )

        self.assertNotIn(MCPGateBlocker.WINDOW_INCOMPLETE, evaluation.blockers)
        self.assertIn(MCPGateBlocker.METRIC_SUMMARY_MISMATCH, evaluation.blockers)

    def test_only_required_metric_series_control_window_completeness(self) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.COHORT_ENFORCE,
            duration=timedelta(days=7),
            payload=self._performance_payload(),
        )
        optional_labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            transport=MCPMetricTransport.STREAMABLE_HTTP,
            protocol_version=MCPMetricProtocolVersion.V2026_07_28,
            adapter=MCPMetricAdapter.PYTHON_2026,
            result_category=MCPMetricResultCategory.SUCCEEDED,
            error_category=MCPMetricErrorCategory.NONE,
            call_kind=MCPCallKind.ORDINARY,
        )
        optional_buckets = (
            MCPMetricBucket(
                metric_name=MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                bucket_started_at=snapshot.window_started_at,
                bucket_ended_at=snapshot.window_started_at + timedelta(minutes=1),
                labels=optional_labels,
                value=1,
            ),
            MCPMetricBucket(
                metric_name=MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                bucket_started_at=snapshot.window_ended_at - timedelta(minutes=1),
                bucket_ended_at=snapshot.window_ended_at,
                labels=optional_labels,
                value=1,
            ),
        )
        optional_gap = self._resign(
            replace(
                snapshot,
                payload=replace(
                    snapshot.payload,
                    metric_buckets=snapshot.payload.metric_buckets + optional_buckets,
                ),
            )
        )

        self.assertNotIn(
            MCPGateBlocker.WINDOW_INCOMPLETE,
            validate_evidence_snapshot(optional_gap),
        )

        target = next(
            item
            for item in snapshot.payload.metric_buckets
            if item.metric_name is MCPMetricName.SAFETY_RED_LINE_TOTAL
            and item.labels.red_line is MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK
        )
        required_gap = self._resign(
            replace(
                snapshot,
                payload=replace(
                    snapshot.payload,
                    metric_buckets=tuple(
                        item for item in snapshot.payload.metric_buckets if item is not target
                    ),
                    continuous_window=False,
                    missing_bucket_count=1,
                ),
            )
        )

        self.assertIn(
            MCPGateBlocker.WINDOW_INCOMPLETE,
            validate_evidence_snapshot(required_gap),
        )

    def test_legacy_baseline_buckets_do_not_inflate_user_scoped_summaries(
        self,
    ) -> None:
        snapshot = self._snapshot(
            stage=MCPRolloutStage.COHORT_ENFORCE,
            duration=timedelta(days=7),
            payload=self._performance_payload(),
        )
        ordinary_success = next(
            item
            for item in snapshot.payload.metric_buckets
            if item.metric_name is MCPMetricName.TOOL_CALLS_TOTAL
            and item.labels.call_kind is MCPCallKind.ORDINARY
            and item.labels.result_category is MCPMetricResultCategory.SUCCEEDED
        )
        legacy_baseline = replace(
            ordinary_success,
            labels=replace(
                ordinary_success.labels,
                execution_path=MCPMetricExecutionPath.LEGACY,
            ),
            value=594,
        )
        payload = replace(
            snapshot.payload,
            metric_buckets=snapshot.payload.metric_buckets + (legacy_baseline,),
        )
        with_baseline = self._resign(replace(snapshot, payload=payload))

        self.assertNotIn(
            MCPGateBlocker.METRIC_SUMMARY_MISMATCH,
            validate_evidence_snapshot(with_baseline),
        )

    def test_cohort_gate_blocks_samples_missing_windows_red_lines_and_regressions(self) -> None:
        call_kinds = list(self._performance_call_kinds())
        call_kinds[0] = replace(
            call_kinds[0],
            terminal_success_count=300,
            terminal_error_count=20,
            p95_latency_ms=111,
        )
        red_lines = list(self._zero_red_lines())
        red_lines[0] = replace(red_lines[0], count=1)
        snapshot = self._snapshot(
            stage=MCPRolloutStage.COHORT_ENFORCE,
            duration=timedelta(days=7),
            payload=self._payload(
                kind=MCPEvidenceKind.COHORT_ENFORCE,
                call_kinds=tuple(call_kinds),
                red_line_counts=tuple(red_lines),
                continuous_window=False,
                missing_bucket_count=1,
            ),
        )
        request = self._request(
            snapshot,
            current_stage=MCPRolloutStage.COHORT_ENFORCE,
            target_stage=MCPRolloutStage.FULL_ENFORCE,
        )

        evaluation = evaluate_mcp_stage_gate(request, (snapshot,))

        self.assertIn(MCPGateBlocker.SAMPLE_INSUFFICIENT, evaluation.blockers)
        self.assertIn(MCPGateBlocker.WINDOW_INCOMPLETE, evaluation.blockers)
        self.assertIn(MCPGateBlocker.SAFETY_RED_LINE, evaluation.blockers)
        self.assertIn(MCPGateBlocker.ERROR_RATE_REGRESSED, evaluation.blockers)
        self.assertIn(MCPGateBlocker.P95_LATENCY_REGRESSED, evaluation.blockers)

    def test_missing_call_kind_is_a_zero_denominator_not_a_blended_pass(self) -> None:
        only_ordinary = (self._performance_call_kinds()[0],)
        only_ordinary = (
            replace(only_ordinary[0], terminal_success_count=999, terminal_error_count=1),
        )
        snapshot = self._snapshot(
            stage=MCPRolloutStage.FULL_ENFORCE,
            duration=timedelta(days=7),
            payload=self._payload(
                kind=MCPEvidenceKind.FULL_ENFORCE,
                call_kinds=only_ordinary,
            ),
        )
        request = self._request(
            snapshot,
            current_stage=MCPRolloutStage.FULL_ENFORCE,
            target_stage=MCPRolloutStage.LEGACY_ASSEMBLY_OFF,
        )

        evaluation = evaluate_mcp_stage_gate(request, (snapshot,))

        self.assertIn(MCPGateBlocker.ZERO_DENOMINATOR, evaluation.blockers)

    def test_exposure_decrease_reuses_the_rollout_config_predicate(self) -> None:
        current = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": "80",
                "MCP_ENFORCE_HASH_SALT": "stable-salt",
            }
        )
        lower = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": "20",
                "MCP_ENFORCE_HASH_SALT": "stable-salt",
            }
        )
        changed_salt = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": "20",
                "MCP_ENFORCE_HASH_SALT": "different-salt",
            }
        )

        self.assertTrue(is_provable_mcp_exposure_decrease(current, lower))
        self.assertFalse(is_provable_mcp_exposure_decrease(current, changed_salt))

    def test_ledger_records_are_immutable_and_resolution_is_append_only(self) -> None:
        created_at = _START + timedelta(days=1)
        approval = MCPRolloutStageApproval(
            approval_id="approval-1",
            environment_id="prod",
            deployment_id="deployment-1",
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            config_fingerprint=_CONFIG_FINGERPRINT,
            evidence_id="evidence-1",
            reason="reviewed",
            approver="operator-1",
            created_at=created_at,
        )
        activation = MCPRolloutDeploymentActivation(
            activation_id="activation-1",
            environment_id="prod",
            deployment_id="deployment-1",
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            config_fingerprint=_CONFIG_FINGERPRINT,
            approval_id=approval.approval_id,
            previous_activation_id=None,
            operator_reason="approved promotion",
            is_rollback=False,
            created_at=created_at,
        )
        block = MCPRolloutPromotionBlock(
            block_id="block-1",
            environment_id="prod",
            deployment_id="deployment-1",
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            config_fingerprint=_CONFIG_FINGERPRINT,
            evidence_id="evidence-1",
            reason_code=MCPGateBlocker.SAFETY_RED_LINE,
            created_at=created_at,
        )
        resolution = MCPRolloutBlockResolution(
            resolution_id="resolution-1",
            block_id=block.block_id,
            approval_id=approval.approval_id,
            evidence_id="evidence-2",
            reason="remediated and independently verified",
            approver="operator-2",
            created_at=created_at + timedelta(hours=1),
        )

        self.assertEqual(active_mcp_promotion_blocks((block,), ()), (block,))
        self.assertEqual(active_mcp_promotion_blocks((block,), (resolution,)), ())
        self.assertEqual(activation.approval_id, approval.approval_id)
        with self.assertRaises(FrozenInstanceError):
            block.reason_code = MCPGateBlocker.WINDOW_TOO_SHORT  # type: ignore[misc]

    @staticmethod
    def _zero_red_lines() -> tuple[MCPRedLineCount, ...]:
        return tuple(MCPRedLineCount(red_line=red_line, count=0) for red_line in MCPSafetyRedLine)

    @staticmethod
    def _shadow_scenarios() -> tuple[MCPShadowScenarioObservation, ...]:
        return tuple(
            MCPShadowScenarioObservation(scenario=scenario, matched_count=3)
            for scenario in CURRENT_MCP_SHADOW_SCENARIOS
        )

    @staticmethod
    def _performance_call_kinds() -> tuple[MCPCallKindObservation, ...]:
        return (
            MCPCallKindObservation(
                call_kind=MCPCallKind.ORDINARY,
                terminal_success_count=595,
                terminal_error_count=5,
                cancellation_count=600,
                p95_latency_ms=109,
                baseline_success_count=594,
                baseline_error_count=6,
                baseline_p95_latency_ms=100,
            ),
            MCPCallKindObservation(
                call_kind=MCPCallKind.REMOTE_TASK,
                terminal_success_count=396,
                terminal_error_count=4,
                cancellation_count=400,
                p95_latency_ms=54,
                baseline_success_count=395,
                baseline_error_count=5,
                baseline_p95_latency_ms=50,
            ),
        )

    def _payload(
        self,
        *,
        kind: MCPEvidenceKind,
        call_kinds: tuple[MCPCallKindObservation, ...] = (),
        shadow_scenarios: tuple[MCPShadowScenarioObservation, ...] = (),
        completed_drills: frozenset[MCPRolloutDrill] = frozenset(),
        red_line_counts: tuple[MCPRedLineCount, ...] | None = None,
        continuous_window: bool = True,
        missing_bucket_count: int = 0,
        ci_conformance_passed: bool = False,
        shadow_observation_count: int = 0,
    ) -> MCPRolloutEvidencePayload:
        return MCPRolloutEvidencePayload(
            kind=kind,
            call_kinds=call_kinds,
            shadow_scenarios=shadow_scenarios,
            completed_drills=completed_drills,
            red_line_counts=self._zero_red_lines() if red_line_counts is None else red_line_counts,
            continuous_window=continuous_window,
            missing_bucket_count=missing_bucket_count,
            ci_conformance_passed=ci_conformance_passed,
            shadow_observation_count=shadow_observation_count,
            manifest_fingerprint=("c" * 64 if kind is MCPEvidenceKind.INTERNAL_SHADOW else None),
            fixture_fingerprint=("d" * 64 if kind is MCPEvidenceKind.INTERNAL_SHADOW else None),
            mapping_fingerprint=("e" * 64 if kind is MCPEvidenceKind.INTERNAL_SHADOW else None),
        )

    def _shadow_payload(self) -> MCPRolloutEvidencePayload:
        return self._payload(
            kind=MCPEvidenceKind.INTERNAL_SHADOW,
            shadow_scenarios=self._shadow_scenarios(),
        )

    def _internal_enforce_payload(self) -> MCPRolloutEvidencePayload:
        return self._payload(
            kind=MCPEvidenceKind.INTERNAL_ENFORCE,
            call_kinds=(self._performance_call_kinds()[0],),
            completed_drills=frozenset(MCPRolloutDrill),
        )

    def _performance_payload(self) -> MCPRolloutEvidencePayload:
        return self._payload(
            kind=MCPEvidenceKind.COHORT_ENFORCE,
            call_kinds=self._performance_call_kinds(),
            shadow_observation_count=5000,
        )

    @staticmethod
    def _snapshot(
        *,
        evidence_id: str = "evidence-1",
        environment_id: str = "prod",
        deployment_id: str = "deployment-1",
        config_fingerprint: str = _CONFIG_FINGERPRINT,
        nonce: str = "nonce-1",
        snapshot_id: int = 1,
        stage: MCPRolloutStage,
        duration: timedelta,
        payload: MCPRolloutEvidencePayload,
        source: MCPEvidenceSource = MCPEvidenceSource.PRODUCTION,
        producer: MCPEvidenceProducer = MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
        started_at: datetime = _START,
        recorded_at: datetime | None = None,
        complete_metrics: bool = True,
        attest: bool = True,
    ) -> MCPEvidenceSnapshot:
        ended_at = started_at + duration
        if source is MCPEvidenceSource.PRODUCTION and complete_metrics:
            payload = UserMCPRolloutEvidenceTests._with_complete_metrics(
                payload,
                started_at=started_at,
                ended_at=ended_at,
            )
        return MCPEvidenceSnapshot.seal(
            evidence_id=evidence_id,
            environment_id=environment_id,
            git_sha=_GIT_SHA,
            deployment_id=deployment_id,
            stage=stage,
            config_fingerprint=config_fingerprint,
            window_started_at=started_at,
            window_ended_at=ended_at,
            recorded_at=recorded_at or ended_at + timedelta(minutes=1),
            producer=producer,
            source=source,
            snapshot_id=snapshot_id,
            nonce=nonce,
            payload=payload,
            attestation_key_id=(
                _ATTESTATION_KEY_ID
                if source is MCPEvidenceSource.PRODUCTION and attest
                else None
            ),
            attestation_key=(
                _ATTESTATION_KEY
                if source is MCPEvidenceSource.PRODUCTION and attest
                else None
            ),
        )

    @staticmethod
    def _with_complete_metrics(
        payload: MCPRolloutEvidencePayload,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> MCPRolloutEvidencePayload:
        buckets: list[MCPMetricBucket] = []
        minute_starts = tuple(
            started_at + timedelta(minutes=offset)
            for offset in range(int((ended_at - started_at).total_seconds() // 60))
        )
        for red_line_count in payload.red_line_counts:
            for index, minute_started_at in enumerate(minute_starts):
                buckets.append(
                    MCPMetricBucket(
                        metric_name=MCPMetricName.SAFETY_RED_LINE_TOTAL,
                        bucket_started_at=minute_started_at,
                        bucket_ended_at=minute_started_at + timedelta(minutes=1),
                        labels=MCPMetricLabels(red_line=red_line_count.red_line),
                        value=red_line_count.count if index == 0 else 0,
                    )
                )
        for observation in payload.call_kinds:
            for result_category, count in (
                (MCPMetricResultCategory.SUCCEEDED, observation.terminal_success_count),
                (MCPMetricResultCategory.FAILED, observation.terminal_error_count),
                (MCPMetricResultCategory.UNKNOWN, 0),
                (MCPMetricResultCategory.CANCELLED, observation.cancellation_count),
            ):
                for index, minute_started_at in enumerate(minute_starts):
                    buckets.append(
                        MCPMetricBucket(
                            metric_name=MCPMetricName.TOOL_CALLS_TOTAL,
                            bucket_started_at=minute_started_at,
                            bucket_ended_at=minute_started_at + timedelta(minutes=1),
                            labels=MCPMetricLabels(
                                result_category=result_category,
                                call_kind=observation.call_kind,
                            ),
                            value=count if index == 0 else 0,
                        )
                    )
        return replace(payload, metric_buckets=tuple(buckets))

    @staticmethod
    def _resign(snapshot: MCPEvidenceSnapshot) -> MCPEvidenceSnapshot:
        return MCPEvidenceSnapshot.seal(
            evidence_id=snapshot.evidence_id,
            environment_id=snapshot.environment_id,
            git_sha=snapshot.git_sha,
            deployment_id=snapshot.deployment_id,
            stage=snapshot.stage,
            config_fingerprint=snapshot.config_fingerprint,
            window_started_at=snapshot.window_started_at,
            window_ended_at=snapshot.window_ended_at,
            recorded_at=snapshot.recorded_at,
            producer=snapshot.producer,
            source=snapshot.source,
            snapshot_id=snapshot.snapshot_id,
            nonce=snapshot.nonce,
            payload=snapshot.payload,
            attestation_key_id=_ATTESTATION_KEY_ID,
            attestation_key=_ATTESTATION_KEY,
        )

    @staticmethod
    def _request(
        snapshot: MCPEvidenceSnapshot,
        *,
        current_stage: MCPRolloutStage,
        target_stage: MCPRolloutStage,
    ) -> MCPStageGateRequest:
        return MCPStageGateRequest(
            evidence_id=snapshot.evidence_id,
            environment_id=snapshot.environment_id,
            evidence_deployment_id=snapshot.deployment_id,
            evidence_config_fingerprint=snapshot.config_fingerprint,
            current_stage=current_stage,
            target_stage=target_stage,
        )


if __name__ == "__main__":
    unittest.main()

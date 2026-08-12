from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from src.integrations.mcp.observability import (
    MCPRolloutMetricContext,
    MCPRolloutMetricRecorder,
    mcp_latency_bucket,
    mcp_evidence_snapshot_to_record,
    mcp_metric_bucket_to_record,
    mcp_terminal_call_sample_count,
)
from src.integrations.mcp.rollout_evidence import (
    MCPCallKind,
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPLatencyBucket,
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
    MCPRolloutStage,
    MCPRolloutEvidencePayload,
    MCPSafetyRedLine,
)
from src.integrations.mcp.safety_detectors import (
    AuthoritativeMCPSafetyDetectorRegistry,
    register_authoritative_mcp_safety_detectors,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


_NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)
_ATTESTATION_KEY_ID = "prod-rollout-v1"
_ATTESTATION_KEY = b"test-only-production-rollout-attestation-key"
_TRUSTED_ATTESTATION_KEYS = {_ATTESTATION_KEY_ID: _ATTESTATION_KEY}


class UserMCPRolloutMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.engine = create_sqlite_engine(f"{self.temp_dir.name}/state.db")
        self.addCleanup(self.engine.dispose)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.context = MCPRolloutMetricContext(
            environment_id="staging",
            deployment_id="deploy-a",
            stage=MCPRolloutStage.COHORT_ENFORCE,
            config_fingerprint="b" * 64,
        )
        self.labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            transport=MCPMetricTransport.STREAMABLE_HTTP,
            protocol_version=MCPMetricProtocolVersion.V2026_07_28,
            adapter=MCPMetricAdapter.PYTHON_2026,
            result_category=MCPMetricResultCategory.SUCCEEDED,
            error_category=MCPMetricErrorCategory.NONE,
            call_kind=MCPCallKind.ORDINARY,
        )

    def test_deterministic_bucket_identity_aggregates_durably(self) -> None:
        recorder = MCPRolloutMetricRecorder(self.storage, self.context)
        kwargs = {
            "labels": self.labels,
            "bucket_started_at": _NOW,
            "bucket_ended_at": _NOW + timedelta(minutes=1),
        }

        first = asyncio.run(
            recorder.record_count(MCPMetricName.ROUTE_REQUESTS_TOTAL, value=2, **kwargs)
        )
        second = asyncio.run(
            recorder.record_count(MCPMetricName.ROUTE_REQUESTS_TOTAL, value=3, **kwargs)
        )

        self.assertEqual(first.metric_bucket_id, second.metric_bucket_id)
        self.assertEqual(second.value, 5)

    def test_gauge_overwrites_same_bucket_instead_of_accumulating(self) -> None:
        recorder = MCPRolloutMetricRecorder(self.storage, self.context)
        labels = replace(
            self.labels,
            transport=MCPMetricTransport.NOT_APPLICABLE,
            protocol_version=MCPMetricProtocolVersion.NOT_APPLICABLE,
            adapter=MCPMetricAdapter.NOT_APPLICABLE,
            result_category=MCPMetricResultCategory.NOT_APPLICABLE,
            error_category=MCPMetricErrorCategory.NOT_APPLICABLE,
            call_kind=MCPCallKind.REMOTE_TASK,
        )
        kwargs = {
            "labels": labels,
            "bucket_started_at": _NOW,
            "bucket_ended_at": _NOW + timedelta(minutes=1),
        }

        first = asyncio.run(
            recorder.record_gauge(
                MCPMetricName.REMOTE_TASKS_ACTIVE, value=7, **kwargs
            )
        )
        second = asyncio.run(
            recorder.record_gauge(
                MCPMetricName.REMOTE_TASKS_ACTIVE, value=2, **kwargs
            )
        )

        self.assertEqual(first.metric_bucket_id, second.metric_bucket_id)
        self.assertEqual(second.value, 2)

    def test_zero_fill_makes_required_series_explicit_without_overwriting_events(
        self,
    ) -> None:
        recorder = MCPRolloutMetricRecorder(self.storage, self.context)
        safety_gaps = []
        registry = AuthoritativeMCPSafetyDetectorRegistry(
            recorder,
            gap_sink=safety_gaps.append,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
        )
        detectors = register_authoritative_mcp_safety_detectors(registry)

        def attest_safety_interval(started_at, ended_at) -> None:
            for detector in detectors.values():
                detector.attest_interval(started_at, ended_at)

        recorder.configure_safety_detector_registry(registry)
        recorder.configure_safety_interval_probes(attest_safety_interval)
        failed_labels = MCPMetricLabels(
            execution_path=MCPMetricExecutionPath.USER_SCOPED,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
            result_category=MCPMetricResultCategory.FAILED,
            error_category=MCPMetricErrorCategory.UNKNOWN,
            call_kind=MCPCallKind.ORDINARY,
        )
        asyncio.run(
            recorder.record_count(
                MCPMetricName.TOOL_CALLS_TOTAL,
                labels=failed_labels,
                bucket_started_at=_NOW,
                bucket_ended_at=_NOW + timedelta(minutes=1),
                value=3,
            )
        )

        asyncio.run(
            recorder.record_required_zero_series(
                bucket_started_at=_NOW,
                bucket_ended_at=_NOW + timedelta(minutes=1),
            )
        )

        buckets = asyncio.run(
            self.storage.list_mcp_rollout_metric_buckets(
                self.context.environment_id,
                self.context.deployment_id,
                self.context.stage.value,
                window_started_at=_NOW,
                window_ended_at=_NOW + timedelta(minutes=1),
            )
        )
        failed = next(
            item
            for item in buckets
            if item.metric_name == MCPMetricName.TOOL_CALLS_TOTAL.value
            and item.call_kind == MCPCallKind.ORDINARY.value
            and item.result_category == MCPMetricResultCategory.FAILED.value
            and item.error_category == MCPMetricErrorCategory.UNKNOWN.value
        )
        self.assertEqual(failed.value, 3)
        red_line_series = [item for item in buckets if item.red_line is not None]
        self.assertEqual(len(red_line_series), len(MCPSafetyRedLine))
        self.assertEqual({item.value for item in red_line_series}, {0})
        self.assertEqual(
            {item.red_line for item in red_line_series},
            {red_line.value for red_line in MCPSafetyRedLine},
        )
        self.assertEqual(safety_gaps, [])
        terminal_series = {
            (item.call_kind, item.result_category)
            for item in buckets
            if item.metric_name == MCPMetricName.TOOL_CALLS_TOTAL.value
        }
        self.assertEqual(
            terminal_series,
            {
                (call_kind.value, result.value)
                for call_kind in MCPCallKind
                for result in (
                    MCPMetricResultCategory.SUCCEEDED,
                    MCPMetricResultCategory.FAILED,
                    MCPMetricResultCategory.UNKNOWN,
                    MCPMetricResultCategory.CANCELLED,
                )
            },
        )

    def test_shadow_mismatch_uses_only_closed_internal_labels(self) -> None:
        recorder = MCPRolloutMetricRecorder(self.storage, self.context)

        record = asyncio.run(recorder.record_shadow_mismatch(observed_at=_NOW))

        self.assertEqual(
            record.metric_name,
            MCPMetricName.ROUTE_SHADOW_MISMATCH_TOTAL.value,
        )
        self.assertEqual(record.execution_path, "user_scoped")
        self.assertEqual(record.routing_mode, "shadow")
        self.assertEqual(record.result_category, "failed")
        self.assertEqual(record.error_category, "validation")

    def test_arbitrary_labels_and_sensitive_values_cannot_enter_record(self) -> None:
        bucket = MCPMetricBucket(
            metric_name=MCPMetricName.TOOL_CALLS_ACTIVE,
            bucket_started_at=_NOW,
            bucket_ended_at=_NOW + timedelta(minutes=1),
            labels=self.labels,
            value=1,
        )
        secret = "Bearer secret-api-key"

        with self.assertRaises(TypeError):
            mcp_metric_bucket_to_record(
                replace(bucket, labels={"owner_user_id": "user-1", "url": secret}),  # type: ignore[arg-type]
                context=self.context,
            )
        with self.assertRaisesRegex(ValueError, "execution_path"):
            mcp_metric_bucket_to_record(
                replace(
                    bucket,
                    labels=replace(
                        self.labels,
                        execution_path=MCPRolloutStage.FULL_ENFORCE,  # type: ignore[arg-type]
                    ),
                ),
                context=self.context,
            )

        record = mcp_metric_bucket_to_record(bucket, context=self.context)
        serialized = json.dumps(asdict(record), default=str)
        self.assertNotIn("owner_user_id", serialized)
        self.assertNotIn("server_id", serialized)
        self.assertNotIn("tool_name", serialized)
        self.assertNotIn(secret, serialized)

    def test_red_line_is_closed_and_part_of_deterministic_bucket_identity(self) -> None:
        first = MCPMetricBucket(
            metric_name=MCPMetricName.SAFETY_RED_LINE_TOTAL,
            bucket_started_at=_NOW,
            bucket_ended_at=_NOW + timedelta(minutes=1),
            labels=replace(
                self.labels,
                call_kind=None,
                red_line=MCPSafetyRedLine.CROSS_USER_ACCESS,
            ),
            value=0,
        )
        second = replace(
            first,
            labels=replace(first.labels, red_line=MCPSafetyRedLine.SECRET_EXPOSURE),
        )

        first_record = mcp_metric_bucket_to_record(first, context=self.context)
        second_record = mcp_metric_bucket_to_record(second, context=self.context)

        self.assertNotEqual(
            first_record.metric_bucket_id, second_record.metric_bucket_id
        )
        self.assertEqual(first_record.red_line, "cross_user_access")
        self.assertEqual(second_record.red_line, "secret_exposure")
        recorder = MCPRolloutMetricRecorder(self.storage, self.context)
        asyncio.run(recorder.record_bucket(first))
        asyncio.run(recorder.record_bucket(second))
        listed = asyncio.run(
            self.storage.list_mcp_rollout_metric_buckets(
                self.context.environment_id,
                self.context.deployment_id,
                self.context.stage.value,
                window_started_at=_NOW,
                window_ended_at=_NOW + timedelta(minutes=1),
            )
        )
        self.assertEqual(
            {item.red_line for item in listed},
            {"cross_user_access", "secret_exposure"},
        )
        with self.assertRaisesRegex(ValueError, "red_line"):
            mcp_metric_bucket_to_record(
                replace(
                    first, labels=replace(first.labels, red_line="cross_user_access")
                ),  # type: ignore[arg-type]
                context=self.context,
            )

    def test_only_sealed_valid_evidence_is_converted_to_allowlisted_record(
        self,
    ) -> None:
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id="ci-evidence-1",
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id="deploy-a",
            stage=MCPRolloutStage.OFF,
            config_fingerprint="b" * 64,
            window_started_at=_NOW,
            window_ended_at=_NOW + timedelta(hours=1),
            recorded_at=_NOW + timedelta(hours=1, seconds=1),
            producer=MCPEvidenceProducer.CI_PIPELINE,
            source=MCPEvidenceSource.CI,
            snapshot_id=1,
            nonce="ci-nonce-1",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.CI_CONFORMANCE,
                ci_conformance_passed=True,
            ),
        )

        record = mcp_evidence_snapshot_to_record(snapshot)

        self.assertEqual(record.payload["kind"], "ci_conformance")
        self.assertEqual(record.payload_digest, snapshot.payload_digest)
        with self.assertRaisesRegex(ValueError, "digest_invalid"):
            mcp_evidence_snapshot_to_record(replace(snapshot, payload_digest="0" * 64))

    def test_terminal_denominator_excludes_non_terminal_and_pre_dispatch_results(
        self,
    ) -> None:
        categories = (
            MCPMetricResultCategory.SUCCEEDED,
            MCPMetricResultCategory.FAILED,
            MCPMetricResultCategory.UNKNOWN,
            MCPMetricResultCategory.CANCELLED,
            MCPMetricResultCategory.INPUT_REQUIRED,
            MCPMetricResultCategory.TASK_CREATED,
            MCPMetricResultCategory.PERMISSION_DENIED,
            MCPMetricResultCategory.NOT_COMPARABLE,
        )
        buckets = [
            MCPMetricBucket(
                metric_name=MCPMetricName.TOOL_CALL_DURATION_SECONDS,
                bucket_started_at=_NOW,
                bucket_ended_at=_NOW + timedelta(minutes=1),
                labels=replace(self.labels, result_category=category),
                value=10,
            )
            for category in categories
        ]
        buckets.append(
            replace(
                buckets[0],
                metric_name=MCPMetricName.ROUTE_REQUESTS_TOTAL,
                value=1000,
            )
        )

        self.assertEqual(mcp_terminal_call_sample_count(buckets), 30)

    def test_production_evidence_conversion_requires_trusted_attestation_key(
        self,
    ) -> None:
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id="prod-evidence-1",
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id="deploy-a",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint="b" * 64,
            window_started_at=_NOW,
            window_ended_at=_NOW + timedelta(hours=24),
            recorded_at=_NOW + timedelta(hours=24, seconds=1),
            producer=MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
            source=MCPEvidenceSource.PRODUCTION,
            snapshot_id=1,
            nonce="prod-nonce-1",
            payload=MCPRolloutEvidencePayload(kind=MCPEvidenceKind.INTERNAL_SHADOW),
            attestation_key_id=_ATTESTATION_KEY_ID,
            attestation_key=_ATTESTATION_KEY,
        )

        with self.assertRaisesRegex(ValueError, "attestation_missing"):
            mcp_evidence_snapshot_to_record(snapshot)
        with self.assertRaisesRegex(ValueError, "metric_series_missing"):
            mcp_evidence_snapshot_to_record(
                snapshot,
                trusted_attestation_keys=_TRUSTED_ATTESTATION_KEYS,
            )

    def test_latency_boundaries_are_exclusive_series_buckets(self) -> None:
        cases = {
            0.0: MCPLatencyBucket.LE_100_MS,
            0.1: MCPLatencyBucket.LE_100_MS,
            0.100001: MCPLatencyBucket.LE_500_MS,
            0.5: MCPLatencyBucket.LE_500_MS,
            1.0: MCPLatencyBucket.LE_1_S,
            5.0: MCPLatencyBucket.LE_5_S,
            30.0: MCPLatencyBucket.LE_30_S,
            120.0: MCPLatencyBucket.LE_120_S,
            120.000001: MCPLatencyBucket.GT_120_S,
        }
        self.assertEqual({value: mcp_latency_bucket(value) for value in cases}, cases)
        for invalid in (-0.1, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                mcp_latency_bucket(invalid)


if __name__ == "__main__":
    unittest.main()

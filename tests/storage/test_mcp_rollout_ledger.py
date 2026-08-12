from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.core.models import (
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutEvidenceSnapshot,
    MCPRolloutInstanceConfigLease,
    MCPRolloutMetricBucket,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class MCPRolloutLedgerTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)

    def _evidence(
        self,
        evidence_id: str,
        *,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
        snapshot_id: int,
        nonce: str,
        window_started_at: datetime,
        window_ended_at: datetime,
        source: str = "production",
    ) -> MCPRolloutEvidenceSnapshot:
        return MCPRolloutEvidenceSnapshot(
            evidence_id=evidence_id,
            environment_id="staging",
            git_sha="a" * 40,
            deployment_id=deployment_id,
            stage=stage,
            config_fingerprint=config_fingerprint,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            recorded_at=window_ended_at + timedelta(seconds=1),
            producer=(
                "production_snapshot_producer" if source == "production" else "ci_pipeline"
            ),
            source=source,
            snapshot_id=snapshot_id,
            nonce=nonce,
            evidence_kind="ci_conformance" if source == "ci" else stage,
            payload={"continuous_window": True},
            payload_digest="b" * 64,
            attestation_key_id=("prod-rollout-v1" if source == "production" else None),
            attestation_signature=("c" * 64 if source == "production" else None),
        )

    def _approval(
        self,
        approval_id: str,
        *,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
        evidence_id: str,
        offset_seconds: int,
    ) -> MCPRolloutStageApproval:
        return MCPRolloutStageApproval(
            approval_id=approval_id,
            environment_id="staging",
            deployment_id=deployment_id,
            stage=stage,
            config_fingerprint=config_fingerprint,
            evidence_id=evidence_id,
            reason="approved rollout transition",
            approver="operator-a",
            created_at=self.now + timedelta(seconds=offset_seconds),
        )

    def _activation(
        self,
        activation_id: str,
        approval: MCPRolloutStageApproval,
        *,
        previous_activation_id: str | None = None,
        is_rollback: bool = False,
        offset_seconds: int,
    ) -> MCPRolloutDeploymentActivation:
        return MCPRolloutDeploymentActivation(
            activation_id=activation_id,
            environment_id=approval.environment_id,
            deployment_id=approval.deployment_id,
            stage=approval.stage,
            config_fingerprint=approval.config_fingerprint,
            approval_id=approval.approval_id,
            evidence_id=approval.evidence_id,
            previous_activation_id=previous_activation_id,
            operator_reason="activate approved deployment",
            is_rollback=is_rollback,
            created_at=self.now + timedelta(seconds=offset_seconds),
        )

    def test_metric_bucket_upsert_accumulates_only_closed_low_cardinality_labels(self) -> None:
        activation_evidence = self._evidence(
            "evidence-metric-activation",
            deployment_id="deploy-off",
            stage="off",
            config_fingerprint="fingerprint-off",
            snapshot_id=1,
            nonce="nonce-metric-activation",
            window_started_at=self.now - timedelta(minutes=10),
            window_ended_at=self.now - timedelta(minutes=5),
            source="ci",
        )
        asyncio.run(
            self.storage.append_mcp_rollout_evidence_snapshot(activation_evidence)
        )
        approval = self._approval(
            "approval-metric-activation",
            deployment_id="deploy-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-a",
            evidence_id=activation_evidence.evidence_id,
            offset_seconds=-1,
        )
        asyncio.run(self.storage.append_mcp_rollout_stage_approval(approval))
        asyncio.run(
            self.storage.activate_mcp_rollout_deployment(
                self._activation(
                    "activation-metric",
                    approval,
                    offset_seconds=0,
                )
            )
        )
        bucket = MCPRolloutMetricBucket(
            metric_bucket_id="bucket-a",
            environment_id="staging",
            deployment_id="deploy-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-a",
            metric_name="mcp_route_requests_total",
            bucket_started_at=self.now,
            bucket_ended_at=self.now + timedelta(minutes=1),
            execution_path="legacy",
            routing_mode="shadow",
            transport="streamable_http",
            protocol_version="2026-07-28",
            adapter="python_2026",
            result_category="succeeded",
            error_category="none",
            latency_bucket="not_applicable",
            value=2,
            created_at=self.now,
            updated_at=self.now,
        )
        saved = asyncio.run(self.storage.upsert_mcp_rollout_metric_bucket(bucket))
        self.assertEqual(saved.value, 2)
        accumulated = asyncio.run(
            self.storage.upsert_mcp_rollout_metric_bucket(
                replace(
                    bucket,
                    metric_bucket_id="bucket-b",
                    value=3,
                    updated_at=self.now + timedelta(seconds=1),
                )
            )
        )
        self.assertEqual((accumulated.metric_bucket_id, accumulated.value), ("bucket-a", 5))
        listed = asyncio.run(
            self.storage.list_mcp_rollout_metric_buckets(
                "staging",
                "deploy-a",
                "internal_shadow",
                window_started_at=self.now,
                window_ended_at=self.now + timedelta(minutes=1),
            )
        )
        self.assertEqual([item.value for item in listed], [5])
        first_red_line = replace(
            bucket,
            metric_bucket_id="bucket-red-line-a",
            metric_name="mcp_safety_red_line_total",
            call_kind=None,
            red_line="cross_user_access",
            value=1,
        )
        second_red_line = replace(
            first_red_line,
            metric_bucket_id="bucket-red-line-b",
            red_line="secret_exposure",
        )
        saved_first_red_line = asyncio.run(
            self.storage.upsert_mcp_rollout_metric_bucket(first_red_line)
        )
        saved_second_red_line = asyncio.run(
            self.storage.upsert_mcp_rollout_metric_bucket(second_red_line)
        )
        self.assertEqual(saved_first_red_line.red_line, "cross_user_access")
        self.assertEqual(saved_second_red_line.red_line, "secret_exposure")
        accumulated_first_red_line = asyncio.run(
            self.storage.upsert_mcp_rollout_metric_bucket(
                replace(
                    first_red_line,
                    metric_bucket_id="bucket-red-line-replay",
                    value=2,
                )
            )
        )
        self.assertEqual(accumulated_first_red_line.value, 3)
        listed = asyncio.run(
            self.storage.list_mcp_rollout_metric_buckets(
                "staging",
                "deploy-a",
                "internal_shadow",
                window_started_at=self.now,
                window_ended_at=self.now + timedelta(minutes=1),
            )
        )
        self.assertEqual(
            {item.red_line: item.value for item in listed},
            {None: 5, "cross_user_access": 3, "secret_exposure": 1},
        )
        blocks = asyncio.run(
            self.storage.list_active_mcp_rollout_promotion_blocks("staging")
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].reason_code, "safety_red_line_nonzero")
        self.assertEqual(blocks[0].evidence_id, activation_evidence.evidence_id)
        self.assertEqual(blocks[0].deployment_id, bucket.deployment_id)
        with self.assertRaisesRegex(ValueError, "routing_mode"):
            asyncio.run(
                self.storage.upsert_mcp_rollout_metric_bucket(
                    replace(bucket, metric_bucket_id="bad", routing_mode="user-controlled")
                )
            )
        with self.assertRaisesRegex(ValueError, "red_line"):
            asyncio.run(
                self.storage.upsert_mcp_rollout_metric_bucket(
                    replace(first_red_line, metric_bucket_id="bad-red-line", red_line="custom")
                )
            )
        for label, hostile in (
            (
                "coarse",
                replace(
                    bucket,
                    metric_bucket_id="bad-coarse",
                    bucket_ended_at=self.now + timedelta(minutes=2),
                ),
            ),
            (
                "subminute",
                replace(
                    bucket,
                    metric_bucket_id="bad-subminute",
                    bucket_ended_at=self.now + timedelta(seconds=30),
                ),
            ),
            (
                "unaligned",
                replace(
                    bucket,
                    metric_bucket_id="bad-unaligned",
                    bucket_started_at=self.now + timedelta(seconds=1),
                    bucket_ended_at=self.now + timedelta(minutes=1, seconds=1),
                ),
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, "UTC-aligned minute"):
                    asyncio.run(
                        self.storage.upsert_mcp_rollout_metric_bucket(hostile)
                    )

    def test_positive_safety_red_line_without_exact_activation_rolls_back(self) -> None:
        bucket = MCPRolloutMetricBucket(
            metric_bucket_id="bucket-unactivated-red-line",
            environment_id="staging",
            deployment_id="deploy-unactivated",
            stage="internal_shadow",
            config_fingerprint="fingerprint-unactivated",
            metric_name="mcp_safety_red_line_total",
            bucket_started_at=self.now,
            bucket_ended_at=self.now + timedelta(minutes=1),
            execution_path="user_scoped",
            routing_mode="shadow",
            transport="not_applicable",
            protocol_version="not_applicable",
            adapter="not_applicable",
            result_category="failed",
            error_category="validation",
            red_line="cross_user_access",
            latency_bucket="not_applicable",
            value=1,
            created_at=self.now,
            updated_at=self.now,
        )

        with self.assertRaisesRegex(ValueError, "exact activation"):
            asyncio.run(self.storage.upsert_mcp_rollout_metric_bucket(bucket))

        self.assertEqual(
            asyncio.run(
                self.storage.list_mcp_rollout_metric_buckets(
                    "staging",
                    "deploy-unactivated",
                    "internal_shadow",
                    window_started_at=self.now,
                    window_ended_at=self.now + timedelta(minutes=1),
                )
            ),
            [],
        )
        self.assertEqual(
            asyncio.run(
                self.storage.list_active_mcp_rollout_promotion_blocks("staging")
            ),
            [],
        )
    def test_evidence_is_append_only_and_rejects_nonce_snapshot_and_monotonic_replay(self) -> None:
        first = self._evidence(
            "evidence-a",
            deployment_id="deploy-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-a",
            snapshot_id=1,
            nonce="nonce-a",
            window_started_at=self.now,
            window_ended_at=self.now + timedelta(hours=1),
        )
        self.assertEqual(
            asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(first)),
            first,
        )
        with self.assertRaisesRegex(ValueError, "ID replay"):
            asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(first))
        with self.assertRaisesRegex(ValueError, "nonce replay"):
            asyncio.run(
                self.storage.append_mcp_rollout_evidence_snapshot(
                    replace(first, evidence_id="evidence-b", snapshot_id=2)
                )
            )
        with self.assertRaisesRegex(ValueError, "snapshot replay"):
            asyncio.run(
                self.storage.append_mcp_rollout_evidence_snapshot(
                    replace(first, evidence_id="evidence-c", nonce="nonce-c")
                )
            )
        third = replace(
            first,
            evidence_id="evidence-d",
            nonce="nonce-d",
            snapshot_id=3,
            window_started_at=first.window_ended_at,
            window_ended_at=first.window_ended_at + timedelta(hours=1),
            recorded_at=first.window_ended_at + timedelta(hours=1, seconds=1),
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(third))
        with self.assertRaisesRegex(ValueError, "monotonic"):
            asyncio.run(
                self.storage.append_mcp_rollout_evidence_snapshot(
                    replace(
                        third,
                        evidence_id="evidence-e",
                        nonce="nonce-e",
                        snapshot_id=2,
                        window_ended_at=third.window_ended_at + timedelta(hours=1),
                        recorded_at=third.recorded_at + timedelta(hours=1),
                    )
                )
            )

    def test_evidence_attestation_contract_is_closed_by_source(self) -> None:
        production = self._evidence(
            "evidence-production",
            deployment_id="deploy-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-a",
            snapshot_id=1,
            nonce="nonce-production",
            window_started_at=self.now,
            window_ended_at=self.now + timedelta(hours=1),
        )
        with self.assertRaisesRegex(ValueError, "attestation is required"):
            asyncio.run(
                self.storage.append_mcp_rollout_evidence_snapshot(
                    replace(
                        production,
                        attestation_key_id=None,
                        attestation_signature=None,
                    )
                )
            )
        with self.assertRaisesRegex(ValueError, "attestation is required"):
            asyncio.run(
                self.storage.append_mcp_rollout_evidence_snapshot(
                    replace(production, attestation_signature="forged")
                )
            )
        ci = replace(
            production,
            evidence_id="evidence-ci-attested",
            source="ci",
            producer="ci_pipeline",
            evidence_kind="ci_conformance",
        )
        with self.assertRaisesRegex(ValueError, "cannot be attested"):
            asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(ci))

    def test_all_attestation_and_metric_evidence_blockers_are_persistable(self) -> None:
        evidence = self._evidence(
            "evidence-blockers",
            deployment_id="deploy-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-a",
            snapshot_id=1,
            nonce="nonce-blockers",
            window_started_at=self.now,
            window_ended_at=self.now + timedelta(hours=1),
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(evidence))
        for index, reason_code in enumerate(
            (
                "attestation_missing",
                "attestation_invalid",
                "metric_series_missing",
                "metric_summary_mismatch",
                "safety_red_line_nonzero",
            )
        ):
            saved = asyncio.run(
                self.storage.append_mcp_rollout_promotion_block(
                    MCPRolloutPromotionBlock(
                        block_id=f"block-{index}",
                        environment_id=evidence.environment_id,
                        deployment_id=evidence.deployment_id,
                        stage=evidence.stage,
                        config_fingerprint=evidence.config_fingerprint,
                        evidence_id=evidence.evidence_id,
                        reason_code=reason_code,
                        created_at=evidence.recorded_at + timedelta(seconds=index + 1),
                    )
                )
            )
            self.assertEqual(saved.reason_code, reason_code)

    def test_active_block_serializes_activation_resolution_and_instance_admission(self) -> None:
        ci = self._evidence(
            "evidence-ci",
            deployment_id="deploy-off",
            stage="off",
            config_fingerprint="fingerprint-off",
            snapshot_id=1,
            nonce="nonce-ci",
            window_started_at=self.now,
            window_ended_at=self.now + timedelta(minutes=5),
            source="ci",
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(ci))
        shadow_approval = self._approval(
            "approval-shadow",
            deployment_id="deploy-shadow",
            stage="internal_shadow",
            config_fingerprint="fingerprint-shadow",
            evidence_id=ci.evidence_id,
            offset_seconds=400,
        )
        asyncio.run(self.storage.append_mcp_rollout_stage_approval(shadow_approval))
        shadow_activation = self._activation(
            "activation-shadow",
            shadow_approval,
            offset_seconds=401,
        )
        asyncio.run(self.storage.activate_mcp_rollout_deployment(shadow_activation))
        first_lease = MCPRolloutInstanceConfigLease(
            instance_config_id="instance-config-a",
            environment_id="staging",
            deployment_id="deploy-shadow",
            instance_id="api-a",
            stage="internal_shadow",
            config_fingerprint="fingerprint-shadow",
            activation_id=shadow_activation.activation_id,
            lease_expires_at=self.now + timedelta(hours=4),
            created_at=self.now + timedelta(seconds=402),
            updated_at=self.now + timedelta(seconds=402),
        )
        asyncio.run(self.storage.save_mcp_rollout_instance_config_lease(first_lease))

        shadow_evidence = self._evidence(
            "evidence-shadow-1",
            deployment_id="deploy-shadow",
            stage="internal_shadow",
            config_fingerprint="fingerprint-shadow",
            snapshot_id=1,
            nonce="nonce-shadow-1",
            window_started_at=self.now + timedelta(minutes=5),
            window_ended_at=self.now + timedelta(hours=1, minutes=5),
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(shadow_evidence))
        block = MCPRolloutPromotionBlock(
            block_id="block-a",
            environment_id="staging",
            deployment_id="deploy-shadow",
            stage="internal_shadow",
            config_fingerprint="fingerprint-shadow",
            evidence_id=shadow_evidence.evidence_id,
            reason_code="safety_red_line",
            created_at=self.now + timedelta(hours=1, minutes=6),
        )
        asyncio.run(self.storage.append_mcp_rollout_promotion_block(block))
        self.assertEqual(
            [item.block_id for item in asyncio.run(
                self.storage.list_active_mcp_rollout_promotion_blocks("staging")
            )],
            ["block-a"],
        )

        promote_approval = self._approval(
            "approval-promote",
            deployment_id="deploy-enforce",
            stage="internal_enforce",
            config_fingerprint="fingerprint-enforce",
            evidence_id=shadow_evidence.evidence_id,
            offset_seconds=4000,
        )
        asyncio.run(self.storage.append_mcp_rollout_stage_approval(promote_approval))
        promote_activation = self._activation(
            "activation-promote",
            promote_approval,
            previous_activation_id=shadow_activation.activation_id,
            offset_seconds=4001,
        )
        with self.assertRaisesRegex(ValueError, "active promotion block"):
            asyncio.run(self.storage.activate_mcp_rollout_deployment(promote_activation))
        with self.assertRaisesRegex(ValueError, "instance admission"):
            asyncio.run(
                self.storage.save_mcp_rollout_instance_config_lease(
                    replace(
                        first_lease,
                        instance_config_id="instance-config-b",
                        instance_id="api-b",
                    )
                )
            )

        healthy_evidence = self._evidence(
            "evidence-shadow-2",
            deployment_id="deploy-shadow",
            stage="internal_shadow",
            config_fingerprint="fingerprint-shadow",
            snapshot_id=2,
            nonce="nonce-shadow-2",
            window_started_at=shadow_evidence.window_ended_at,
            window_ended_at=shadow_evidence.window_ended_at + timedelta(hours=1),
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(healthy_evidence))
        resolution_approval = self._approval(
            "approval-resolution",
            deployment_id="deploy-enforce-2",
            stage="internal_enforce",
            config_fingerprint="fingerprint-enforce-2",
            evidence_id=healthy_evidence.evidence_id,
            offset_seconds=8000,
        )
        asyncio.run(self.storage.append_mcp_rollout_stage_approval(resolution_approval))
        resolution = MCPRolloutBlockResolution(
            resolution_id="resolution-a",
            block_id=block.block_id,
            approval_id=resolution_approval.approval_id,
            evidence_id=healthy_evidence.evidence_id,
            reason="verified remediation evidence",
            approver="operator-b",
            created_at=self.now + timedelta(seconds=8001),
        )
        asyncio.run(self.storage.append_mcp_rollout_block_resolution(resolution))
        self.assertEqual(
            asyncio.run(self.storage.list_active_mcp_rollout_promotion_blocks("staging")),
            [],
        )
        with self.assertRaisesRegex(ValueError, "already resolved"):
            asyncio.run(
                self.storage.append_mcp_rollout_block_resolution(
                    replace(resolution, resolution_id="resolution-replay")
                )
            )
        with self.assertRaisesRegex(ValueError, "consumed by a block resolution"):
            asyncio.run(
                self.storage.activate_mcp_rollout_deployment(
                    self._activation(
                        "activation-resolution-replay",
                        resolution_approval,
                        previous_activation_id=shadow_activation.activation_id,
                        offset_seconds=8002,
                    )
                )
            )
        activated = asyncio.run(self.storage.activate_mcp_rollout_deployment(promote_activation))
        self.assertEqual(activated.activation_id, "activation-promote")
        with self.assertRaisesRegex(ValueError, "replay|consumed"):
            asyncio.run(self.storage.activate_mcp_rollout_deployment(promote_activation))

    def test_rollback_instance_lease_allows_active_block_but_rejects_fingerprint_mismatch(self) -> None:
        ci = self._evidence(
            "rollback-ci",
            deployment_id="deploy-off",
            stage="off",
            config_fingerprint="fingerprint-off",
            snapshot_id=1,
            nonce="rollback-ci-nonce",
            window_started_at=self.now,
            window_ended_at=self.now + timedelta(minutes=5),
            source="ci",
        )
        asyncio.run(self.storage.append_mcp_rollout_evidence_snapshot(ci))
        approval = self._approval(
            "rollback-approval",
            deployment_id="deploy-rollback",
            stage="off",
            config_fingerprint="fingerprint-rollback",
            evidence_id=ci.evidence_id,
            offset_seconds=400,
        )
        asyncio.run(self.storage.append_mcp_rollout_stage_approval(approval))
        activation = self._activation(
            "rollback-activation",
            approval,
            is_rollback=True,
            offset_seconds=401,
        )
        asyncio.run(self.storage.activate_mcp_rollout_deployment(activation))
        lease = MCPRolloutInstanceConfigLease(
            instance_config_id="rollback-instance-a",
            environment_id="staging",
            deployment_id="deploy-rollback",
            instance_id="api-a",
            stage="off",
            config_fingerprint="fingerprint-rollback",
            activation_id=activation.activation_id,
            lease_expires_at=self.now + timedelta(hours=1),
            created_at=self.now + timedelta(seconds=402),
            updated_at=self.now + timedelta(seconds=402),
        )
        asyncio.run(self.storage.save_mcp_rollout_instance_config_lease(lease))
        with self.assertRaisesRegex(ValueError, "config fingerprint mismatch"):
            asyncio.run(
                self.storage.save_mcp_rollout_instance_config_lease(
                    replace(
                        lease,
                        instance_config_id="rollback-instance-b",
                        instance_id="api-b",
                        config_fingerprint="different-fingerprint",
                    )
                )
            )

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from src.integrations.mcp.rollout_evidence import (
    MCPMetricRoutingMode,
    MCPSafetyRedLine,
)
from src.integrations.mcp.safety_detectors import (
    AUTHORITATIVE_MCP_SAFETY_HOOKS,
    AuthoritativeMCPSafetyDetectorRegistry,
    MCPSafetyMetricGap,
    register_authoritative_mcp_safety_detectors,
)
from src.integrations.mcp.audit import MCPAuditService
from src.integrations.mcp.dispatch_coordinator import UserMCPDispatchCoordinator
from src.integrations.mcp.gateway import MCPGateway


_START = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
_END = _START + timedelta(minutes=1)


class _Recorder:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.error: Exception | None = None

    async def record_count(self, metric_name, **kwargs):
        if self.error is not None:
            raise self.error
        record = {"metric_name": metric_name, **kwargs}
        self.records.append(record)
        return record


class MCPSafetyDetectorRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = _Recorder()
        self.gaps: list[MCPSafetyMetricGap] = []
        self.registry = AuthoritativeMCPSafetyDetectorRegistry(
            self.recorder,
            gap_sink=self.gaps.append,
            routing_mode=MCPMetricRoutingMode.ENFORCE,
        )

    def test_registry_requires_exact_closed_authoritative_hooks(self) -> None:
        with self.assertRaisesRegex(ValueError, "authoritative"):
            self.registry.register(
                MCPSafetyRedLine.CROSS_USER_ACCESS, "gateway.custom_hook"
            )
        detectors = register_authoritative_mcp_safety_detectors(self.registry)
        self.assertEqual(set(detectors), set(MCPSafetyRedLine))
        self.assertTrue(self.registry.complete)
        self.assertEqual(
            {item.red_line: item.hook_id for item in detectors.values()},
            AUTHORITATIVE_MCP_SAFETY_HOOKS,
        )
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register(
                MCPSafetyRedLine.CROSS_USER_ACCESS,
                AUTHORITATIVE_MCP_SAFETY_HOOKS[
                    MCPSafetyRedLine.CROSS_USER_ACCESS
                ],
            )

    def test_zero_requires_exact_fresh_attestation_from_every_detector(self) -> None:
        detectors = register_authoritative_mcp_safety_detectors(self.registry)
        for detector in detectors.values():
            detector.attest_interval(_START, _END)
        records = asyncio.run(
            self.registry.record_verified_zero_series(
                bucket_started_at=_START, bucket_ended_at=_END
            )
        )
        self.assertEqual(len(records), len(MCPSafetyRedLine))
        self.assertEqual(
            {record["labels"].red_line for record in records}, set(MCPSafetyRedLine)
        )
        self.assertTrue(all(record["value"] == 0 for record in records))

        with self.assertRaisesRegex(RuntimeError, "interval"):
            asyncio.run(
                self.registry.record_verified_zero_series(
                    bucket_started_at=_START, bucket_ended_at=_END
                )
            )
        self.assertEqual(self.gaps[-1].reason_code, "interval_attestation_missing")
        with self.assertRaisesRegex(ValueError, "full UTC minute"):
            detectors[MCPSafetyRedLine.CROSS_USER_ACCESS].attest_interval(
                _START, _END + timedelta(seconds=1)
            )

    def test_unregistered_unhealthy_and_missing_attestation_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            asyncio.run(
                self.registry.record_verified_zero_series(
                    bucket_started_at=_START, bucket_ended_at=_END
                )
            )
        self.assertEqual(self.gaps[-1].reason_code, "detector_unregistered")

        detectors = register_authoritative_mcp_safety_detectors(self.registry)
        for detector in detectors.values():
            detector.attest_interval(_START, _END)
        detectors[MCPSafetyRedLine.SECRET_EXPOSURE].mark_unhealthy()
        with self.assertRaisesRegex(RuntimeError, "not healthy"):
            asyncio.run(
                self.registry.record_verified_zero_series(
                    bucket_started_at=_START, bucket_ended_at=_END
                )
            )
        self.assertEqual(self.gaps[-1].reason_code, "detector_unhealthy")

    def test_boundary_violation_is_additive_and_reason_is_closed(self) -> None:
        detector = self.registry.register(
            MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS,
            AUTHORITATIVE_MCP_SAFETY_HOOKS[
                MCPSafetyRedLine.ENDPOINT_POLICY_BYPASS
            ],
        )
        asyncio.run(
            detector.report_violation(
                reason_code="endpoint_policy_rejected", observed_at=_START
            )
        )
        asyncio.run(
            detector.report_violation(
                reason_code="endpoint_policy_rejected", observed_at=_START
            )
        )
        self.assertEqual([record["value"] for record in self.recorder.records], [1, 1])
        with self.assertRaisesRegex(ValueError, "reason"):
            asyncio.run(
                detector.report_violation(
                    reason_code="https://private.example/secret", observed_at=_START
                )
            )

    def test_metric_failure_persists_gap_and_is_not_swallowed(self) -> None:
        detector = self.registry.register(
            MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK,
            AUTHORITATIVE_MCP_SAFETY_HOOKS[
                MCPSafetyRedLine.PERSISTENT_RESOURCE_LEAK
            ],
        )
        self.recorder.error = RuntimeError("storage unavailable")
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            asyncio.run(
                detector.report_violation(
                    reason_code="cleanup_failed", observed_at=_START
                )
            )
        self.assertEqual(self.gaps[-1].reason_code, "safety_metric_write_failed")

    def test_each_authoritative_boundary_attests_only_its_owned_hooks(self) -> None:
        detectors = register_authoritative_mcp_safety_detectors(self.registry)
        audit = MCPAuditService.__new__(MCPAuditService)
        audit._safety_detector = detectors[MCPSafetyRedLine.SECRET_EXPOSURE]
        gateway = MCPGateway.__new__(MCPGateway)
        gateway._safety_detectors = detectors
        dispatch = UserMCPDispatchCoordinator.__new__(UserMCPDispatchCoordinator)
        dispatch._safety_detectors = detectors
        producers = (
            audit.attest_safety_interval,
            gateway.attest_safety_interval,
            dispatch.attest_safety_interval,
        )
        for producer in producers:
            producer(_START, _END)
        records = asyncio.run(self.registry.record_verified_zero_series(
            bucket_started_at=_START, bucket_ended_at=_END
        ))
        self.assertEqual({record["labels"].red_line for record in records}, set(MCPSafetyRedLine))

        for missing_index in range(len(producers)):
            recorder = _Recorder()
            gaps: list[MCPSafetyMetricGap] = []
            registry = AuthoritativeMCPSafetyDetectorRegistry(
                recorder, gap_sink=gaps.append, routing_mode=MCPMetricRoutingMode.ENFORCE
            )
            owned = register_authoritative_mcp_safety_detectors(registry)
            objects = (
                MCPAuditService.__new__(MCPAuditService),
                MCPGateway.__new__(MCPGateway),
                UserMCPDispatchCoordinator.__new__(UserMCPDispatchCoordinator),
            )
            objects[0]._safety_detector = owned[MCPSafetyRedLine.SECRET_EXPOSURE]
            objects[1]._safety_detectors = owned
            objects[2]._safety_detectors = owned
            calls = tuple(obj.attest_safety_interval for obj in objects)
            for index, call in enumerate(calls):
                if index != missing_index:
                    call(_START, _END)
            with self.assertRaisesRegex(RuntimeError, "interval"):
                asyncio.run(registry.record_verified_zero_series(
                    bucket_started_at=_START, bucket_ended_at=_END
                ))
            self.assertEqual(gaps[-1].reason_code, "interval_attestation_missing")


if __name__ == "__main__":
    unittest.main()

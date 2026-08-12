from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.core.models import (
    MCP_ROLLOUT_DRILLS,
    MCPRolloutDrillObservation,
    seal_mcp_rollout_drill_observation,
    validate_mcp_rollout_drill_observation,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class MCPRolloutDrillObservationTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)

    def _observation(
        self,
        observation_id: str = "drill-observation-a",
        **changes: object,
    ) -> MCPRolloutDrillObservation:
        observation = MCPRolloutDrillObservation(
            drill_observation_id=observation_id,
            environment_id="staging",
            deployment_id="deployment-a",
            config_fingerprint="a" * 64,
            drill="cancellation",
            outcome="passed",
            observed_at=self.now,
            recorded_at=self.now + timedelta(seconds=1),
            expires_at=self.now + timedelta(days=30),
            payload_digest="",
        )
        return seal_mcp_rollout_drill_observation(
            replace(observation, **changes)
        )

    def test_all_closed_drill_values_append_and_list(self) -> None:
        expected: list[MCPRolloutDrillObservation] = []
        for index, drill in enumerate(sorted(MCP_ROLLOUT_DRILLS)):
            observation = self._observation(
                f"drill-observation-{index}",
                drill=drill,
                outcome="failed" if index % 2 else "passed",
                observed_at=self.now + timedelta(minutes=index),
                recorded_at=self.now + timedelta(minutes=index, seconds=1),
            )
            expected.append(
                asyncio.run(
                    self.storage.append_mcp_rollout_drill_observation(observation)
                )
            )

        listed = asyncio.run(
            self.storage.list_mcp_rollout_drill_observations(
                "staging",
                "deployment-a",
                window_started_at=self.now,
                window_ended_at=self.now + timedelta(hours=1),
            )
        )

        self.assertEqual(listed, expected)
        self.assertEqual({item.drill for item in listed}, MCP_ROLLOUT_DRILLS)

    def test_exact_replay_is_idempotent_but_tamper_and_scope_replay_fail(self) -> None:
        observation = self._observation()
        first = asyncio.run(
            self.storage.append_mcp_rollout_drill_observation(observation)
        )
        replay = asyncio.run(
            self.storage.append_mcp_rollout_drill_observation(observation)
        )
        self.assertEqual(replay, first)

        tampered = seal_mcp_rollout_drill_observation(
            replace(observation, outcome="failed")
        )
        with self.assertRaisesRegex(ValueError, "ID payload conflict"):
            asyncio.run(
                self.storage.append_mcp_rollout_drill_observation(tampered)
            )

        scope_replay = self._observation("drill-observation-b")
        with self.assertRaisesRegex(ValueError, "scope replay"):
            asyncio.run(
                self.storage.append_mcp_rollout_drill_observation(scope_replay)
            )

    def test_validation_rejects_tamper_open_values_and_invalid_timestamps(self) -> None:
        observation = self._observation()
        self.assertEqual(
            observation.payload_digest,
            "6d340e594ab2e305d62cf524984ce920a6921e10345491878f7d22cb7a720ad5",
        )
        cases = (
            (replace(observation, payload_digest="0" * 64), "digest_invalid"),
            (
                seal_mcp_rollout_drill_observation(
                    replace(observation, stage="cohort_enforce")
                ),
                "scope_invalid",
            ),
            (
                seal_mcp_rollout_drill_observation(
                    replace(observation, drill="unreviewed_drill")
                ),
                "drill_invalid",
            ),
            (
                seal_mcp_rollout_drill_observation(
                    replace(observation, outcome="unknown")
                ),
                "outcome_invalid",
            ),
            (
                replace(
                    observation,
                    observed_at=self.now.replace(tzinfo=None),
                ),
                "timestamp_invalid",
            ),
            (
                seal_mcp_rollout_drill_observation(
                    replace(observation, expires_at=observation.recorded_at)
                ),
                "timestamp_order_invalid",
            ),
        )
        for invalid, expected_blocker in cases:
            with self.subTest(expected_blocker=expected_blocker):
                self.assertIn(
                    expected_blocker,
                    validate_mcp_rollout_drill_observation(invalid),
                )

    def test_list_uses_half_open_window_and_requires_retention_through_end(self) -> None:
        inside = self._observation(
            "inside",
            observed_at=self.now + timedelta(minutes=1),
            recorded_at=self.now + timedelta(minutes=1, seconds=1),
            expires_at=self.now + timedelta(hours=1),
        )
        end_boundary = self._observation(
            "end-boundary",
            drill="fair_queueing",
            observed_at=self.now + timedelta(minutes=10),
            recorded_at=self.now + timedelta(minutes=10, seconds=1),
            expires_at=self.now + timedelta(hours=1),
        )
        expires_at_end = self._observation(
            "expires-at-end",
            drill="flag_rollback",
            observed_at=self.now + timedelta(minutes=2),
            recorded_at=self.now + timedelta(minutes=2, seconds=1),
            expires_at=self.now + timedelta(minutes=10),
        )
        for observation in (inside, end_boundary, expires_at_end):
            asyncio.run(
                self.storage.append_mcp_rollout_drill_observation(observation)
            )

        listed = asyncio.run(
            self.storage.list_mcp_rollout_drill_observations(
                "staging",
                "deployment-a",
                window_started_at=self.now,
                window_ended_at=self.now + timedelta(minutes=10),
            )
        )

        self.assertEqual(listed, [inside])


if __name__ == "__main__":
    import unittest

    unittest.main()

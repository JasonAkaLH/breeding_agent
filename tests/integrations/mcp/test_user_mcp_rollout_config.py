from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from src.integrations.mcp.rollout import (
    MCPExecutionPath,
    MCPExposureChange,
    MCPRouteReason,
    MCPRoutingMode,
    MCPRolloutConfig,
    compare_mcp_rollout_exposure,
    stable_user_bucket,
)


class MCPRolloutConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_defaults_are_fail_closed_to_legacy_only(self) -> None:
        config = MCPRolloutConfig.from_env({})

        self.assertFalse(config.gateway_enabled)
        self.assertEqual(config.routing_mode, MCPRoutingMode.OFF)
        self.assertTrue(config.legacy_enabled)
        assignment = config.assign_authenticated_user("auth-user-1")
        self.assertEqual(assignment.real_path, MCPExecutionPath.LEGACY)
        self.assertFalse(assignment.shadow_enabled)
        self.assertEqual(assignment.reason_code, MCPRouteReason.ROUTING_OFF)
        with self.assertRaises(FrozenInstanceError):
            assignment.shadow_enabled = True  # type: ignore[misc]

        unavailable = MCPRolloutConfig.from_env(
            {"MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false"}
        ).assign_authenticated_user("auth-user-1")
        self.assertEqual(unavailable.real_path, MCPExecutionPath.UNAVAILABLE)
        self.assertEqual(unavailable.reason_code, MCPRouteReason.NO_EXECUTION_PATH)

    def test_parses_all_canonical_environment_variables(self) -> None:
        cohort_path = self._write_cohort_file(
            config_version="cohorts-v3",
            user_cohorts={"auth-user-1": ["internal", "early"]},
        )
        config = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_COHORTS": "internal,early",
                "MCP_ENFORCE_PERCENT": "25",
                "MCP_ENFORCE_HASH_SALT": "fixed-rollout-salt",
                "MCP_ENFORCE_COHORT_CONFIG_FILE": str(cohort_path),
            }
        )

        self.assertEqual(config.routing_mode, MCPRoutingMode.ENFORCE)
        self.assertEqual(config.enforce_cohorts, frozenset({"internal", "early"}))
        self.assertEqual(config.enforce_percent, 25)
        self.assertEqual(config.cohort_config_version, "cohorts-v3")
        self.assertEqual(len(config.cohort_file_digest), 64)
        self.assertEqual(len(config.fingerprint), 64)
        with self.assertRaises(TypeError):
            config._user_cohorts["auth-user-2"] = frozenset({"internal"})  # type: ignore[index]

    def test_rejects_open_values_and_illegal_combinations(self) -> None:
        invalid_environments = (
            {"MCP_USER_SCOPED_GATEWAY_ENABLED": "yes"},
            {"MCP_ROUTING_MODE": "ENFORCE"},
            {"MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "1"},
            {"MCP_ENFORCE_PERCENT": "101"},
            {"MCP_ENFORCE_PERCENT": "1.5"},
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "shadow",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            },
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "off",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            },
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "shadow",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
            },
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": "25",
            },
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "false",
                "MCP_ENFORCE_PERCENT": "99",
                "MCP_ENFORCE_HASH_SALT": "salt",
            },
        )
        for env in invalid_environments:
            with self.subTest(env=env), self.assertRaises(ValueError):
                MCPRolloutConfig.from_env(env)

    def test_cohort_file_uses_closed_schema_and_restrictive_permissions(self) -> None:
        good_path = self._write_cohort_file(config_version="v1", user_cohorts={"u1": ["a"]})
        base_env = self._enforce_env(cohort_path=good_path, cohorts="a")
        self.assertEqual(MCPRolloutConfig.from_env(base_env).cohorts_for_user("u1"), frozenset({"a"}))

        os.chmod(good_path, 0o640)
        with self.assertRaisesRegex(ValueError, "permission"):
            MCPRolloutConfig.from_env(base_env)

        cases = (
            {"schema_version": "wrong", "config_version": "v1", "user_cohorts": {}},
            {"schema_version": "maf.mcp.rollout_cohorts.v1", "config_version": "v1", "user_cohorts": {}, "extra": True},
            {"schema_version": "maf.mcp.rollout_cohorts.v1", "config_version": "", "user_cohorts": {}},
            {"schema_version": "maf.mcp.rollout_cohorts.v1", "config_version": "v1", "user_cohorts": {"u1": ["a", "a"]}},
        )
        for index, payload in enumerate(cases):
            path = self.workspace / f"bad-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o440)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                MCPRolloutConfig.from_env(self._enforce_env(cohort_path=path, cohorts="a"))

    def test_nonempty_cohort_filter_requires_mapping_and_digest_affects_fingerprint(self) -> None:
        with self.assertRaisesRegex(ValueError, "MCP_ENFORCE_COHORT_CONFIG_FILE"):
            MCPRolloutConfig.from_env(self._enforce_env(cohorts="internal"))

        first_path = self._write_cohort_file(config_version="v1", user_cohorts={"u1": ["internal"]})
        first = MCPRolloutConfig.from_env(self._enforce_env(cohort_path=first_path, cohorts="internal"))
        second_path = self._write_cohort_file(config_version="v1", user_cohorts={"u2": ["internal"]}, name="second.json")
        second = MCPRolloutConfig.from_env(self._enforce_env(cohort_path=second_path, cohorts="internal"))
        self.assertNotEqual(first.cohort_file_digest, second.cohort_file_digest)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

        missing_mapping = self._write_cohort_file(config_version="v2", user_cohorts={"u1": ["other"]}, name="missing.json")
        with self.assertRaisesRegex(ValueError, "no mapped users"):
            MCPRolloutConfig.from_env(self._enforce_env(cohort_path=missing_mapping, cohorts="internal"))

    def test_hmac_bucket_and_assignments_are_stable_and_do_not_expose_raw_user(self) -> None:
        bucket = stable_user_bucket(authenticated_user_id="private-user", salt="stable-salt")
        self.assertEqual(bucket, 26)
        self.assertEqual(bucket, stable_user_bucket(authenticated_user_id="private-user", salt="stable-salt"))
        self.assertNotEqual(bucket, stable_user_bucket(authenticated_user_id="another-user", salt="stable-salt"))
        self.assertGreaterEqual(bucket, 0)
        self.assertLess(bucket, 100)

        selected = MCPRolloutConfig.from_env(self._enforce_env(percent="100"))
        assignment = selected.assign_authenticated_user("private-user")
        self.assertEqual(assignment.real_path, MCPExecutionPath.USER_SCOPED)
        self.assertEqual(assignment.reason_code, MCPRouteReason.ENFORCE_SELECTED)
        self.assertNotIn("private-user", repr(assignment))

        shadow = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "shadow",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            }
        ).assign_authenticated_user("private-user")
        self.assertEqual(shadow.real_path, MCPExecutionPath.LEGACY)
        self.assertTrue(shadow.shadow_enabled)
        self.assertEqual(shadow.reason_code, MCPRouteReason.SHADOW_ENABLED)

    def test_cohort_and_percent_misses_choose_legacy_or_unavailable(self) -> None:
        path = self._write_cohort_file(config_version="v1", user_cohorts={"member": ["internal"]})
        config = MCPRolloutConfig.from_env(self._enforce_env(cohort_path=path, cohorts="internal", percent="0"))
        percent_miss = config.assign_authenticated_user("member")
        cohort_miss = config.assign_authenticated_user("outsider")
        self.assertEqual(percent_miss.real_path, MCPExecutionPath.LEGACY)
        self.assertEqual(percent_miss.reason_code, MCPRouteReason.PERCENT_NOT_SELECTED)
        self.assertEqual(cohort_miss.reason_code, MCPRouteReason.COHORT_NOT_SELECTED)
        custom_owner_cohort_miss = config.assign_authenticated_user(
            "outsider",
            has_user_scoped_server=True,
        )
        self.assertEqual(
            custom_owner_cohort_miss.real_path,
            MCPExecutionPath.UNAVAILABLE,
        )
        self.assertEqual(
            custom_owner_cohort_miss.reason_code,
            MCPRouteReason.USER_SERVER_ROLLOUT_UNAVAILABLE,
        )

        full = MCPRolloutConfig.from_env(self._enforce_env(percent="100", legacy="false"))
        self.assertEqual(full.assign_authenticated_user("member").real_path, MCPExecutionPath.USER_SCOPED)

    def test_custom_server_owner_cannot_fall_back_to_legacy_on_percent_miss(self) -> None:
        config = MCPRolloutConfig.from_env(self._enforce_env(percent="0"))

        assignment = config.assign_authenticated_user(
            "custom-server-owner",
            has_user_scoped_server=True,
        )

        self.assertEqual(assignment.real_path, MCPExecutionPath.UNAVAILABLE)
        self.assertEqual(
            assignment.reason_code,
            MCPRouteReason.USER_SERVER_ROLLOUT_UNAVAILABLE,
        )

    def test_explicit_registered_legacy_capability_can_bypass_enforce_selection(self) -> None:
        config = MCPRolloutConfig.from_env(self._enforce_env(percent="100"))

        assignment = config.assign_authenticated_user(
            "custom-server-owner",
            has_user_scoped_server=True,
            explicit_legacy_capability=True,
        )

        self.assertEqual(assignment.real_path, MCPExecutionPath.LEGACY)
        self.assertEqual(
            assignment.reason_code,
            MCPRouteReason.EXPLICIT_LEGACY_CAPABILITY,
        )

    def test_exposure_comparison_only_accepts_provable_strict_decrease(self) -> None:
        enforce_all = MCPRolloutConfig.from_env(self._enforce_env(percent="80"))
        enforce_less = MCPRolloutConfig.from_env(self._enforce_env(percent="20"))
        enforce_equal = MCPRolloutConfig.from_env(self._enforce_env(percent="80"))
        salt_changed = MCPRolloutConfig.from_env(self._enforce_env(percent="20", salt="other-salt"))
        shadow = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "shadow",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_HASH_SALT": "stable-salt",
            }
        )
        canonical_off = MCPRolloutConfig.from_env({})
        off_with_stale_salt = MCPRolloutConfig.from_env(
            {"MCP_ENFORCE_HASH_SALT": "stale-salt"}
        )

        self.assertEqual(compare_mcp_rollout_exposure(enforce_all, enforce_less), MCPExposureChange.DECREASE)
        self.assertEqual(compare_mcp_rollout_exposure(enforce_all, enforce_equal), MCPExposureChange.UNCHANGED)
        self.assertEqual(compare_mcp_rollout_exposure(enforce_all, salt_changed), MCPExposureChange.INCREASE)
        self.assertEqual(compare_mcp_rollout_exposure(enforce_all, shadow), MCPExposureChange.DECREASE)
        self.assertEqual(compare_mcp_rollout_exposure(shadow, enforce_less), MCPExposureChange.INCREASE)
        self.assertEqual(compare_mcp_rollout_exposure(enforce_all, canonical_off), MCPExposureChange.DECREASE)
        self.assertEqual(compare_mcp_rollout_exposure(shadow, canonical_off), MCPExposureChange.DECREASE)
        self.assertEqual(compare_mcp_rollout_exposure(canonical_off, canonical_off), MCPExposureChange.UNCHANGED)
        self.assertEqual(compare_mcp_rollout_exposure(canonical_off, off_with_stale_salt), MCPExposureChange.INCREASE)

        old_path = self._write_cohort_file(config_version="v1", user_cohorts={"u1": ["a"], "u2": ["b"]}, name="old.json")
        changed_path = self._write_cohort_file(config_version="v2", user_cohorts={"u1": ["a"], "u2": ["c"]}, name="changed.json")
        old = MCPRolloutConfig.from_env(self._enforce_env(cohort_path=old_path, cohorts="a,b"))
        mapping_changed = MCPRolloutConfig.from_env(self._enforce_env(cohort_path=changed_path, cohorts="a"))
        self.assertEqual(compare_mcp_rollout_exposure(old, mapping_changed), MCPExposureChange.INCREASE)

    def _write_cohort_file(
        self,
        *,
        config_version: str,
        user_cohorts: dict[str, list[str]],
        name: str = "cohorts.json",
    ) -> Path:
        path = self.workspace / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": "maf.mcp.rollout_cohorts.v1",
                    "config_version": config_version,
                    "user_cohorts": user_cohorts,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o440)
        return path

    @staticmethod
    def _enforce_env(
        *,
        cohort_path: Path | None = None,
        cohorts: str = "",
        percent: str = "25",
        salt: str = "stable-salt",
        legacy: str = "true",
    ) -> dict[str, str]:
        env = {
            "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
            "MCP_ROUTING_MODE": "enforce",
            "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": legacy,
            "MCP_ENFORCE_COHORTS": cohorts,
            "MCP_ENFORCE_PERCENT": percent,
            "MCP_ENFORCE_HASH_SALT": salt,
        }
        if cohort_path is not None:
            env["MCP_ENFORCE_COHORT_CONFIG_FILE"] = str(cohort_path)
        return env


if __name__ == "__main__":
    unittest.main()
